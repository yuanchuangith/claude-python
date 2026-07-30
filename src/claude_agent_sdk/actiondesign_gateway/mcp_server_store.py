from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .mcp_proxy import McpServerConfig
from .models import McpToolDefinition
from .secret_store import ManagedSecretStore


_SCHEMA_VERSION = "1"
_RuntimeT = TypeVar("_RuntimeT")


class McpServerStoreError(RuntimeError):
    """Raised when managed MCP Server metadata cannot be loaded or saved."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


class McpServerEnabledRequest(_StrictModel):
    enabled: bool


class McpSecretBindingStatus(_StrictModel):
    secret_id: str = Field(alias="secretId")
    configured: bool


class McpServerResponse(McpServerConfig):
    source: Literal["environment", "managed"]
    proxy_enabled: bool = Field(alias="proxyEnabled")
    secret_binding_status: list[McpSecretBindingStatus] = Field(
        default_factory=list,
        alias="secretBindingStatus",
        max_length=32,
    )


class McpServerConnectionTestResponse(_StrictModel):
    id: str
    status: Literal["healthy", "unhealthy"]
    checked_at: datetime = Field(alias="checkedAt")
    latency_ms: int = Field(alias="latencyMs", ge=0)
    server_name: str = Field(default="", alias="serverName", max_length=128)
    server_version: str = Field(default="", alias="serverVersion", max_length=128)
    tool_count: int = Field(default=0, alias="toolCount", ge=0, le=256)
    tools: list[McpToolDefinition] = Field(default_factory=list, max_length=256)
    error_code: str = Field(default="", alias="errorCode", max_length=128)

    @model_validator(mode="after")
    def _status_shape_is_consistent(self) -> "McpServerConnectionTestResponse":
        if self.status == "healthy" and self.error_code:
            raise ValueError("healthy MCP connection result must not include errorCode")
        if self.status == "unhealthy" and not self.error_code:
            raise ValueError("unhealthy MCP connection result requires errorCode")
        if self.tool_count != len(self.tools):
            raise ValueError("MCP connection result toolCount does not match tools")
        return self


class McpServerDeleteResponse(_StrictModel):
    id: str
    deleted: Literal[True] = True


class _ManagedMcpServerFile(_StrictModel):
    schema_version: Literal["1"] = Field(alias="schemaVersion")
    servers: list[McpServerConfig] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _server_ids_are_unique(self) -> "_ManagedMcpServerFile":
        ids = [server.id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("managed MCP Server ids must be unique")
        return self


class McpServerStore:
    """Strict persisted managed MCP Servers plus immutable environment Servers."""

    def __init__(
        self,
        path: Path,
        environment_servers: list[McpServerConfig | dict[str, Any]],
    ) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        parsed_environment = [
            McpServerConfig.model_validate(server) for server in environment_servers
        ]
        environment_ids = [server.id for server in parsed_environment]
        if len(environment_ids) != len(set(environment_ids)):
            raise McpServerStoreError("environment MCP Server ids must be unique")
        self._environment = {server.id: server for server in parsed_environment}
        self._managed = self._load()
        conflicts = sorted(set(self._environment).intersection(self._managed))
        if conflicts:
            raise McpServerStoreError(
                "managed MCP Server ids conflict with read-only environment Servers: "
                + ", ".join(conflicts)
            )

    def list_configs(self) -> list[McpServerConfig]:
        with self._lock:
            return self._merged_configs(self._managed)

    def list_public(
        self,
        secret_store: ManagedSecretStore,
        *,
        proxy_enabled: bool,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public(
                    server,
                    "environment" if server.id in self._environment else "managed",
                    secret_store,
                    proxy_enabled=proxy_enabled,
                )
                for server in self._merged_configs(self._managed)
            ]

    def get_public(
        self,
        server_id: str,
        secret_store: ManagedSecretStore,
        *,
        proxy_enabled: bool,
    ) -> dict[str, Any] | None:
        with self._lock:
            server = self._environment.get(server_id) or self._managed.get(server_id)
            if server is None:
                return None
            source = "environment" if server_id in self._environment else "managed"
            return self._public(
                server,
                source,
                secret_store,
                proxy_enabled=proxy_enabled,
            )

    def upsert(
        self,
        server: McpServerConfig | dict[str, Any],
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], None],
    ) -> tuple[McpServerConfig, _RuntimeT]:
        parsed = McpServerConfig.model_validate(server)
        with self._lock:
            if parsed.id in self._environment:
                raise PermissionError("environment MCP Servers are read-only")
            candidate = dict(self._managed)
            candidate[parsed.id] = parsed
            runtime = build_runtime(self._merged_configs(candidate))
            previous = self._managed
            self._managed = candidate
            try:
                self._save()
            except Exception:
                self._managed = previous
                raise
            publish_runtime(runtime)
            return parsed, runtime

    def set_enabled(
        self,
        server_id: str,
        enabled: bool,
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], None],
    ) -> tuple[McpServerConfig, _RuntimeT]:
        with self._lock:
            if server_id in self._environment:
                raise PermissionError("environment MCP Servers are read-only")
            current = self._managed.get(server_id)
            if current is None:
                raise KeyError(server_id)
            updated = McpServerConfig.model_validate(
                {
                    **current.model_dump(mode="python", by_alias=True),
                    "enabled": enabled,
                }
            )
            candidate = dict(self._managed)
            candidate[server_id] = updated
            runtime = build_runtime(self._merged_configs(candidate))
            previous = self._managed
            self._managed = candidate
            try:
                self._save()
            except Exception:
                self._managed = previous
                raise
            publish_runtime(runtime)
            return updated, runtime

    def delete(
        self,
        server_id: str,
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], None],
    ) -> _RuntimeT:
        with self._lock:
            if server_id in self._environment:
                raise PermissionError("environment MCP Servers are read-only")
            if server_id not in self._managed:
                raise KeyError(server_id)
            candidate = dict(self._managed)
            del candidate[server_id]
            runtime = build_runtime(self._merged_configs(candidate))
            previous = self._managed
            self._managed = candidate
            try:
                self._save()
            except Exception:
                self._managed = previous
                raise
            publish_runtime(runtime)
            return runtime

    def _load(self) -> dict[str, McpServerConfig]:
        if not self.path.exists():
            return {}
        try:
            payload = _ManagedMcpServerFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            return {server.id: server for server in payload.servers}
        except (OSError, ValueError, ValidationError) as exc:
            raise McpServerStoreError(
                "managed MCP Server metadata is invalid and was not loaded"
            ) from exc

    def _save(self) -> None:
        payload = _ManagedMcpServerFile.model_validate(
            {
                "schemaVersion": _SCHEMA_VERSION,
                "servers": [self._managed[item] for item in sorted(self._managed)],
            }
        ).model_dump(mode="json", by_alias=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            temp_path.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            raise McpServerStoreError(
                "managed MCP Server metadata is unavailable"
            ) from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _merged_configs(
        self,
        managed: dict[str, McpServerConfig],
    ) -> list[McpServerConfig]:
        merged = {**self._environment, **managed}
        return [merged[item] for item in sorted(merged)]

    @staticmethod
    def _secret_ids(server: McpServerConfig) -> list[str]:
        transport = server.stdio if server.transport == "stdio" else server.streamable_http
        if transport is None:
            return []
        return sorted({binding.secret_id for binding in transport.secret_bindings})

    @classmethod
    def _public(
        cls,
        server: McpServerConfig,
        source: Literal["environment", "managed"],
        secret_store: ManagedSecretStore,
        *,
        proxy_enabled: bool,
    ) -> dict[str, Any]:
        binding_status = []
        for secret_id in cls._secret_ids(server):
            secret = secret_store.get(secret_id)
            binding_status.append(
                {
                    "secretId": secret_id,
                    "configured": bool(secret and secret.get("configured")),
                }
            )
        return McpServerResponse.model_validate(
            {
                **server.model_dump(mode="python", by_alias=True),
                "source": source,
                "proxyEnabled": proxy_enabled,
                "secretBindingStatus": binding_status,
            }
        ).model_dump(mode="json", by_alias=True)


__all__ = [
    "McpServerConnectionTestResponse",
    "McpServerDeleteResponse",
    "McpServerEnabledRequest",
    "McpServerResponse",
    "McpServerStore",
    "McpServerStoreError",
]
