"""Environment-backed settings for the ActionDesign gateway."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, validator

try:
    from pydantic import ConfigDict, field_validator
except ImportError:  # pragma: no cover - pydantic v1
    ConfigDict = None  # type: ignore[assignment]
    field_validator = None  # type: ignore[assignment]


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_csv(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return Path(value)


def _env_optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return Path(value)


def _env_origins() -> list[str]:
    value = os.environ.get("ACTIONDESIGN_AGENT_ALLOW_ORIGINS")
    if value is None or value.strip() == "":
        return ["*"]
    return [origin.strip() for origin in value.split(",") if origin.strip()]


class Settings(BaseModel):
    """Settings read from ActionDesign gateway environment variables."""

    if ConfigDict is not None:
        model_config = ConfigDict(arbitrary_types_allowed=True)
    else:

        class Config:
            arbitrary_types_allowed = True

    default_provider: str = Field(
        default_factory=lambda: _first_env(
            "ACTIONDESIGN_AGENT_DEFAULT_PROVIDER",
            default="mimo",
        )
    )
    log_root: Path | None = Field(
        default_factory=lambda: (
            Path(os.environ["ACTIONDESIGN_AGENT_LOG_ROOT"])
            if os.environ.get("ACTIONDESIGN_AGENT_LOG_ROOT")
            else Path("debug_logs/actiondesign-agent")
        )
    )
    allow_origins: list[str] = Field(default_factory=_env_origins)
    mimo_api_key: str = Field(
        default_factory=lambda: _first_env(
            "GXP_MIMO_API_KEY",
            "MODEL_MIMO_KEY",
        )
    )
    mimo_messages_url: str = Field(
        default_factory=lambda: _first_env(
            "GXP_MIMO_MESSAGES_URL",
            "MODEL_MIMO_URL",
            default="https://api.xiaomimimo.com/anthropic/v1/messages",
        )
    )
    mimo_default_model: str = Field(
        default_factory=lambda: _first_env(
            "GXP_MIMO_DEFAULT_MODEL",
            default="mimo-v2.5",
        )
    )
    mimo_auth_mode: str = Field(
        default_factory=lambda: _first_env(
            "GXP_MIMO_AUTH_MODE",
            default="api-key",
        )
    )
    mimo_timeout_seconds: float = Field(
        default_factory=lambda: _env_float("GXP_MIMO_TIMEOUT_SECONDS", 120.0)
    )
    claude_code_enabled: bool = Field(
        default_factory=lambda: _env_bool("CLAUDE_CODE_PROVIDER_ENABLED", True)
    )
    claude_code_default_model: str = Field(
        default_factory=lambda: _first_env("CLAUDE_CODE_DEFAULT_MODEL")
    )
    claude_code_timeout_seconds: float = Field(
        default_factory=lambda: _env_float("CLAUDE_CODE_TIMEOUT_SECONDS", 300.0)
    )
    claude_code_internal_tools: list[str] = Field(
        default_factory=lambda: _env_csv(
            "CLAUDE_CODE_INTERNAL_TOOLS",
            ["Read", "Grep", "Glob", "LS"],
        )
    )
    claude_code_auto_allow_internal_tools: bool = Field(
        default_factory=lambda: _env_bool(
            "CLAUDE_CODE_AUTO_ALLOW_INTERNAL_TOOLS",
            True,
        )
    )
    mimo_max_backend_tool_turns: int = Field(
        default_factory=lambda: int(
            _env_float("MIMO_MAX_BACKEND_TOOL_TURNS", 6.0)
        )
    )
    mimo_max_backend_tool_calls_per_turn: int = Field(
        default_factory=lambda: int(
            _env_float("MIMO_MAX_BACKEND_TOOL_CALLS_PER_TURN", 4.0)
        )
    )
    claude_code_max_backend_tool_turns: int = Field(
        default_factory=lambda: int(
            _env_float("CLAUDE_CODE_MAX_BACKEND_TOOL_TURNS", 6.0)
        )
    )
    claude_code_max_backend_tool_calls_per_turn: int = Field(
        default_factory=lambda: int(
            _env_float("CLAUDE_CODE_MAX_BACKEND_TOOL_CALLS_PER_TURN", 4.0)
        )
    )
    mcp_read_only_tool_names: list[str] = Field(
        default_factory=lambda: _env_csv(
            "ACTIONDESIGN_AGENT_MCP_READ_ONLY_TOOLS",
            [],
        )
    )
    knowledge_root: Path | None = Field(
        default_factory=lambda: _env_optional_path("ACTIONDESIGN_KNOWLEDGE_ROOT")
    )
    knowledge_index_dir: Path = Field(
        default_factory=lambda: _env_path(
            "ACTIONDESIGN_KNOWLEDGE_INDEX_DIR",
            Path("debug_logs/actiondesign-agent/knowledge-index"),
        )
    )
    embedding_provider: str = Field(
        default_factory=lambda: _first_env(
            "ACTIONDESIGN_EMBEDDING_PROVIDER",
            default="openai-compatible",
        )
    )
    embedding_base_url: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_EMBEDDING_BASE_URL")
    )
    embedding_api_key: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_EMBEDDING_API_KEY")
    )
    embedding_model: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_EMBEDDING_MODEL")
    )
    embedding_dimensions: int = Field(
        default_factory=lambda: _env_int(
            "ACTIONDESIGN_EMBEDDING_DIMENSIONS",
            1024,
        )
    )
    embedding_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ACTIONDESIGN_EMBEDDING_TIMEOUT_SECONDS",
            60.0,
        )
    )
    knowledge_max_results: int = Field(
        default_factory=lambda: _env_int("ACTIONDESIGN_KNOWLEDGE_MAX_RESULTS", 4)
    )
    knowledge_max_chars_per_item: int = Field(
        default_factory=lambda: _env_int(
            "ACTIONDESIGN_KNOWLEDGE_MAX_CHARS_PER_ITEM",
            4000,
        )
    )
    knowledge_max_context_chars: int = Field(
        default_factory=lambda: _env_int(
            "ACTIONDESIGN_KNOWLEDGE_MAX_CONTEXT_CHARS",
            12000,
        )
    )
    knowledge_recall_limit: int = Field(
        default_factory=lambda: _env_int("ACTIONDESIGN_KNOWLEDGE_RECALL_LIMIT", 20)
    )
    knowledge_admin_token: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_KNOWLEDGE_ADMIN_TOKEN")
    )
    qdrant_url: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_QDRANT_URL")
    )
    qdrant_api_key: str = Field(
        default_factory=lambda: _first_env("ACTIONDESIGN_QDRANT_API_KEY")
    )
    qdrant_collection: str = Field(
        default_factory=lambda: _first_env(
            "ACTIONDESIGN_QDRANT_COLLECTION",
            default="actiondesign_knowledge",
        )
    )
    embedding_client: Any = None
    backend_tool_executor: Any = None
    knowledge_store: Any = None

    if field_validator is not None:

        @field_validator("allow_origins", mode="before")
        @classmethod
        def _split_origins_v2(cls, value: Any) -> Any:
            return _split_origins(value)

    else:

        @validator("allow_origins", pre=True)
        def _split_origins_v1(cls, value: Any) -> Any:
            return _split_origins(value)

    if field_validator is not None:

        @field_validator("claude_code_internal_tools", mode="before")
        @classmethod
        def _split_internal_tools_v2(cls, value: Any) -> Any:
            return _split_csv(value)

        @field_validator("mcp_read_only_tool_names", mode="before")
        @classmethod
        def _split_read_only_mcp_tools_v2(cls, value: Any) -> Any:
            return _split_csv(value)

    else:

        @validator("claude_code_internal_tools", pre=True)
        def _split_internal_tools_v1(cls, value: Any) -> Any:
            return _split_csv(value)

        @validator("mcp_read_only_tool_names", pre=True)
        def _split_read_only_mcp_tools_v1(cls, value: Any) -> Any:
            return _split_csv(value)


def _split_origins(value: Any) -> Any:
    if isinstance(value, str):
        return [origin.strip() for origin in value.split(",") if origin.strip()]
    return value


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value
