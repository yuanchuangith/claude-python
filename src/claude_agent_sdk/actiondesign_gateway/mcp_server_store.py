from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .mcp_proxy import McpAuthMode, McpExecutionMode, McpServerConfig
from .models import McpToolDefinition
from .secret_store import ManagedSecretCandidateOverlay, ManagedSecretStore

_SCHEMA_VERSION = "1"
_RuntimeT = TypeVar("_RuntimeT")
McpConnectionStatus = Literal[
    "disconnected",
    "connecting",
    "healthy",
    "unhealthy",
    "draining",
]
McpCandidateOperation = Literal["upsert", "set_enabled", "delete", "secret_overlay"]


class McpServerStoreError(RuntimeError):
    """Raised when managed MCP Server metadata cannot be loaded or saved."""


class McpServerCandidateStaleError(McpServerStoreError):
    """Raised when a tested candidate no longer matches the active store revision."""


@dataclass(frozen=True)
class McpServerCandidate:
    """Immutable MCP configuration snapshot to build and connection-test."""

    base_revision: int
    operation: McpCandidateOperation
    server_ids: tuple[str, ...]
    configs: tuple[McpServerConfig, ...]
    managed_configs: tuple[McpServerConfig, ...]
    active_versions: tuple[tuple[str, int], ...]

    def config(self, server_id: str) -> McpServerConfig | None:
        return next((item for item in self.configs if item.id == server_id), None)


@dataclass(frozen=True)
class McpRuntimeTransition(Generic[_RuntimeT]):
    """Published runtime plus the previous runtime retained for draining calls."""

    candidate: McpServerCandidate
    active_runtime: _RuntimeT
    draining_runtime: _RuntimeT | None


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


class McpServerResponse(_StrictModel):
    id: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    transport: Literal["stdio", "streamable_http"]
    expected_server_name: str = Field(
        default="",
        alias="expectedServerName",
        max_length=128,
    )
    timeout_seconds: float = Field(alias="timeoutSeconds", ge=1.0, le=600.0)
    enabled: bool
    source: Literal["environment", "managed"]
    proxy_enabled: bool = Field(alias="proxyEnabled")
    browser_configurable: bool = Field(alias="browserConfigurable")
    execution_mode: McpExecutionMode = Field(alias="executionMode")
    auth_mode: McpAuthMode = Field(alias="authMode")
    browser_direct_url: str = Field(default="", alias="browserDirectUrl")
    config_version: int = Field(alias="configVersion", ge=1)
    active_version: int = Field(alias="activeVersion", ge=0)
    connection_status: McpConnectionStatus = Field(alias="connectionStatus")
    secret_binding_status: list[McpSecretBindingStatus] = Field(
        default_factory=list,
        alias="secretBindingStatus",
        max_length=32,
    )


class McpRuntimeConfigResponse(_StrictModel):
    """Minimal safe MCP configuration exposed to the browser runtime."""

    id: str
    transport: Literal["stdio", "streamable_http"]
    execution_mode: McpExecutionMode = Field(alias="executionMode")
    auth_mode: McpAuthMode = Field(alias="authMode")
    browser_direct_url: str = Field(default="", alias="browserDirectUrl")
    enabled: bool
    proxy_enabled: bool = Field(alias="proxyEnabled")
    config_version: int = Field(alias="configVersion", ge=1)
    active_version: int = Field(alias="activeVersion", ge=0)
    connection_status: McpConnectionStatus = Field(alias="connectionStatus")


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
        self._revision = 0
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
        configs = self._merged_configs(self._managed)
        self._active_versions = {
            server.id: server.config_version for server in configs
        }
        self._connection_statuses: dict[str, McpConnectionStatus] = {
            server.id: "disconnected" for server in configs
        }

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
                    active_version=self._active_versions.get(server.id, 0),
                    connection_status=self._connection_statuses.get(
                        server.id, "disconnected"
                    ),
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
            source: Literal["environment", "managed"] = (
                "environment" if server_id in self._environment else "managed"
            )
            return self._public(
                server,
                source,
                secret_store,
                proxy_enabled=proxy_enabled,
                active_version=self._active_versions.get(server.id, 0),
                connection_status=self._connection_statuses.get(
                    server.id, "disconnected"
                ),
            )

    def create_upsert_candidate(
        self,
        server: McpServerConfig | dict[str, Any],
    ) -> McpServerCandidate:
        parsed = McpServerConfig.model_validate(server)
        with self._lock:
            return self._create_upsert_candidate(parsed)

    def create_enabled_candidate(
        self,
        server_id: str,
        enabled: bool,
    ) -> McpServerCandidate:
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
            return self._create_upsert_candidate(
                updated,
                operation="set_enabled",
            )

    def create_delete_candidate(self, server_id: str) -> McpServerCandidate:
        with self._lock:
            if server_id in self._environment:
                raise PermissionError("environment MCP Servers are read-only")
            if server_id not in self._managed:
                raise KeyError(server_id)
            managed = dict(self._managed)
            del managed[server_id]
            return self._candidate(
                operation="delete",
                server_ids=(server_id,),
                managed=managed,
                active_versions={server_id: self._next_active_version(server_id)},
            )

    def create_secret_candidate(
        self,
        secret_ids: Collection[str],
    ) -> McpServerCandidate:
        """Snapshot configs affected by candidate Secret values.

        The values themselves belong in ``ManagedSecretCandidateOverlay`` and are
        intentionally absent from this serializable configuration snapshot.
        """

        with self._lock:
            affected = self.affected_server_ids(
                self._merged_configs(self._managed),
                secret_ids,
            )
            managed = dict(self._managed)
            active_versions = {
                server_id: self._next_active_version(server_id)
                for server_id in affected
            }
            for server_id, next_version in active_versions.items():
                current = managed.get(server_id)
                if current is None:
                    continue
                managed[server_id] = McpServerConfig.model_validate(
                    current.model_copy(
                        update={"config_version": next_version}
                    ).model_dump(mode="python")
                )
            return self._candidate(
                operation="secret_overlay",
                server_ids=affected,
                managed=managed,
                active_versions=active_versions,
            )

    def commit_candidate(
        self,
        candidate: McpServerCandidate,
        runtime: _RuntimeT,
        publish_runtime: Callable[[_RuntimeT], _RuntimeT | None],
        *,
        previous_runtime: _RuntimeT | None = None,
        connection_status: McpConnectionStatus = "healthy",
    ) -> McpRuntimeTransition[_RuntimeT]:
        """Atomically publish a successfully tested candidate.

        Candidate build or connection-test failures never call this method and
        therefore cannot change active metadata or runtime.  The publish callback
        may return the old runtime so callers can retain it while in-flight
        requests drain naturally.
        """

        if connection_status not in {
            "disconnected",
            "connecting",
            "healthy",
            "unhealthy",
            "draining",
        }:
            raise ValueError("MCP connection status is invalid")
        with self._lock:
            if candidate.base_revision != self._revision:
                raise McpServerCandidateStaleError(
                    "MCP Server candidate is stale and must be rebuilt"
                )
            previous_managed = self._managed
            previous_versions = dict(self._active_versions)
            previous_statuses = dict(self._connection_statuses)
            next_managed = {server.id: server for server in candidate.managed_configs}
            next_configs = {
                server.id: server for server in candidate.configs
            }
            self._managed = next_managed
            self._active_versions = {
                server_id: version
                for server_id, version in self._active_versions.items()
                if server_id in next_configs
            }
            self._connection_statuses = {
                server_id: status
                for server_id, status in self._connection_statuses.items()
                if server_id in next_configs
            }
            for server_id, version in candidate.active_versions:
                if server_id not in next_configs:
                    continue
                self._active_versions[server_id] = version
                server = next_configs[server_id]
                self._connection_statuses[server_id] = (
                    connection_status if server.enabled else "disconnected"
                )
            for server_id, server in next_configs.items():
                self._active_versions.setdefault(server_id, server.config_version)
                self._connection_statuses.setdefault(server_id, "disconnected")
            metadata_changed = next_managed != previous_managed
            metadata_saved = False
            try:
                if metadata_changed:
                    self._save()
                    metadata_saved = True
                draining_runtime = publish_runtime(runtime)
            except Exception:
                self._managed = previous_managed
                self._active_versions = previous_versions
                self._connection_statuses = previous_statuses
                if metadata_saved:
                    self._save()
                raise
            self._revision += 1
            return McpRuntimeTransition(
                candidate=candidate,
                active_runtime=runtime,
                draining_runtime=(
                    draining_runtime
                    if draining_runtime is not None
                    else previous_runtime
                ),
            )

    def set_connection_status(
        self,
        server_id: str,
        status: McpConnectionStatus,
    ) -> None:
        if status not in {
            "disconnected",
            "connecting",
            "healthy",
            "unhealthy",
            "draining",
        }:
            raise ValueError("MCP connection status is invalid")
        with self._lock:
            if server_id not in self._environment and server_id not in self._managed:
                raise KeyError(server_id)
            self._connection_statuses[server_id] = status

    @classmethod
    def affected_server_ids(
        cls,
        configs: Collection[McpServerConfig],
        secret_ids: Collection[str],
    ) -> tuple[str, ...]:
        requested = set(secret_ids)
        return tuple(
            sorted(
                server.id
                for server in configs
                if requested.intersection(cls._secret_ids(server))
            )
        )

    def upsert(
        self,
        server: McpServerConfig | dict[str, Any],
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], _RuntimeT | None],
    ) -> tuple[McpServerConfig, _RuntimeT]:
        parsed = McpServerConfig.model_validate(server)
        with self._lock:
            candidate = self._create_upsert_candidate(parsed)
            runtime = build_runtime(list(candidate.configs))
            self.commit_candidate(
                candidate,
                runtime,
                publish_runtime,
                connection_status="disconnected",
            )
            saved = candidate.config(parsed.id)
            assert saved is not None
            return saved, runtime

    def set_enabled(
        self,
        server_id: str,
        enabled: bool,
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], _RuntimeT | None],
    ) -> tuple[McpServerConfig, _RuntimeT]:
        with self._lock:
            candidate = self.create_enabled_candidate(server_id, enabled)
            runtime = build_runtime(list(candidate.configs))
            self.commit_candidate(
                candidate,
                runtime,
                publish_runtime,
                connection_status="disconnected",
            )
            saved = candidate.config(server_id)
            assert saved is not None
            return saved, runtime

    def delete(
        self,
        server_id: str,
        build_runtime: Callable[[list[McpServerConfig]], _RuntimeT],
        publish_runtime: Callable[[_RuntimeT], _RuntimeT | None],
    ) -> _RuntimeT:
        with self._lock:
            candidate = self.create_delete_candidate(server_id)
            runtime = build_runtime(list(candidate.configs))
            self.commit_candidate(
                candidate,
                runtime,
                publish_runtime,
                connection_status="disconnected",
            )
            return runtime

    def _create_upsert_candidate(
        self,
        parsed: McpServerConfig,
        *,
        operation: Literal["upsert", "set_enabled"] = "upsert",
    ) -> McpServerCandidate:
        if parsed.id in self._environment:
            raise PermissionError("environment MCP Servers are read-only")
        next_version = self._next_active_version(parsed.id)
        versioned = parsed.model_copy(update={"config_version": next_version})
        versioned = McpServerConfig.model_validate(versioned.model_dump(mode="python"))
        managed = dict(self._managed)
        managed[versioned.id] = versioned
        return self._candidate(
            operation=operation,
            server_ids=(versioned.id,),
            managed=managed,
            active_versions={versioned.id: next_version},
        )

    def _candidate(
        self,
        *,
        operation: McpCandidateOperation,
        server_ids: tuple[str, ...],
        managed: dict[str, McpServerConfig],
        active_versions: Mapping[str, int],
    ) -> McpServerCandidate:
        return McpServerCandidate(
            base_revision=self._revision,
            operation=operation,
            server_ids=tuple(sorted(server_ids)),
            configs=tuple(self._merged_configs(managed)),
            managed_configs=tuple(managed[item] for item in sorted(managed)),
            active_versions=tuple(sorted(active_versions.items())),
        )

    def _next_active_version(self, server_id: str) -> int:
        current = self._environment.get(server_id) or self._managed.get(server_id)
        config_version = current.config_version if current is not None else 0
        return max(config_version, self._active_versions.get(server_id, 0)) + 1

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
        secret_store: ManagedSecretStore | ManagedSecretCandidateOverlay,
        *,
        proxy_enabled: bool,
        active_version: int,
        connection_status: McpConnectionStatus,
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
        browser_direct_url = ""
        if (
            server.transport == "streamable_http"
            and server.auth_mode == "none"
            and server.execution_mode in {"auto", "browser_direct"}
        ):
            assert server.streamable_http is not None
            browser_direct_url = server.streamable_http.url
        return McpServerResponse.model_validate(
            {
                "id": server.id,
                "displayName": server.display_name,
                "transport": server.transport,
                "expectedServerName": server.expected_server_name,
                "timeoutSeconds": server.timeout_seconds,
                "enabled": server.enabled,
                "source": source,
                "proxyEnabled": proxy_enabled,
                "browserConfigurable": source == "managed",
                "executionMode": server.execution_mode,
                "authMode": server.auth_mode,
                "browserDirectUrl": browser_direct_url,
                "configVersion": server.config_version,
                "activeVersion": active_version,
                "connectionStatus": connection_status,
                "secretBindingStatus": binding_status,
            }
        ).model_dump(mode="json", by_alias=True)


__all__ = [
    "McpConnectionStatus",
    "McpRuntimeConfigResponse",
    "McpRuntimeTransition",
    "McpServerCandidate",
    "McpServerCandidateStaleError",
    "McpServerConnectionTestResponse",
    "McpServerDeleteResponse",
    "McpServerEnabledRequest",
    "McpServerResponse",
    "McpServerStore",
    "McpServerStoreError",
]
