"""Environment-backed settings for the thin ActionDesign gateway."""

from __future__ import annotations

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
    service_auth_mode: Literal["disabled", "required"] = Field(
        default_factory=lambda: _env(
            "ACTIONDESIGN_SERVICE_AUTH_MODE",
            default="disabled",
        )
    )
    service_auth_token: str = Field(
        default_factory=lambda: _env("ACTIONDESIGN_SERVICE_AUTH_TOKEN")
    )
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
    browser_api_key_store_path: Path = Field(
        default_factory=lambda: Path(
            _env(
                "ACTIONDESIGN_BROWSER_API_KEY_STORE_PATH",
                default="config/actiondesign-browser-api-key.json",
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

    gxp_readonly_mcp_enabled: bool = Field(
        default_factory=lambda: _bool("ACTIONDESIGN_GXP_READONLY_MCP_ENABLED", False)
    )
    gxp_readonly_mcp_python_path: Path | None = Field(
        default_factory=lambda: _optional_path("ACTIONDESIGN_GXP_READONLY_MCP_PYTHON_PATH")
    )
    gxp_readonly_mcp_server_path: Path | None = Field(
        default_factory=lambda: _optional_path("ACTIONDESIGN_GXP_READONLY_MCP_SERVER_PATH")
    )
    gxp_readonly_mcp_timeout_seconds: float = Field(
        default_factory=lambda: _float("ACTIONDESIGN_GXP_READONLY_MCP_TIMEOUT_SECONDS", 30.0),
        ge=1.0,
        le=120.0,
    )

    langsmith_enabled: bool = Field(default_factory=lambda: _bool("ACTIONDESIGN_LANGSMITH_ENABLED", False))
    langsmith_endpoint: str = Field(default_factory=lambda: _env("LANGSMITH_ENDPOINT", default="https://api.smith.langchain.com"))
    langsmith_api_key: str = Field(default_factory=lambda: _env("LANGSMITH_API_KEY"))
    langsmith_project: str = Field(default_factory=lambda: _env("LANGSMITH_PROJECT", default="actiondesign-agent"))
    langsmith_workspace_id: str = Field(default_factory=lambda: _env("LANGSMITH_WORKSPACE_ID"))
    langsmith_timeout_seconds: float = Field(default_factory=lambda: _float("ACTIONDESIGN_LANGSMITH_TIMEOUT_SECONDS", 15.0), ge=1.0, le=120.0)

    @field_validator("allow_origins", "claude_code_models", "profile_allowed_hosts", mode="before")
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
    def _validate_allowed_hosts(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("profile_allowed_hosts must not be empty")
        for host in value:
            if "://" in host or "/" in host or "@" in host:
                raise ValueError("profile_allowed_hosts entries must be host names only")
        return value

    @field_validator(
        "mimo_api_key",
        "claude_code_api_key",
        "profile_admin_token",
        "admin_password",
        "langsmith_api_key",
        "service_auth_token",
    )
    def _validate_secret_length(cls, value: str) -> str:
        if value and len(value) > 8192:
            raise ValueError("secret is too long")
        return value

    @model_validator(mode="after")
    def _validate_feature_dependencies(self) -> "Settings":
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
        if self.gxp_readonly_mcp_enabled:
            if self.service_auth_mode != "required":
                raise ValueError(
                    "ACTIONDESIGN_SERVICE_AUTH_MODE must be required when read-only MCP is enabled"
                )
            if "*" in self.allow_origins:
                raise ValueError(
                    "ACTIONDESIGN_AGENT_ALLOW_ORIGINS must be explicit when read-only MCP is enabled"
                )
            for label, path in (
                ("gxp_readonly_mcp_python_path", self.gxp_readonly_mcp_python_path),
                ("gxp_readonly_mcp_server_path", self.gxp_readonly_mcp_server_path),
            ):
                if path is None or not path.is_absolute() or not path.is_file():
                    raise ValueError(f"{label} must be an existing absolute file when read-only MCP is enabled")
        return self


def _split(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value
