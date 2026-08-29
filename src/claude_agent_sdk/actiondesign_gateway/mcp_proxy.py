from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .mcp_deployment import McpDeploymentRef
from .redaction import redact_value
from .secret_store import ManagedSecretStoreError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SENSITIVE_HEADER_RE = re.compile(
    r"(?:authorization|cookie|api[-_]?key|secret|password|token|credential)",
    re.IGNORECASE,
)
_SAFE_SUBPROCESS_ENV = (
    "APPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PROGRAMDATA",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)
_MAX_SCHEMA_CHARACTERS = 500_000
_MAX_ARGUMENT_CHARACTERS = 2_000_000
_MAX_RESULT_CHARACTERS = 2_000_000
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 50_000
McpExecutionMode = Literal["auto", "browser_direct", "gateway_proxy"]
McpAuthMode = Literal["none", "backend_secret"]


class McpProxyError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class _ValidatedDnsNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and pin each new MCP TCP connection to validated IP addresses."""

    def __init__(
        self,
        *,
        expected_host: str,
        expected_port: int,
        allow_private: bool,
    ) -> None:
        self._expected_host = _normalize_dns_host(expected_host)
        self._expected_port = expected_port
        self._allow_private = allow_private
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if (
            _normalize_dns_host(host) != self._expected_host
            or port != self._expected_port
        ):
            raise OSError("MCP transport attempted an unexpected network target")
        addresses = await _resolve_host_addresses_async(host, port)
        if not self._allow_private:
            _require_global_addresses(addresses)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # pragma: no cover - exercised by httpcore retries
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("MCP transport DNS resolution returned no addresses")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise OSError("MCP transport does not allow Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


class McpSecretResolver(Protocol):
    """Minimal Secret surface required by the transport-only MCP proxy."""

    def resolve_value(self, secret_id: str) -> str: ...


class McpSecretBinding(_StrictModel):
    secret_id: str = Field(alias="secretId")
    target: Literal["env", "header"]
    name: str
    value_prefix: str = Field(default="", alias="valuePrefix", max_length=64)

    @field_validator("secret_id")
    @classmethod
    def _secret_id_is_safe(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("MCP Secret id is invalid")
        return value

    @field_validator("name")
    @classmethod
    def _binding_name_is_safe(cls, value: str, info: Any) -> str:
        target = info.data.get("target")
        pattern = _ENV_NAME_RE if target == "env" else _HEADER_NAME_RE
        if not pattern.fullmatch(value):
            raise ValueError("MCP Secret binding name is invalid")
        return value

    @field_validator("value_prefix")
    @classmethod
    def _prefix_is_safe(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("MCP Secret valuePrefix contains control characters")
        return value


class StdioTransportConfig(_StrictModel):
    command: str = Field(min_length=1, max_length=4_096)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = Field(default="", max_length=4_096)
    secret_bindings: list[McpSecretBinding] = Field(
        default_factory=list,
        alias="secretBindings",
        max_length=32,
    )

    @field_validator("command", "cwd")
    @classmethod
    def _path_text_is_safe(cls, value: str) -> str:
        if value and (value != value.strip() or "\x00" in value or "\r" in value or "\n" in value):
            raise ValueError("MCP stdio path is invalid")
        return value

    @field_validator("args")
    @classmethod
    def _arguments_are_safe(cls, value: list[str]) -> list[str]:
        for argument in value:
            if (
                not argument
                or len(argument) > 8_192
                or "\x00" in argument
                or "\r" in argument
                or "\n" in argument
                or ".." in Path(argument).parts
            ):
                raise ValueError("MCP stdio argument is invalid")
        return value

    @model_validator(mode="after")
    def _bindings_are_stdio_only(self) -> StdioTransportConfig:
        if any(binding.target != "env" for binding in self.secret_bindings):
            raise ValueError("stdio MCP Secret bindings must target env")
        names = [binding.name for binding in self.secret_bindings]
        if len(names) != len(set(names)):
            raise ValueError("stdio MCP Secret binding names must be unique")
        return self


class StreamableHttpTransportConfig(_StrictModel):
    url: str = Field(min_length=1, max_length=4_096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=64)
    secret_bindings: list[McpSecretBinding] = Field(
        default_factory=list,
        alias="secretBindings",
        max_length=32,
    )
    sse_read_timeout_seconds: float = Field(
        default=300.0,
        alias="sseReadTimeoutSeconds",
        ge=1.0,
        le=3_600.0,
    )

    @field_validator("url")
    @classmethod
    def _url_is_structurally_safe(cls, value: str, info: ValidationInfo) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("streamable HTTP MCP URL is invalid")
        parsed = urlsplit(value)
        allow_browser_http = bool(
            info.context and info.context.get("allow_browser_http")
        )
        allowed_schemes = {"http", "https"} if allow_browser_http else {"https"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            requirement = "HTTP(S)" if allow_browser_http else "HTTPS"
            raise ValueError(
                f"streamable HTTP MCP URL must use absolute {requirement}"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("streamable HTTP MCP URL port is invalid") from exc
        if port is not None and not (1 <= port <= 65_535):
            raise ValueError("streamable HTTP MCP URL port is invalid")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("streamable HTTP MCP URL must not contain credentials, query, or fragment")
        path = parsed.path or "/"
        if (
            not path.startswith("/")
            or "\\" in path
            or "%" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError("streamable HTTP MCP path is invalid")
        return value

    @field_validator("headers")
    @classmethod
    def _static_headers_are_public(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: set[str] = set()
        for name, item in value.items():
            lowered = name.lower()
            if (
                not _HEADER_NAME_RE.fullmatch(name)
                or lowered in normalized
                or _SENSITIVE_HEADER_RE.search(name)
                or len(item) > 8_192
                or "\x00" in item
                or "\r" in item
                or "\n" in item
            ):
                raise ValueError("streamable HTTP MCP static header is invalid")
            normalized.add(lowered)
        return value

    @model_validator(mode="after")
    def _bindings_are_http_only(self) -> StreamableHttpTransportConfig:
        if any(binding.target != "header" for binding in self.secret_bindings):
            raise ValueError("streamable HTTP MCP Secret bindings must target header")
        names = [binding.name.lower() for binding in self.secret_bindings]
        if len(names) != len(set(names)):
            raise ValueError("streamable HTTP MCP Secret binding names must be unique")
        static_names = {name.lower() for name in self.headers}
        if any(name in static_names for name in names):
            raise ValueError("MCP Secret header must not duplicate a static header")
        return self


class McpServerConfig(_StrictModel):
    id: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    transport: Literal["stdio", "streamable_http"]
    stdio: StdioTransportConfig | None = None
    streamable_http: StreamableHttpTransportConfig | None = Field(
        default=None,
        alias="streamableHttp",
    )
    expected_server_name: str = Field(
        default="",
        alias="expectedServerName",
        max_length=128,
    )
    execution_mode: McpExecutionMode = Field(
        default="gateway_proxy",
        alias="executionMode",
    )
    auth_mode: McpAuthMode = Field(
        default="none",
        alias="authMode",
    )
    config_version: int = Field(default=1, alias="configVersion", ge=1)
    timeout_seconds: float = Field(
        default=30.0,
        alias="timeoutSeconds",
        ge=1.0,
        le=600.0,
    )
    enabled: bool = True
    deployment_ref: McpDeploymentRef | None = Field(
        default=None,
        alias="deploymentRef",
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_defaults_are_safe(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        # streamable HTTP used to support ``auto`` and ``gateway_proxy``.  It is
        # now browser-direct only.  Coerce persisted/environment legacy values
        # during validation so the v1 -> v2 store migration is lossless instead
        # of making an otherwise valid deployment fail at startup.
        if payload.get("transport") == "streamable_http":
            payload.pop("execution_mode", None)
            payload["executionMode"] = "browser_direct"
        elif "executionMode" not in payload and "execution_mode" not in payload:
            payload["executionMode"] = (
                "gateway_proxy"
            )
        if "authMode" not in payload and "auth_mode" not in payload:
            transport = payload.get("transport")
            transport_config = (
                payload.get("stdio")
                if transport == "stdio"
                else payload.get("streamableHttp", payload.get("streamable_http"))
            )
            bindings = (
                transport_config.get(
                    "secretBindings",
                    transport_config.get("secret_bindings", []),
                )
                if isinstance(transport_config, Mapping)
                else []
            )
            payload["authMode"] = "backend_secret" if bindings else "none"
        return payload

    @field_validator("id", "expected_server_name")
    @classmethod
    def _identifier_is_safe(cls, value: str) -> str:
        if value and not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("MCP server identifier is invalid")
        return value

    @field_validator("display_name")
    @classmethod
    def _display_name_is_safe(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("MCP server displayName is invalid")
        return value

    @model_validator(mode="after")
    def _transport_shape_is_exact(self) -> McpServerConfig:
        if self.transport == "stdio":
            if self.stdio is None or self.streamable_http is not None:
                raise ValueError("stdio MCP server requires only stdio configuration")
        elif self.streamable_http is None or self.stdio is not None:
            raise ValueError(
                "streamable_http MCP server requires only streamableHttp configuration"
            )
        if self.transport == "stdio":
            assert self.stdio is not None
            has_secret_bindings = bool(self.stdio.secret_bindings)
        else:
            assert self.streamable_http is not None
            has_secret_bindings = bool(self.streamable_http.secret_bindings)
        if self.auth_mode == "backend_secret" and not has_secret_bindings:
            raise ValueError(
                "backend_secret MCP authentication requires a Secret binding"
            )
        if self.auth_mode == "none" and has_secret_bindings:
            raise ValueError("MCP Secret bindings require backend_secret authentication")
        if self.transport == "stdio" and self.execution_mode != "gateway_proxy":
            raise ValueError("stdio MCP servers must use gateway_proxy execution")
        if (
            self.transport == "streamable_http"
            and self.execution_mode != "browser_direct"
        ):
            raise ValueError(
                "streamable HTTP MCP servers must use browser_direct execution"
            )
        return self


def validate_browser_mcp_server_config(value: Any) -> McpServerConfig:
    """Validate persisted browser-direct metadata, including explicit HTTP URLs.

    The regular ``McpServerConfig`` entry point remains HTTPS-only so a caller
    cannot accidentally pass an insecure browser URL to the Gateway transport.
    """

    return McpServerConfig.model_validate(
        value,
        context={"allow_browser_http": True},
    )


class McpProxy:
    """Transport-only proxy for explicitly preconfigured MCP servers."""

    def __init__(
        self,
        servers: list[McpServerConfig | dict[str, Any]],
        secret_store: McpSecretResolver,
        *,
        allowed_stdio_paths: Sequence[Path] = (),
        allowed_http_urls: Sequence[str] = (),
    ) -> None:
        parsed_servers = [McpServerConfig.model_validate(item) for item in servers]
        for server in parsed_servers:
            if (
                server.transport == "streamable_http"
                and server.streamable_http is not None
                and urlsplit(server.streamable_http.url).scheme != "https"
            ):
                raise ValueError("gateway MCP proxy requires HTTPS")
        ids = [item.id for item in parsed_servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server ids must be unique")
        self._secret_store = secret_store
        self._allowed_stdio_executables = frozenset(
            self._validate_stdio_executable(item) for item in allowed_stdio_paths
        )
        self._allowed_stdio_roots = tuple(
            sorted({item.parent for item in self._allowed_stdio_executables})
        )
        self._allowed_http_urls = self._validate_http_urls(allowed_http_urls)
        self._servers = {item.id: item for item in parsed_servers}
        for server in parsed_servers:
            self._validate_server_allowlist(server)

    def list_servers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": server.id,
                "displayName": server.display_name,
                "transport": server.transport,
                "enabled": server.enabled,
            }
            for server in sorted(self._servers.values(), key=lambda item: item.id)
        ]

    async def list_tools(self, server_id: str) -> dict[str, Any]:
        server = self._resolve_server(server_id)
        async with self._session(server) as (session, identity, secret_values):
            listed = await session.list_tools()
            tools = self._validated_tools(listed.tools)
            response = {
                "serverId": server.id,
                **identity,
                "tools": [self._tool_to_public(tool) for tool in tools],
            }
            # Tool schemas are transport contracts and must pass through without
            # key-name redaction (for example, a legitimate `apiKey` property).
            # Exact injected Secret values are still removed defensively.
            return _redact_secret_values(response, secret_values)

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        server = self._resolve_server(server_id)
        if not _TOOL_NAME_RE.fullmatch(tool_name):
            raise McpProxyError("MCP_TOOL_NAME_INVALID", "MCP tool name is invalid", 422)
        validated_arguments = _validate_bounded_json(
            arguments,
            label="MCP tool arguments",
            max_characters=_MAX_ARGUMENT_CHARACTERS,
        )
        started = time.perf_counter()
        async with self._session(server) as (session, identity, secret_values):
            listed = await session.list_tools()
            tools = self._validated_tools(listed.tools)
            tool = next((item for item in tools if str(item.name) == tool_name), None)
            if tool is None:
                raise McpProxyError("MCP_TOOL_NOT_FOUND", "MCP tool is not exposed by the server", 404)
            schema = self._input_schema(tool)
            errors = sorted(
                Draft202012Validator(schema).iter_errors(validated_arguments),
                key=lambda item: list(item.absolute_path),
            )
            if errors:
                path = ".".join(str(item) for item in errors[0].absolute_path) or "$"
                raise McpProxyError(
                    "MCP_ARGUMENTS_INVALID",
                    f"MCP tool arguments failed inputSchema validation at {path}",
                    422,
                )
            result = await session.call_tool(tool_name, validated_arguments)
            serialized_result = _model_to_json(result)
            if (
                isinstance(serialized_result, dict)
                and serialized_result.get("_meta") is None
            ):
                serialized_result["_meta"] = {}
            raw_result = _validate_bounded_json(
                serialized_result,
                label="MCP tool result",
                max_characters=_MAX_RESULT_CHARACTERS,
            )
            response = {
                "serverId": server.id,
                **identity,
                "toolName": tool_name,
                "durationMs": max(0, int((time.perf_counter() - started) * 1_000)),
                "result": raw_result,
            }
            return self._sanitize_response(response, secret_values)

    def _resolve_server(self, server_id: str) -> McpServerConfig:
        if not _IDENTIFIER_RE.fullmatch(server_id):
            raise McpProxyError("MCP_SERVER_ID_INVALID", "MCP server id is invalid", 422)
        server = self._servers.get(server_id)
        if server is None:
            raise McpProxyError("MCP_SERVER_NOT_FOUND", "MCP server is not configured", 404)
        if not server.enabled:
            raise McpProxyError("MCP_SERVER_DISABLED", "MCP server is disabled", 503)
        return server

    @asynccontextmanager
    async def _session(
        self,
        server: McpServerConfig,
    ) -> AsyncIterator[tuple[ClientSession, dict[str, str], tuple[str, ...]]]:
        secret_values: tuple[str, ...] = ()
        try:
            async with asyncio.timeout(server.timeout_seconds):
                if server.transport == "stdio":
                    parameters, secret_values = self._stdio_parameters(server)
                    async with stdio_client(parameters) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            initialized = await session.initialize()
                            identity = self._server_identity(server, initialized)
                            yield session, identity, secret_values
                    return

                url, headers, secret_values = self._http_parameters(server)
                assert server.streamable_http is not None
                async with self._create_http_client(server, headers) as http_client:
                    async with streamable_http_client(
                        url,
                        http_client=http_client,
                    ) as (read_stream, write_stream, _get_session_id):
                        async with ClientSession(read_stream, write_stream) as session:
                            initialized = await session.initialize()
                            identity = self._server_identity(server, initialized)
                            yield session, identity, secret_values
        except McpProxyError:
            raise
        except TimeoutError as exc:
            raise McpProxyError("MCP_TIMEOUT", "MCP request timed out", 504) from exc
        except Exception as exc:
            # Never include exception text: transports may echo headers, env,
            # arguments, or upstream response bodies containing injected Secret values.
            raise McpProxyError(
                "MCP_TRANSPORT_ERROR",
                f"MCP transport failed ({type(exc).__name__})",
            ) from exc

    def _stdio_parameters(
        self,
        server: McpServerConfig,
    ) -> tuple[StdioServerParameters, tuple[str, ...]]:
        config = server.stdio
        assert config is not None
        env = {
            name: value
            for name in _SAFE_SUBPROCESS_ENV
            if (value := os.environ.get(name)) is not None
        }
        secret_values: list[str] = []
        for binding in config.secret_bindings:
            value = self._resolve_secret(binding.secret_id)
            secret_values.append(value)
            env[binding.name] = f"{binding.value_prefix}{value}"
        return (
            StdioServerParameters(
                command=str(Path(config.command).resolve(strict=True)),
                args=[
                    str(Path(argument).resolve(strict=True))
                    if Path(argument).is_absolute()
                    else argument
                    for argument in config.args
                ],
                cwd=(
                    str(Path(config.cwd).resolve(strict=True))
                    if config.cwd
                    else str(Path(config.command).resolve(strict=True).parent)
                ),
                env=env,
                encoding="utf-8",
                encoding_error_handler="strict",
            ),
            tuple(secret_values),
        )

    def _http_parameters(
        self,
        server: McpServerConfig,
    ) -> tuple[str, dict[str, str], tuple[str, ...]]:
        config = server.streamable_http
        assert config is not None
        headers = dict(config.headers)
        secret_values: list[str] = []
        for binding in config.secret_bindings:
            value = self._resolve_secret(binding.secret_id)
            secret_values.append(value)
            headers[binding.name] = f"{binding.value_prefix}{value}"
        return config.url, headers, tuple(secret_values)

    def _create_http_client(
        self,
        server: McpServerConfig,
        headers: dict[str, str],
    ) -> httpx.AsyncClient:
        config = server.streamable_http
        assert config is not None
        parsed = urlsplit(config.url)
        assert parsed.hostname is not None
        transport = httpx.AsyncHTTPTransport(trust_env=False)
        transport._pool._network_backend = _ValidatedDnsNetworkBackend(  # type: ignore[attr-defined]
            expected_host=parsed.hostname,
            expected_port=parsed.port or 443,
            allow_private=_normalize_http_url(config.url) in self._allowed_http_urls,
        )
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                server.timeout_seconds,
                read=config.sse_read_timeout_seconds,
            ),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def _resolve_secret(self, secret_id: str) -> str:
        try:
            return self._secret_store.resolve_value(secret_id)
        except (KeyError, ManagedSecretStoreError) as exc:
            raise McpProxyError(
                "MCP_SECRET_UNAVAILABLE",
                "Configured MCP Secret is unavailable",
                503,
            ) from exc

    def _server_identity(self, server: McpServerConfig, initialized: Any) -> dict[str, str]:
        server_info = getattr(initialized, "serverInfo", None)
        name = str(getattr(server_info, "name", "") or "")
        version = str(getattr(server_info, "version", "") or "")
        if server.expected_server_name and name != server.expected_server_name:
            raise McpProxyError(
                "MCP_SERVER_IDENTITY_MISMATCH",
                "MCP server identity does not match its preconfigured identity",
            )
        if len(name) > 128 or len(version) > 128:
            raise McpProxyError("MCP_SERVER_IDENTITY_INVALID", "MCP server identity is invalid")
        return {"serverName": name, "serverVersion": version}

    def _validated_tools(self, tools: Sequence[Any]) -> list[Any]:
        if len(tools) > 256:
            raise McpProxyError("MCP_TOOL_LIST_TOO_LARGE", "MCP tool list exceeds the limit")
        names: list[str] = []
        validated: list[Any] = []
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            description = str(getattr(tool, "description", "") or "")
            if not _TOOL_NAME_RE.fullmatch(name) or len(description) > 8_000:
                raise McpProxyError("MCP_TOOL_SCHEMA_INVALID", "MCP tool metadata is invalid")
            self._input_schema(tool)
            output_schema = getattr(tool, "outputSchema", None)
            if output_schema is not None:
                self._validate_schema(output_schema, "outputSchema")
            names.append(name)
            validated.append(tool)
        if len(names) != len(set(names)):
            raise McpProxyError("MCP_TOOL_SCHEMA_INVALID", "MCP tool names must be unique")
        return validated

    def _input_schema(self, tool: Any) -> dict[str, Any]:
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict):
            raise McpProxyError("MCP_TOOL_SCHEMA_INVALID", "MCP tool inputSchema is invalid")
        return self._validate_schema(schema, "inputSchema")

    @staticmethod
    def _validate_schema(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise McpProxyError("MCP_TOOL_SCHEMA_INVALID", f"MCP tool {label} is invalid")
        schema = _validate_bounded_json(
            value,
            label=f"MCP tool {label}",
            max_characters=_MAX_SCHEMA_CHARACTERS,
        )
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise McpProxyError(
                "MCP_TOOL_SCHEMA_INVALID",
                f"MCP tool {label} is not valid Draft 2020-12 JSON Schema",
            ) from exc
        return schema

    def _tool_to_public(self, tool: Any) -> dict[str, Any]:
        result = {
            "name": str(tool.name),
            "description": str(getattr(tool, "description", "") or ""),
            "inputSchema": self._input_schema(tool),
        }
        output_schema = getattr(tool, "outputSchema", None)
        if output_schema is not None:
            result["outputSchema"] = self._validate_schema(output_schema, "outputSchema")
        return result

    def _validate_server_allowlist(self, server: McpServerConfig) -> None:
        if server.transport == "stdio":
            if not self._allowed_stdio_executables:
                raise ValueError("stdio MCP servers require an absolute path allowlist")
            config = server.stdio
            assert config is not None
            command = Path(config.command)
            if not command.is_absolute() or not command.is_file():
                raise ValueError("stdio MCP command must be an existing absolute file")
            self._require_allowed_executable(command)
            cwd = Path(config.cwd) if config.cwd else command.parent
            if not cwd.is_absolute() or not cwd.is_dir():
                raise ValueError("stdio MCP cwd must be an existing absolute directory")
            self._require_allowed_path(cwd)
            for argument in config.args:
                candidate = Path(argument)
                if candidate.is_absolute():
                    if not candidate.exists():
                        raise ValueError("absolute stdio MCP path argument must exist")
                    self._require_allowed_path(candidate)
            return

        config = server.streamable_http
        assert config is not None
        parsed = urlsplit(config.url)
        assert parsed.hostname is not None
        addresses = _resolve_host_addresses_sync(
            _normalize_dns_host(parsed.hostname),
            parsed.port or 443,
        )
        if _normalize_http_url(config.url) not in self._allowed_http_urls:
            _require_global_addresses(addresses)

    def _require_allowed_path(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if not any(_is_relative_to(resolved, root) for root in self._allowed_stdio_roots):
            raise ValueError("stdio MCP path is not allowlisted")

    def _require_allowed_executable(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if resolved not in self._allowed_stdio_executables:
            raise ValueError("stdio MCP executable is not explicitly allowlisted")

    @staticmethod
    def _validate_stdio_executable(path: Path) -> Path:
        value = Path(path)
        if not value.is_absolute() or not value.is_file():
            raise ValueError("stdio MCP allowlist entries must be existing absolute files")
        return value.resolve(strict=True)

    @staticmethod
    def _validate_http_urls(values: Sequence[str]) -> frozenset[str]:
        result: set[str] = set()
        for value in values:
            try:
                StreamableHttpTransportConfig.model_validate({"url": value})
                normalized = _normalize_http_url(value)
            except ValueError as exc:
                raise ValueError(
                    "streamable HTTP MCP exact URL allowlist entry is invalid"
                ) from exc
            if normalized in result:
                raise ValueError(
                    "streamable HTTP MCP exact URL allowlist entries must be unique"
                )
            result.add(normalized)
        return frozenset(result)

    @staticmethod
    def _sanitize_response(value: Any, secret_values: Sequence[str]) -> Any:
        return _redact_secret_values(redact_value(value), secret_values)


def _normalize_http_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path or "/"
    return urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


def _normalize_dns_host(value: str) -> str:
    candidate = value.rstrip(".")
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        try:
            return candidate.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("MCP host name is invalid") from exc


def _resolved_addresses(records: Sequence[Any]) -> tuple[_IpAddress, ...]:
    addresses: list[_IpAddress] = []
    seen: set[_IpAddress] = set()
    for record in records:
        raw = str(record[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError("MCP DNS resolution returned an invalid address") from exc
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise ValueError("MCP DNS resolution returned no addresses")
    return tuple(addresses)


def _resolve_host_addresses_sync(host: str, port: int) -> tuple[_IpAddress, ...]:
    try:
        records = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("MCP host DNS resolution failed") from exc
    return _resolved_addresses(records)


async def _resolve_host_addresses_async(host: str, port: int) -> tuple[_IpAddress, ...]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise OSError("MCP host DNS resolution failed") from exc
    return _resolved_addresses(records)


def _require_global_addresses(addresses: Sequence[_IpAddress]) -> None:
    for address in addresses:
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or (mapped is not None and not mapped.is_global)
        ):
            raise ValueError("public MCP host resolved to a non-global address")


def _validate_bounded_json(
    value: Any,
    *,
    label: str,
    max_characters: int,
) -> Any:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise McpProxyError("MCP_JSON_INVALID", f"{label} exceeds the JSON node limit", 422)
        if depth > _MAX_JSON_DEPTH:
            raise McpProxyError("MCP_JSON_INVALID", f"{label} exceeds the JSON depth limit", 422)
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if current != current or current in {float("inf"), float("-inf")}:
                raise McpProxyError("MCP_JSON_INVALID", f"{label} contains a non-finite number", 422)
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 1_024:
                    raise McpProxyError("MCP_JSON_INVALID", f"{label} contains an invalid key", 422)
                stack.append((item, depth + 1))
            continue
        raise McpProxyError("MCP_JSON_INVALID", f"{label} must contain JSON values only", 422)
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise McpProxyError("MCP_JSON_INVALID", f"{label} is not JSON serializable", 422) from exc
    if len(serialized) > max_characters:
        raise McpProxyError("MCP_JSON_TOO_LARGE", f"{label} exceeds the size limit", 413)
    return value


def _model_to_json(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _model_to_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_model_to_json(item) for item in value]
    if isinstance(value, list):
        return [_model_to_json(item) for item in value]
    return value


def _redact_secret_values(value: Any, secret_values: Sequence[str]) -> Any:
    filtered = tuple(item for item in secret_values if item)
    if isinstance(value, Mapping):
        return {
            key: _redact_secret_values(item, filtered)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_secret_values(item, filtered) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_secret_values(item, filtered) for item in value]
    if isinstance(value, str):
        result = value
        for secret in filtered:
            result = result.replace(secret, "[REDACTED]")
        return result
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "McpAuthMode",
    "McpExecutionMode",
    "McpProxy",
    "McpProxyError",
    "McpSecretBinding",
    "McpSecretResolver",
    "McpServerConfig",
    "StdioTransportConfig",
    "StreamableHttpTransportConfig",
]
