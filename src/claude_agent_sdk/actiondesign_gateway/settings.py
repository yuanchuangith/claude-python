"""Environment-backed settings for the thin ActionDesign gateway."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return default if not value else float(value)


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if not value else int(value)


def _csv(name: str, default: list[str] | None = None) -> list[str]:
    value = os.environ.get(name)
    if not value:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def _json_list(name: str) -> list[dict[str, Any]]:
    value = os.environ.get(name, "").strip()
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError(f"{name} must contain a JSON array")
    return payload


def _claude_code_api_key() -> str:
    return _env("CLAUDE_CODE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _claude_code_auth_mode() -> str:
    explicit = _env("CLAUDE_CODE_AUTH_MODE")
    if explicit:
        return explicit
    if os.environ.get("ANTHROPIC_AUTH_TOKEN") and not _env(
        "CLAUDE_CODE_API_KEY", "ANTHROPIC_API_KEY"
    ):
        return "bearer"
    return "api-key"


def _default_provider() -> str:
    explicit = _env("ACTIONDESIGN_AGENT_DEFAULT_PROVIDER")
    if explicit:
        return explicit
    return "claude-code" if _claude_code_api_key() else "mimo"


class McpSecretBinding(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    target: Literal["header", "env"]
    name: str = Field(min_length=1, max_length=128)
    prefix: str = Field(default="", max_length=128)

    @field_validator("name")
    def _validate_name(cls, value: str, info: Any) -> str:
        pattern = (
            r"[A-Z_][A-Z0-9_]{0,127}"
            if info.data.get("target") == "env"
            else r"[A-Za-z][A-Za-z0-9_-]{0,127}"
        )
        if not re.fullmatch(pattern, value):
            raise ValueError("MCP Secret binding name is invalid")
        return value


class McpConfiguredSecretBinding(McpSecretBinding):
    secret_id: str = Field(alias="secretId", min_length=1, max_length=128)

    @field_validator("secret_id")
    def _validate_secret_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("MCP secretId is invalid")
        return value


class McpServerSettings(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    id: str
    enabled: bool = True
    transport: Literal["stdio", "streamable_http"]
    url: str = ""
    executable_path: str = Field(default="", alias="executablePath", max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=64)
    secret_id: str = Field(default="", alias="secretId", max_length=128)
    secret_binding: McpSecretBinding | None = Field(
        default=None,
        alias="secretBinding",
    )
    secret_bindings: list[McpConfiguredSecretBinding] = Field(
        default_factory=list,
        alias="secretBindings",
        max_length=32,
    )

    @field_validator("id")
    def _validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("MCP server id is invalid")
        return value

    @field_validator("secret_id")
    def _validate_secret_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("MCP secretId is invalid")
        return value

    @field_validator("args")
    def _validate_args(cls, value: list[str]) -> list[str]:
        if any(
            not item
            or len(item) > 2048
            or any(character in item for character in ("\x00", "\r", "\n"))
            or Path(item).is_absolute()
            for item in value
        ):
            raise ValueError(
                "MCP stdio args are invalid; executable scripts must use an allowlisted wrapper"
            )
        return value

    @model_validator(mode="after")
    def _validate_transport_shape(self) -> McpServerSettings:
        if self.transport == "streamable_http":
            if not self.url or self.executable_path or self.args:
                raise ValueError("HTTP MCP requires only url transport configuration")
            parsed = urlsplit(self.url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("MCP url must be an absolute HTTPS URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("MCP url must not contain credentials, query, or fragment")
        else:
            if self.url or not self.executable_path:
                raise ValueError("stdio MCP requires executablePath and no url")
            if not Path(self.executable_path).is_absolute():
                raise ValueError("MCP executablePath must be absolute")
        if bool(self.secret_id) != bool(self.secret_binding):
            raise ValueError("MCP secretId and secretBinding must be configured together")
        if self.secret_bindings and self.secret_binding:
            raise ValueError("use secretBindings or legacy secretId/secretBinding, not both")
        bindings = list(self.secret_bindings)
        if self.secret_binding:
            bindings.append(
                McpConfiguredSecretBinding(
                    secretId=self.secret_id,
                    target=self.secret_binding.target,
                    name=self.secret_binding.name,
                    prefix=self.secret_binding.prefix,
                )
            )
        if self.transport == "streamable_http" and any(
            binding.target != "header" for binding in bindings
        ):
            raise ValueError("HTTP MCP Secrets must use header bindings")
        if self.transport == "stdio" and any(
            binding.target != "env" for binding in bindings
        ):
            raise ValueError("stdio MCP Secrets must use env bindings")
        names = [binding.name.lower() for binding in bindings]
        if len(names) != len(set(names)):
            raise ValueError("MCP Secret binding names must be unique")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )

    default_provider: str = Field(
        default_factory=_default_provider
    )
    allow_origins: list[str] = Field(
        default_factory=lambda: _csv("ACTIONDESIGN_AGENT_ALLOW_ORIGINS", ["*"])
    )
    allow_private_network: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_AGENT_ALLOW_PRIVATE_NETWORK", False)
    )
    local_default_client_api_key: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_LOCAL_DEFAULT_CLIENT_API_KEY"),
        repr=False,
    )
    local_default_client_origin: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_LOCAL_DEFAULT_CLIENT_ORIGIN")
    )
    service_auth_mode: Literal["disabled", "required"] = Field(
        default_factory=lambda: _env(
            "ACTIONDESIGN_SERVICE_AUTH_MODE",
            default="disabled",
        )
    )
    service_auth_token: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_SERVICE_AUTH_TOKEN")
    )
    localdoc_base_url: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_LOCALDOC_BASE_URL")
    )
    localdoc_knowledge_base_id: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_LOCALDOC_KNOWLEDGE_BASE_ID")
    )

    @field_validator("localdoc_knowledge_base_id")
    def _canonical_localdoc_id(cls, value: str) -> str:
        from uuid import UUID

        return str(UUID(value)) if value else value

    @model_validator(mode="after")
    def _validate_localdoc(self) -> Settings:
        if bool(self.localdoc_base_url) != bool(self.localdoc_knowledge_base_id):
            raise ValueError("LocalDoc URL and knowledge base ID must be configured together")
        if self.localdoc_base_url:
            parsed = urlsplit(self.localdoc_base_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("LocalDoc URL must be a loopback HTTP origin")
            # Accessing port also rejects malformed/out-of-range ports.
            _ = parsed.port
        return self

    log_root: Path | None = Field(
        default_factory=lambda: Path(_env("ACTIONDESIGN_AGENT_LOG_ROOT", default="debug_logs/actiondesign-agent"))
    )
    full_conversation_log_enabled: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_FULL_CONVERSATION_LOG", True)
    )
    full_conversation_log_root: Path = Field(
        default_factory=lambda: Path(_env("ACTIONDESIGN_FULL_CONVERSATION_LOG_ROOT", default="logs/actiondesign-agent"))
    )
    full_conversation_log_max_bytes: int = Field(
        default_factory=lambda: _int(
            "ACTIONDESIGN_FULL_CONVERSATION_LOG_MAX_BYTES",
            100 * 1024 * 1024,
        ),
        ge=1024 * 1024,
        le=1024 * 1024 * 1024,
    )
    system_log_enabled: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_SYSTEM_LOG_ENABLED", False)
    )
    system_log_path: Path = Field(
        default_factory=lambda: Path(_env(
            "ACTIONDESIGN_SYSTEM_LOG_PATH", default="logs/local/system-logs.sqlite3"
        ))
    )
    request_log_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_REQUEST_LOG_PATH",
                default="logs/actiondesign-agent/request-logs.jsonl",
            )
        )
    )
    request_log_max_bytes: int = Field(
        default_factory=lambda: _int(
            "ACTIONDESIGN_REQUEST_LOG_MAX_BYTES",
            10 * 1024 * 1024,
        ),
        ge=64 * 1024,
        le=1024 * 1024 * 1024,
    )
    request_log_backup_count: int = Field(
        default_factory=lambda: _int("ACTIONDESIGN_REQUEST_LOG_BACKUP_COUNT", 5),
        ge=1,
        le=100,
    )

    mimo_api_key: str = Field(default_factory=lambda: _env("GXP_MIMO_API_KEY", "MODEL_MIMO_KEY"))
    mimo_messages_url: str = Field(
        default_factory=lambda: _env(
            "GXP_MIMO_MESSAGES_URL",
            "MODEL_MIMO_URL",
            default="https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages",
        )
    )
    mimo_default_model: str = Field(default_factory=lambda: _env("GXP_MIMO_DEFAULT_MODEL", default="mimo-v2.5"))
    mimo_auth_mode: str = Field(default_factory=lambda: _env("GXP_MIMO_AUTH_MODE", default="api-key"))
    mimo_timeout_seconds: float = Field(default_factory=lambda: _float("GXP_MIMO_TIMEOUT_SECONDS", 120.0), ge=1.0, le=600.0)

    claude_code_api_key: str = Field(default_factory=_claude_code_api_key)
    claude_code_messages_url: str = Field(
        default_factory=lambda: _env(
            "CLAUDE_CODE_MESSAGES_URL",
            "ANTHROPIC_BASE_URL",
            default="https://api.anthropic.com/v1/messages",
        )
    )
    claude_code_default_model: str = Field(
        default_factory=lambda: _env(
            "CLAUDE_CODE_DEFAULT_MODEL",
            "ANTHROPIC_MODEL",
            default="claude-sonnet-4-5",
        )
    )
    claude_code_models: list[str] = Field(default_factory=lambda: _csv("CLAUDE_CODE_MODELS", []))
    claude_code_auth_mode: str = Field(default_factory=_claude_code_auth_mode)
    claude_code_timeout_seconds: float = Field(default_factory=lambda: _float("CLAUDE_CODE_TIMEOUT_SECONDS", 120.0), ge=1.0, le=600.0)

    profile_store_path: Path = Field(
        default_factory=lambda: Path(_env("ACTIONDESIGN_PROFILE_STORE_PATH", default="config/actiondesign-api-profiles.json"))
    )
    profile_secret_store_backend: Literal["keyring", "file"] = Field(
        default_factory=lambda: _env(
            "ACTIONDESIGN_PROFILE_SECRET_STORE_BACKEND",
            default="keyring",
        )
    )
    profile_secret_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_PROFILE_SECRET_STORE_PATH",
                default="config/actiondesign-api-profile-secrets.json",
            )
        )
    )
    managed_secret_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_MANAGED_SECRET_STORE_PATH",
                default="config/actiondesign-managed-secrets.json",
            )
        )
    )
    mcp_server_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_MCP_SERVER_STORE_PATH",
                default="config/actiondesign-managed-mcp-servers.json",
            )
        )
    )
    profile_admin_token: str = Field(default_factory=lambda: _env("ACTIONDESIGN_PROFILE_ADMIN_TOKEN"))
    admin_password: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_ADMIN_PASSWORD")
    )
    admin_session_ttl_seconds: int = Field(
        default_factory=lambda: _int("ACTIONDESIGN_ADMIN_SESSION_TTL_SECONDS", 3600),
        ge=300,
        le=86_400,
    )
    admin_allow_remote: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_ADMIN_ALLOW_REMOTE", False)
    )
    admin_require_https_remote: bool = Field(
        default_factory=lambda: _bool(
            "ACTIONDESIGN_ADMIN_REQUIRE_HTTPS_REMOTE",
            True,
        )
    )
    browser_api_key_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_BROWSER_API_KEY_STORE_PATH",
                default="config/actiondesign-browser-api-key.json",
            )
        )
    )
    client_api_key_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_CLIENT_API_KEY_STORE_PATH",
                default="config/actiondesign-client-api-keys.json",
            )
        )
    )
    profile_allowed_hosts: list[str] = Field(
        default_factory=lambda: _csv(
            "ACTIONDESIGN_PROFILE_ALLOWED_HOSTS",
            [
                "token-plan-cn.xiaomimimo.com",
                "api.anthropic.com",
                "localhost",
                "127.0.0.1",
                "::1",
            ],
        )
    )

    mcp_proxy_enabled: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_MCP_PROXY_ENABLED", False)
    )
    public_browser_mcp_config: bool = Field(
        default_factory=lambda: _bool(
            "ACTIONDESIGN_PUBLIC_BROWSER_MCP_CONFIG",
            False,
        )
    )
    mcp_deployments_enabled: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_MCP_DEPLOYMENTS_ENABLED", False)
    )
    mcp_deployer_socket_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_MCP_DEPLOYER_SOCKET",
                default="/run/actiondesign-mcp-deployer/api.sock",
            )
        )
    )
    mcp_deployer_token: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_MCP_DEPLOYER_TOKEN")
    )
    mcp_stdio_bridge_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_MCP_STDIO_BRIDGE_PATH",
                default="/usr/local/libexec/actiondesign-mcp-stdio-bridge",
            )
        )
    )
    mcp_deployment_public_base_url: str = Field(
        default_factory=lambda: _env(
            "ACTIONDESIGN_MCP_PUBLIC_BASE_URL",
            default="https://43-135-137-212.sslip.io",
        )
    )
    mcp_servers: list[McpServerSettings] = Field(
        default_factory=lambda: _json_list("ACTIONDESIGN_MCP_SERVERS_JSON"),
        max_length=64,
    )
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: _csv(
            "ACTIONDESIGN_MCP_ALLOWED_HOSTS",
            [],
        )
    )
    mcp_allowed_http_paths: list[str] = Field(
        default_factory=lambda: _csv("ACTIONDESIGN_MCP_ALLOWED_HTTP_PATHS", [])
    )
    mcp_allowed_http_urls: list[str] = Field(
        default_factory=lambda: _csv("ACTIONDESIGN_MCP_ALLOWED_HTTP_URLS", [])
    )
    mcp_allowed_executable_paths: list[str] = Field(
        default_factory=lambda: _csv(
            "ACTIONDESIGN_MCP_ALLOWED_EXECUTABLE_PATHS",
            [],
        )
    )
    mcp_allowed_directory_paths: list[str] = Field(
        default_factory=lambda: _csv("ACTIONDESIGN_MCP_ALLOWED_DIRECTORY_PATHS", [])
    )
    mcp_timeout_seconds: float = Field(
        default_factory=lambda: _float("ACTIONDESIGN_MCP_TIMEOUT_SECONDS", 30.0),
        ge=1.0,
        le=120.0,
    )

    langsmith_enabled: bool = Field(default_factory=lambda: _bool("ACTIONDESIGN_LANGSMITH_ENABLED", False))
    langsmith_endpoint: str = Field(default_factory=lambda: _env("LANGSMITH_ENDPOINT", default="https://api.smith.langchain.com"))
    langsmith_api_key: str = Field(default_factory=lambda: _env("LANGSMITH_API_KEY"))
    langsmith_project: str = Field(default_factory=lambda: _env("LANGSMITH_PROJECT", default="actiondesign-agent"))
    langsmith_workspace_id: str = Field(default_factory=lambda: _env("LANGSMITH_WORKSPACE_ID"))
    langsmith_timeout_seconds: float = Field(default_factory=lambda: _float("ACTIONDESIGN_LANGSMITH_TIMEOUT_SECONDS", 15.0), ge=1.0, le=120.0)

    @field_validator(
        "allow_origins",
        "claude_code_models",
        "profile_allowed_hosts",
        "mcp_allowed_hosts",
        "mcp_allowed_http_paths",
        "mcp_allowed_http_urls",
        "mcp_allowed_executable_paths",
        "mcp_allowed_directory_paths",
        mode="before",
    )
    @classmethod
    def _split_lists(cls, value: Any) -> Any:
        return _split(value)

    @field_validator("mimo_messages_url", "claude_code_messages_url", "langsmith_endpoint")
    def _validate_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("value must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("URL must not contain query parameters or fragments")
        return value.rstrip("/")

    @field_validator("default_provider")
    def _validate_default_provider(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("default provider must be a valid channel identifier")
        return value

    @field_validator("mimo_auth_mode", "claude_code_auth_mode")
    def _validate_auth_mode(cls, value: str) -> str:
        if value not in {"api-key", "bearer"}:
            raise ValueError("auth mode must be api-key or bearer")
        return value

    @field_validator("mimo_default_model", "claude_code_default_model", "langsmith_project")
    def _validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        if len(value) > 256:
            raise ValueError("value is too long")
        return value

    @field_validator("profile_allowed_hosts")
    def _validate_profile_allowed_hosts(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("profile allowed host list must not be empty")
        return cls._validate_host_entries(value)

    @field_validator("mcp_allowed_hosts")
    def _validate_legacy_mcp_allowed_hosts(cls, value: list[str]) -> list[str]:
        return cls._validate_host_entries(value)

    @staticmethod
    def _validate_host_entries(value: list[str]) -> list[str]:
        for host in value:
            if "://" in host or "/" in host or "@" in host:
                raise ValueError("allowed host entries must be host names only")
        return value

    @field_validator("mcp_allowed_http_paths")
    def _validate_mcp_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if not path.startswith("/") or "?" in path or "#" in path:
                raise ValueError("MCP allowed HTTP paths must be absolute URL paths")
        return value

    @field_validator("mcp_allowed_http_urls")
    def _validate_mcp_urls(cls, value: list[str]) -> list[str]:
        normalized: set[str] = set()
        for url in value:
            parsed = urlsplit(url)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("MCP allowed HTTP URLs contain an invalid port") from exc
            path = parsed.path or "/"
            if (
                url != url.strip()
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or port is not None and not (1 <= port <= 65_535)
                or not path.startswith("/")
                or "\\" in path
                or "%" in path
                or any(part in {".", ".."} for part in path.split("/"))
            ):
                raise ValueError(
                    "MCP allowed HTTP URLs must be exact absolute HTTPS authority and path values"
                )
            canonical = f"https://{parsed.netloc.lower()}{path}"
            if canonical in normalized:
                raise ValueError("MCP allowed HTTP URLs must be unique")
            normalized.add(canonical)
        return value

    @field_validator(
        "mimo_api_key",
        "claude_code_api_key",
        "profile_admin_token",
        "admin_password",
        "langsmith_api_key",
        "service_auth_token",
        "mcp_deployer_token",
        "local_default_client_api_key",
    )
    def _validate_secret_length(cls, value: str) -> str:
        if value and len(value) > 8192:
            raise ValueError("secret is too long")
        return value

    @model_validator(mode="after")
    def _validate_feature_dependencies(self) -> Settings:
        if self.local_default_client_api_key or self.local_default_client_origin:
            parsed_origin = urlsplit(self.local_default_client_origin)
            if (
                not self.local_default_client_api_key
                or self.local_default_client_origin not in self.allow_origins
                or parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.netloc
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
                or self.service_auth_mode != "required"
            ):
                raise ValueError("Local default client requires a key, an exact allowed Origin and required service auth")
        if self.allow_private_network and (
            not self.allow_origins or "*" in self.allow_origins
        ):
            raise ValueError(
                "ACTIONDESIGN_AGENT_ALLOW_ORIGINS must be explicit when private network access is enabled"
            )
        if self.service_auth_mode == "required":
            if len(self.service_auth_token) < 32:
                raise ValueError(
                    "ACTIONDESIGN_SERVICE_AUTH_TOKEN must contain at least 32 characters when service auth is required"
                )
        elif self.service_auth_token:
            raise ValueError(
                "ACTIONDESIGN_SERVICE_AUTH_TOKEN must be empty when service auth is disabled"
            )
        if self.langsmith_enabled and not self.langsmith_api_key:
            raise ValueError("LANGSMITH_API_KEY is required when LangSmith forwarding is enabled")
        if self.mcp_proxy_enabled:
            if self.mcp_allowed_hosts or self.mcp_allowed_http_paths:
                raise ValueError(
                    "ACTIONDESIGN_MCP_ALLOWED_HOSTS and "
                    "ACTIONDESIGN_MCP_ALLOWED_HTTP_PATHS are legacy ignored settings; "
                    "use ACTIONDESIGN_MCP_ALLOWED_HTTP_URLS for exact private MCP URLs"
                )
            if self.service_auth_mode != "required":
                raise ValueError(
                    "ACTIONDESIGN_SERVICE_AUTH_MODE must be required when MCP proxy is enabled"
                )
            if "*" in self.allow_origins:
                raise ValueError(
                    "ACTIONDESIGN_AGENT_ALLOW_ORIGINS must be explicit when MCP proxy is enabled"
                )
            enabled_servers = [server for server in self.mcp_servers if server.enabled]
            ids = [server.id for server in self.mcp_servers]
            if len(ids) != len(set(ids)):
                raise ValueError("MCP server ids must be unique")
            try:
                allowed_executables = {
                    Path(path).resolve(strict=True)
                    for path in self.mcp_allowed_executable_paths
                }
            except OSError as exc:
                raise ValueError("MCP executable allowlist contains an invalid path") from exc
            for server in enabled_servers:
                if server.transport == "streamable_http":
                    continue
                else:
                    try:
                        executable = Path(server.executable_path).resolve(strict=True)
                    except OSError as exc:
                        raise ValueError("MCP executablePath is invalid") from exc
                    if not executable.is_file() or executable not in allowed_executables:
                        raise ValueError("MCP executablePath is not allowlisted")
        if self.public_browser_mcp_config:
            if not self.allow_origins or "*" in self.allow_origins:
                raise ValueError(
                    "ACTIONDESIGN_AGENT_ALLOW_ORIGINS must contain at least one "
                    "explicit Origin when public browser MCP configuration is enabled"
                )
        if self.mcp_deployments_enabled:
            if not self.mcp_proxy_enabled or not self.public_browser_mcp_config:
                raise ValueError(
                    "MCP deployments require MCP proxy and public browser MCP configuration"
                )
            if len(self.mcp_deployer_token) < 32:
                raise ValueError(
                    "ACTIONDESIGN_MCP_DEPLOYER_TOKEN must contain at least 32 characters"
                )
            if not self.mcp_deployer_socket_path.is_absolute():
                raise ValueError("MCP Deployer socket path must be absolute")
            if not self.mcp_stdio_bridge_path.is_absolute():
                raise ValueError("MCP stdio bridge path must be absolute")
            parsed_public = urlsplit(self.mcp_deployment_public_base_url)
            if (
                parsed_public.scheme != "https"
                or not parsed_public.hostname
                or parsed_public.username
                or parsed_public.password
                or parsed_public.query
                or parsed_public.fragment
            ):
                raise ValueError("MCP deployment public base URL must use absolute HTTPS")
        return self


def _split(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value
