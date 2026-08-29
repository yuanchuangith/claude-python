from __future__ import annotations

import secrets
import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .mcp_proxy import (
    McpServerConfig,
    StdioTransportConfig,
    validate_browser_mcp_server_config,
)
from .mcp_server_store import McpServerCandidate, McpServerStore
from .secret_store import (
    ManagedSecretRollbackSnapshot,
    ManagedSecretStore,
    ManagedSecretStoreError,
    SecretUpsertRequest,
)

MCP_BROWSER_PROTOCOL_VERSION = "2025-06-18"
_RESERVED_HEADERS = {
    "accept",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
}
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SENSITIVE_HEADER_RE = re.compile(
    r"(?:authorization|cookie|api[-_]?key|secret|password|token|credential)",
    re.IGNORECASE,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


class BrowserMcpSecretBindingWrite(_StrictModel):
    secret_id: str = Field(alias="secretId", min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    value_prefix: str = Field(default="", alias="valuePrefix", max_length=64)
    value: str = Field(min_length=8, max_length=8192)

    @field_validator("name")
    @classmethod
    def _header_is_not_reserved(cls, value: str) -> str:
        if value.lower() in _RESERVED_HEADERS:
            raise ValueError("MCP protocol headers cannot be configured")
        return value


class BrowserMcpStreamableHttpWrite(_StrictModel):
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] | None = Field(default=None, max_length=64)
    static_headers: dict[str, str] = Field(
        default_factory=dict,
        alias="staticHeaders",
        max_length=64,
    )
    secret_bindings: list[BrowserMcpSecretBindingWrite] = Field(
        default_factory=list,
        alias="secretBindings",
        max_length=32,
    )
    sse_read_timeout_seconds: float = Field(
        default=300.0,
        alias="sseReadTimeoutSeconds",
        ge=1.0,
        le=3600.0,
    )

    @model_validator(mode="after")
    def _headers_are_unique_and_not_reserved(self) -> BrowserMcpStreamableHttpWrite:
        if self.headers is not None and (self.static_headers or self.secret_bindings):
            raise ValueError(
                "use final headers or legacy staticHeaders/secretBindings, not both"
            )
        names: set[str] = set()
        configured_headers = (
            self.headers if self.headers is not None else self.static_headers
        )
        for name, value in configured_headers.items():
            lowered = name.lower()
            if (
                not _HEADER_NAME_RE.fullmatch(name)
                or lowered in _RESERVED_HEADERS
                or len(value) > 8192
                or any(character in value for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError("MCP protocol headers cannot be configured")
            if lowered in names:
                raise ValueError("MCP Header names must be unique ignoring case")
            names.add(lowered)
        secret_values: dict[str, str] = {}
        for binding in self.secret_bindings:
            lowered = binding.name.lower()
            if lowered in names:
                raise ValueError("MCP Header names must be unique ignoring case")
            names.add(lowered)
            previous = secret_values.get(binding.secret_id)
            if previous is not None and not secrets.compare_digest(previous, binding.value):
                raise ValueError("one Secret id cannot carry different values")
            secret_values[binding.secret_id] = binding.value
        return self


class BrowserMcpServerWriteRequest(_StrictModel):
    id: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    transport: Literal["streamable_http"]
    expected_config_version: int = Field(alias="expectedConfigVersion", ge=0)
    expected_deployment_version: int | None = Field(
        default=None,
        alias="expectedDeploymentVersion",
        ge=1,
    )
    expected_server_name: str = Field(
        default="",
        alias="expectedServerName",
        max_length=128,
    )
    timeout_seconds: float = Field(
        default=30.0,
        alias="timeoutSeconds",
        ge=1.0,
        le=600.0,
    )
    enabled: bool = True
    streamable_http: BrowserMcpStreamableHttpWrite = Field(alias="streamableHttp")


class BrowserMcpEnabledRequest(_StrictModel):
    enabled: bool
    expected_config_version: int = Field(alias="expectedConfigVersion", ge=1)
    expected_deployment_version: int | None = Field(
        default=None,
        alias="expectedDeploymentVersion",
        ge=1,
    )


class BrowserMcpDeleteRequest(_StrictModel):
    expected_config_version: int = Field(alias="expectedConfigVersion", ge=1)
    expected_deployment_version: int | None = Field(
        default=None,
        alias="expectedDeploymentVersion",
        ge=1,
    )


class AdminMcpStdioServerWriteRequest(_StrictModel):
    """Administrator-only stdio write shape.

    Keeping this separate from the anonymous HTTP shape prevents command, cwd,
    environment bindings, or executable paths from ever entering the public
    browser configuration mutation surface.
    """

    id: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    transport: Literal["stdio"]
    expected_config_version: int = Field(alias="expectedConfigVersion", ge=0)
    expected_server_name: str = Field(
        default="",
        alias="expectedServerName",
        max_length=128,
    )
    timeout_seconds: float = Field(
        default=30.0,
        alias="timeoutSeconds",
        ge=1.0,
        le=600.0,
    )
    enabled: bool = True
    stdio: StdioTransportConfig


class BrowserMcpConfigurationTestResponse(_StrictModel):
    id: str
    status: Literal["valid", "invalid"]
    config_version: int = Field(alias="configVersion", ge=1)
    error_code: str = Field(default="", alias="errorCode", max_length=128)
    checks: list[str] = Field(default_factory=list, max_length=32)


class BrowserMcpConfigurationService:
    """One projection and mutation service over the authoritative MCP stores."""

    def __init__(
        self,
        server_store: McpServerStore,
        secret_store: ManagedSecretStore,
    ) -> None:
        self.server_store = server_store
        self.secret_store = secret_store

    def list_servers(self) -> list[dict[str, Any]]:
        return [self._project(server) for server in self.server_store.list_configs()]

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        server = self.server_store.get_config(server_id)
        return self._project(server) if server is not None else None

    def configuration_hash(self, server_id: str) -> str:
        """Hash an effective configuration without hashing plaintext Secret values."""

        server = self.server_store.get_config(server_id)
        if server is None:
            raise KeyError(server_id)
        payload = server.model_dump(mode="json", by_alias=True)
        canonical = json.dumps(
            {
                "server": payload,
                "effectiveVersion": self.server_store.effective_version(server_id),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def upsert_server(
        self,
        request: BrowserMcpServerWriteRequest | dict[str, Any],
    ) -> dict[str, Any]:
        payload = BrowserMcpServerWriteRequest.model_validate(request)
        existing = self.server_store.get_config(payload.id)
        deployment_ref = existing.deployment_ref if existing is not None else None
        if deployment_ref is not None:
            if existing.transport != "streamable_http" or existing.streamable_http is None:
                raise PermissionError("deployment-managed transport cannot be changed")
            if payload.streamable_http.url != existing.streamable_http.url:
                raise PermissionError("deployment-managed URL cannot be changed")
        transport = payload.streamable_http
        static_headers, secret_bindings = self._storage_headers(
            payload.id,
            transport,
        )
        secret_values = {
            binding.secret_id: binding.value for binding in secret_bindings
        }
        changed_values: dict[str, str] = {}
        for secret_id, value in secret_values.items():
            current_metadata = self.secret_store.get(secret_id)
            if (
                current_metadata is not None
                and current_metadata.get("exposure") != "browser_header"
            ):
                raise PermissionError(
                    "deployment-only or stdio Secret cannot become a browser Header"
                )
            try:
                current = self.secret_store.resolve_value(secret_id)
            except (KeyError, ManagedSecretStoreError):
                changed_values[secret_id] = value
            else:
                if not secrets.compare_digest(current, value):
                    changed_values[secret_id] = value

        server = validate_browser_mcp_server_config(
            {
                "id": payload.id,
                "displayName": payload.display_name,
                "transport": "streamable_http",
                "executionMode": "browser_direct",
                "authMode": "backend_secret" if secret_bindings else "none",
                "expectedServerName": payload.expected_server_name,
                "timeoutSeconds": payload.timeout_seconds,
                "enabled": payload.enabled,
                "streamableHttp": {
                    "url": transport.url,
                    "headers": static_headers,
                    "secretBindings": [
                        {
                            "secretId": binding.secret_id,
                            "target": "header",
                            "name": binding.name,
                            "valuePrefix": binding.value_prefix,
                        }
                        for binding in secret_bindings
                    ],
                    "sseReadTimeoutSeconds": transport.sse_read_timeout_seconds,
                },
                "deploymentRef": deployment_ref,
            }
        )
        candidate = self.server_store.create_upsert_candidate(
            server,
            expected_config_version=payload.expected_config_version,
            changed_secret_ids=changed_values,
        )
        snapshots = self._snapshots(changed_values)
        try:
            for secret_id, value in changed_values.items():
                current = self.secret_store.get(secret_id)
                self.secret_store.upsert(
                    SecretUpsertRequest.model_validate(
                        {
                            "id": secret_id,
                            "kind": current["kind"] if current else "mcp_key",
                            "displayName": (
                                current["displayName"]
                                if current
                                else f"MCP {payload.id} {secret_id}"
                            ),
                            "value": value,
                            "exposure": "browser_header",
                        }
                    )
                )
            self.server_store.commit_metadata_candidate(candidate)
        except Exception:
            self._restore_snapshots(snapshots)
            raise
        result = self.get_server(payload.id)
        assert result is not None
        return result

    def set_enabled(
        self,
        server_id: str,
        request: BrowserMcpEnabledRequest | dict[str, Any],
    ) -> dict[str, Any]:
        payload = BrowserMcpEnabledRequest.model_validate(request)
        self._require_managed_http(server_id)
        candidate = self.server_store.create_enabled_candidate(
            server_id,
            payload.enabled,
            expected_config_version=payload.expected_config_version,
        )
        self.server_store.commit_metadata_candidate(candidate)
        result = self.get_server(server_id)
        assert result is not None
        return result

    def delete_server(
        self,
        server_id: str,
        request: BrowserMcpDeleteRequest | dict[str, Any],
    ) -> dict[str, Any]:
        payload = BrowserMcpDeleteRequest.model_validate(request)
        self._require_managed_http(server_id)
        candidate = self.server_store.create_delete_candidate(
            server_id,
            expected_config_version=payload.expected_config_version,
        )
        self.server_store.commit_metadata_candidate(candidate)
        return {"id": server_id, "deleted": True}

    def test_configuration(self, server_id: str) -> dict[str, Any]:
        projected = self.get_server(server_id)
        if projected is None:
            raise KeyError(server_id)
        valid = projected["configStatus"] == "ready"
        response = BrowserMcpConfigurationTestResponse.model_validate(
            {
                "id": server_id,
                "status": "valid" if valid else "invalid",
                "configVersion": projected["configVersion"],
                "errorCode": "" if valid else projected["errorCode"],
                "checks": [
                    "strict_schema",
                    "http_url",
                    "header_names",
                    "secret_bindings",
                    "config_version",
                ],
            }
        )
        return response.model_dump(mode="json", by_alias=True)

    @staticmethod
    def _storage_headers(
        server_id: str,
        transport: BrowserMcpStreamableHttpWrite,
    ) -> tuple[dict[str, str], list[BrowserMcpSecretBindingWrite]]:
        if transport.headers is None:
            return dict(transport.static_headers), list(transport.secret_bindings)
        static_headers: dict[str, str] = {}
        secret_bindings: list[BrowserMcpSecretBindingWrite] = []
        for name, value in transport.headers.items():
            if not _SENSITIVE_HEADER_RE.search(name):
                static_headers[name] = value
                continue
            digest = hashlib.sha256(
                f"{server_id}\0{name.lower()}".encode("utf-8")
            ).hexdigest()[:32]
            secret_bindings.append(
                BrowserMcpSecretBindingWrite.model_validate(
                    {
                        "secretId": f"browser.{digest}",
                        "name": name,
                        "valuePrefix": "",
                        "value": value,
                    }
                )
            )
        return static_headers, secret_bindings

    def create_stdio_candidate(
        self,
        request: AdminMcpStdioServerWriteRequest | dict[str, Any],
    ) -> McpServerCandidate:
        payload = AdminMcpStdioServerWriteRequest.model_validate(request)
        server = McpServerConfig.model_validate(
            {
                "id": payload.id,
                "displayName": payload.display_name,
                "transport": "stdio",
                "executionMode": "gateway_proxy",
                "authMode": (
                    "backend_secret" if payload.stdio.secret_bindings else "none"
                ),
                "expectedServerName": payload.expected_server_name,
                "timeoutSeconds": payload.timeout_seconds,
                "enabled": payload.enabled,
                "stdio": payload.stdio,
            }
        )
        return self.server_store.create_upsert_candidate(
            server,
            expected_config_version=payload.expected_config_version,
        )

    def create_enabled_candidate(
        self,
        server_id: str,
        enabled: bool,
        *,
        expected_config_version: int,
    ) -> McpServerCandidate:
        return self.server_store.create_enabled_candidate(
            server_id,
            enabled,
            expected_config_version=expected_config_version,
        )

    def create_delete_candidate(
        self,
        server_id: str,
        *,
        expected_config_version: int,
    ) -> McpServerCandidate:
        return self.server_store.create_delete_candidate(
            server_id,
            expected_config_version=expected_config_version,
        )

    def create_secret_candidate(
        self,
        secret_ids: Collection[str],
    ) -> McpServerCandidate:
        return self.server_store.create_secret_candidate(secret_ids)

    @staticmethod
    def candidate_requires_stdio_runtime(candidate: McpServerCandidate) -> bool:
        return any(server.transport == "stdio" for server in candidate.configs)

    def commit_metadata_candidate(self, candidate: McpServerCandidate) -> None:
        """Commit configuration/version metadata without constructing an MCP runtime."""

        self.server_store.commit_metadata_candidate(candidate)

    def commit_runtime_candidate(
        self,
        candidate: McpServerCandidate,
        runtime: Any,
        publish_runtime: Callable[[Any], Any | None],
        *,
        connection_status: str = "disconnected",
    ) -> None:
        self.server_store.commit_candidate(
            candidate,
            runtime,
            publish_runtime,
            connection_status=connection_status,  # type: ignore[arg-type]
        )

    def _require_managed_http(self, server_id: str) -> McpServerConfig:
        source = self.server_store.source_for(server_id)
        if source == "environment":
            raise PermissionError("environment MCP Servers are read-only")
        server = self.server_store.get_config(server_id)
        if server is None:
            raise KeyError(server_id)
        if server.transport != "streamable_http":
            raise PermissionError("stdio MCP Servers are administrator-managed")
        return server

    def _project(self, server: McpServerConfig) -> dict[str, Any]:
        source = self.server_store.source_for(server.id)
        assert source is not None
        version = self.server_store.effective_version(server.id)
        common: dict[str, Any] = {
            "id": server.id,
            "displayName": server.display_name,
            "source": source,
            "editable": source == "managed" and server.transport == "streamable_http",
            "enabled": server.enabled,
            "transport": server.transport,
            "executionMode": (
                "browser_direct"
                if server.transport == "streamable_http"
                else "gateway_proxy"
            ),
            "browserSupported": server.transport == "streamable_http",
            "expectedServerName": server.expected_server_name,
            "timeoutSeconds": server.timeout_seconds,
            "configVersion": version,
            "configStatus": "ready",
            "errorCode": "",
            "streamableHttp": None,
            "managedBy": "deployment" if server.deployment_ref else "configuration",
            "deploymentRef": (
                server.deployment_ref.model_dump(mode="json", by_alias=True)
                if server.deployment_ref
                else None
            ),
        }
        if server.transport == "stdio":
            return common
        assert server.streamable_http is not None
        try:
            headers = dict(server.streamable_http.headers)
            normalized = {name.lower() for name in headers}
            bindings: list[dict[str, str]] = []
            for binding in server.streamable_http.secret_bindings:
                lowered = binding.name.lower()
                if lowered in normalized or lowered in _RESERVED_HEADERS:
                    raise ValueError("MCP Header names are invalid")
                self.secret_store.require_exposure(
                    binding.secret_id,
                    {"browser_header"},
                )
                value = self.secret_store.resolve_value(binding.secret_id)
                normalized.add(lowered)
                headers[binding.name] = f"{binding.value_prefix}{value}"
                bindings.append(
                    {
                        "secretId": binding.secret_id,
                        "name": binding.name,
                        "valuePrefix": binding.value_prefix,
                        "value": value,
                    }
                )
            common["streamableHttp"] = {
                "url": server.streamable_http.url,
                "headers": headers,
                "staticHeaders": dict(server.streamable_http.headers),
                "secretBindings": bindings,
                "protocolVersion": MCP_BROWSER_PROTOCOL_VERSION,
                "sseReadTimeoutSeconds": (
                    server.streamable_http.sse_read_timeout_seconds
                ),
            }
        except (KeyError, ManagedSecretStoreError):
            common["configStatus"] = "invalid"
            common["errorCode"] = "MCP_SECRET_UNAVAILABLE"
        except ValueError:
            common["configStatus"] = "invalid"
            common["errorCode"] = "MCP_BROWSER_CONFIG_INVALID"
        return common

    def _snapshots(
        self,
        values: dict[str, str],
    ) -> list[ManagedSecretRollbackSnapshot]:
        return [
            self.secret_store.snapshot_for_rollback(secret_id)
            for secret_id in sorted(values)
        ]

    def _restore_snapshots(
        self,
        snapshots: Iterable[ManagedSecretRollbackSnapshot],
    ) -> None:
        rollback_error: Exception | None = None
        for snapshot in reversed(list(snapshots)):
            try:
                self.secret_store.restore_snapshot(snapshot)
            except Exception as exc:  # pragma: no cover - catastrophic backend failure
                rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise ManagedSecretStoreError(
                "MCP browser configuration rollback failed"
            ) from rollback_error


__all__ = [
    "AdminMcpStdioServerWriteRequest",
    "BrowserMcpConfigurationService",
    "BrowserMcpConfigurationTestResponse",
    "BrowserMcpDeleteRequest",
    "BrowserMcpEnabledRequest",
    "BrowserMcpSecretBindingWrite",
    "BrowserMcpServerWriteRequest",
    "BrowserMcpStreamableHttpWrite",
    "MCP_BROWSER_PROTOCOL_VERSION",
]
