from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


_SCHEMA_VERSION = "1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$"
)
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_RESERVED_HEADERS = {"accept", "content-type", "mcp-protocol-version", "mcp-session-id"}
DeploymentStatus = Literal[
    "deploying",
    "running",
    "stopped",
    "degraded",
    "rollback_failed",
]
DeploymentJobStatus = Literal[
    "queued",
    "pulling",
    "cloning",
    "building",
    "starting",
    "awaiting_browser_validation",
    "promoting",
    "running",
    "failed",
    "rolling_back",
    "rolled_back",
    "rollback_failed",
]
_TERMINAL_JOB_STATES = {"running", "failed", "rolled_back", "rollback_failed"}


class McpDeploymentError(RuntimeError):
    """Base error for deployment metadata and Deployer communication."""


class McpDeploymentVersionConflictError(McpDeploymentError):
    def __init__(self, deployment_id: str, expected: int, current: int) -> None:
        super().__init__(
            f"MCP deployment {deployment_id!r} version conflict: "
            f"expected {expected}, current {current}"
        )
        self.deployment_id = deployment_id
        self.expected = expected
        self.current = current


class McpDeploymentBusyError(McpDeploymentError):
    """Raised when a second mutation targets a deployment with an active job."""


class McpDeployerUnavailableError(McpDeploymentError):
    """Raised when the privileged Deployer cannot be reached."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


def _safe_id(value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError("MCP deployment identifier is invalid")
    return value


def _safe_relative_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{label} must be a safe relative POSIX path")
    return value


class McpDeploymentSecretBinding(_StrictModel):
    name: str
    secret_id: str = Field(alias="secretId")
    value_prefix: str = Field(default="", alias="valuePrefix", max_length=64)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, value: str) -> str:
        if not _ENV_NAME_RE.fullmatch(value):
            raise ValueError("deployment environment variable name is invalid")
        return value

    @field_validator("secret_id")
    @classmethod
    def _secret_id_is_safe(cls, value: str) -> str:
        return _safe_id(value)


class McpDeploymentResources(_StrictModel):
    cpu: float = Field(default=0.5, ge=0.1, le=1.0)
    memory_mib: int = Field(default=256, alias="memoryMiB", ge=64, le=512)
    pids: int = Field(default=128, ge=16, le=128)


class McpHttpHealthCheck(_StrictModel):
    type: Literal["http", "tcp"] = "http"
    path: str = Field(default="/health", min_length=1, max_length=512)

    @field_validator("path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("health check path is invalid")
        return value

    @model_validator(mode="after")
    def _tcp_has_no_custom_path(self) -> McpHttpHealthCheck:
        if self.type == "tcp" and self.path != "/health":
            raise ValueError("tcp health check cannot specify a path")
        return self


class McpHttpRuntime(_StrictModel):
    container_port: int = Field(alias="containerPort", ge=1024, le=65535)
    mcp_path: str = Field(default="/mcp", alias="mcpPath", min_length=1, max_length=512)
    health_check: McpHttpHealthCheck = Field(
        default_factory=McpHttpHealthCheck,
        alias="healthCheck",
    )

    @field_validator("mcp_path")
    @classmethod
    def _mcp_path_is_safe(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("MCP path is invalid")
        return value


class McpStdioRuntime(_StrictModel):
    command: list[str] = Field(min_length=1, max_length=32)

    @field_validator("command")
    @classmethod
    def _command_is_safe(cls, value: list[str]) -> list[str]:
        for argument in value:
            if not argument or len(argument) > 4096 or "\x00" in argument:
                raise ValueError("stdio command argument is invalid")
        return value


class McpDeploymentManifest(_StrictModel):
    api_version: Literal["actiondesign.mcp/v1"] = Field(alias="apiVersion")
    transport: Literal["streamable_http", "stdio"]
    http: McpHttpRuntime | None = None
    stdio: McpStdioRuntime | None = None
    environment: list[McpDeploymentSecretBinding] = Field(
        default_factory=list,
        max_length=32,
    )
    resources: McpDeploymentResources = Field(default_factory=McpDeploymentResources)
    startup_timeout_seconds: int = Field(
        default=60,
        alias="startupTimeoutSeconds",
        ge=5,
        le=300,
    )

    @model_validator(mode="after")
    def _transport_is_exact(self) -> McpDeploymentManifest:
        if self.transport == "streamable_http":
            if self.http is None or self.stdio is not None:
                raise ValueError("HTTP deployment requires only http runtime settings")
        elif self.stdio is None or self.http is not None:
            raise ValueError("stdio deployment requires only stdio runtime settings")
        names = [item.name for item in self.environment]
        if len(names) != len(set(names)):
            raise ValueError("deployment environment names must be unique")
        return self


class McpOciSource(_StrictModel):
    type: Literal["oci"]
    image: str = Field(min_length=1, max_length=1024)
    credential_secret_id: str = Field(default="", alias="credentialSecretId")

    @field_validator("image")
    @classmethod
    def _image_is_digest_pinned(cls, value: str) -> str:
        if not _DIGEST_IMAGE_RE.fullmatch(value):
            raise ValueError("OCI image must be pinned to a sha256 digest")
        return value

    @field_validator("credential_secret_id")
    @classmethod
    def _credential_id_is_safe(cls, value: str) -> str:
        return _safe_id(value) if value else value


class McpGitSource(_StrictModel):
    type: Literal["git"]
    repository: str = Field(min_length=1, max_length=2048)
    commit: str
    dockerfile_path: str = Field(default="Dockerfile", alias="dockerfilePath")
    context_path: str = Field(default=".", alias="contextPath")
    manifest_path: str = Field(default="mcp-deploy.yaml", alias="manifestPath")
    credential_secret_id: str = Field(default="", alias="credentialSecretId")

    @field_validator("repository")
    @classmethod
    def _repository_is_https_or_ssh(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise ValueError("Git repository must use absolute HTTPS or SSH")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Git repository cannot contain credentials, query, or fragment")
        return value

    @field_validator("commit")
    @classmethod
    def _commit_is_pinned(cls, value: str) -> str:
        if not _COMMIT_RE.fullmatch(value):
            raise ValueError("Git commit must be a full lowercase SHA-1")
        return value

    @field_validator("dockerfile_path")
    @classmethod
    def _dockerfile_is_safe(cls, value: str) -> str:
        return _safe_relative_path(value, label="dockerfilePath")

    @field_validator("context_path")
    @classmethod
    def _context_is_safe(cls, value: str) -> str:
        if value == ".":
            return value
        return _safe_relative_path(value, label="contextPath")

    @field_validator("manifest_path")
    @classmethod
    def _manifest_is_safe(cls, value: str) -> str:
        return _safe_relative_path(value, label="manifestPath")

    @field_validator("credential_secret_id")
    @classmethod
    def _credential_id_is_safe(cls, value: str) -> str:
        return _safe_id(value) if value else value

    @model_validator(mode="after")
    def _build_files_are_fixed(self) -> McpGitSource:
        if (
            self.dockerfile_path != "Dockerfile"
            or self.context_path != "."
            or self.manifest_path != "mcp-deploy.yaml"
        ):
            raise ValueError(
                "Git deployments require repository-root Dockerfile and mcp-deploy.yaml"
            )
        return self


class McpBrowserHeaderBinding(_StrictModel):
    secret_id: str = Field(alias="secretId")
    name: str = Field(min_length=1, max_length=128)
    value_prefix: str = Field(default="", alias="valuePrefix", max_length=64)

    @field_validator("secret_id")
    @classmethod
    def _secret_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("name")
    @classmethod
    def _header_name_is_safe(cls, value: str) -> str:
        if not _HEADER_NAME_RE.fullmatch(value) or value.lower() in _RESERVED_HEADERS:
            raise ValueError("browser Header name is invalid or reserved")
        return value


class McpDeploymentRegistration(_StrictModel):
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    expected_server_name: str = Field(default="", alias="expectedServerName", max_length=128)
    timeout_seconds: float = Field(default=30.0, alias="timeoutSeconds", ge=1.0, le=600.0)
    enabled: bool = True
    static_headers: dict[str, str] = Field(default_factory=dict, alias="staticHeaders", max_length=64)
    secret_bindings: list[McpBrowserHeaderBinding] = Field(
        default_factory=list,
        alias="secretBindings",
        max_length=32,
    )

    @model_validator(mode="after")
    def _headers_are_safe_and_unique(self) -> McpDeploymentRegistration:
        names: set[str] = set()
        for name, value in self.static_headers.items():
            lowered = name.lower()
            if (
                not _HEADER_NAME_RE.fullmatch(name)
                or lowered in _RESERVED_HEADERS
                or lowered in names
                or len(value) > 8192
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("static browser Headers are invalid")
            names.add(lowered)
        for binding in self.secret_bindings:
            lowered = binding.name.lower()
            if lowered in names:
                raise ValueError("browser Header names must be unique ignoring case")
            names.add(lowered)
        return self


class McpDeploymentUpsertRequest(_StrictModel):
    id: str
    source: McpOciSource | McpGitSource = Field(discriminator="type")
    manifest: McpDeploymentManifest | None = None
    registration: McpDeploymentRegistration
    expected_deployment_version: int = Field(alias="expectedDeploymentVersion", ge=0)
    expected_config_version: int = Field(alias="expectedConfigVersion", ge=0)

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @model_validator(mode="after")
    def _manifest_source_contract(self) -> McpDeploymentUpsertRequest:
        if self.source.type == "oci" and self.manifest is None:
            raise ValueError("OCI deployment requires an inline manifest")
        if self.source.type == "git" and self.manifest is not None:
            raise ValueError("Git deployment reads mcp-deploy.yaml from the pinned commit")
        return self


class McpDeploymentRef(_StrictModel):
    deployment_id: str = Field(alias="deploymentId")
    revision: str = Field(min_length=12, max_length=64)

    @field_validator("deployment_id")
    @classmethod
    def _deployment_id_is_safe(cls, value: str) -> str:
        return _safe_id(value)


class McpDeploymentRecord(_StrictModel):
    id: str
    source: McpOciSource | McpGitSource = Field(discriminator="type")
    manifest: McpDeploymentManifest
    registration: McpDeploymentRegistration
    deployment_version: int = Field(alias="deploymentVersion", ge=1)
    config_version: int = Field(alias="configVersion", ge=0)
    status: DeploymentStatus
    active_revision: str = Field(default="", alias="activeRevision")
    previous_revision: str = Field(default="", alias="previousRevision")
    active_image: str = Field(default="", alias="activeImage")
    previous_image: str = Field(default="", alias="previousImage")
    previous_source: McpOciSource | McpGitSource | None = Field(
        default=None,
        alias="previousSource",
    )
    previous_manifest: McpDeploymentManifest | None = Field(
        default=None,
        alias="previousManifest",
    )
    previous_registration: McpDeploymentRegistration | None = Field(
        default=None,
        alias="previousRegistration",
    )
    internal_port: int = Field(default=0, alias="internalPort", ge=0, le=65535)
    updated_at: str = Field(alias="updatedAt")
    last_error_code: str = Field(default="", alias="lastErrorCode", max_length=128)

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _safe_id(value)


class McpDeploymentJob(_StrictModel):
    id: str
    deployment_id: str = Field(alias="deploymentId")
    status: DeploymentJobStatus
    request: McpDeploymentUpsertRequest
    manifest: McpDeploymentManifest | None = None
    revision: str = Field(default="", max_length=64)
    image: str = Field(default="", max_length=1024)
    internal_port: int = Field(default=0, alias="internalPort", ge=0, le=65535)
    candidate_path: str = Field(default="", alias="candidatePath", max_length=512)
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    error_code: str = Field(default="", alias="errorCode", max_length=128)

    @field_validator("id", "deployment_id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _safe_id(value)


class McpDeploymentFile(_StrictModel):
    schema_version: Literal["1"] = Field(alias="schemaVersion")
    deployments: list[McpDeploymentRecord] = Field(default_factory=list, max_length=128)
    jobs: list[McpDeploymentJob] = Field(default_factory=list, max_length=512)


class McpDeploymentStore:
    """Atomic operational metadata store owned by the privileged Deployer."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._deployments, self._jobs = self._load()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list_deployments(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._deployments[item].model_dump(mode="json", by_alias=True)
                for item in sorted(self._deployments)
            ]

    def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            item = self._deployments.get(safe_id)
            return item.model_dump(mode="json", by_alias=True) if item else None

    def get_record(self, deployment_id: str) -> McpDeploymentRecord | None:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            item = self._deployments.get(safe_id)
            return item.model_copy(deep=True) if item else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        safe_id = _safe_id(job_id)
        with self._lock:
            item = self._jobs.get(safe_id)
            return item.model_dump(mode="json", by_alias=True) if item else None

    def get_job_record(self, job_id: str) -> McpDeploymentJob | None:
        safe_id = _safe_id(job_id)
        with self._lock:
            item = self._jobs.get(safe_id)
            return item.model_copy(deep=True) if item else None

    def active_job_records(self) -> list[McpDeploymentJob]:
        with self._lock:
            return [
                job.model_copy(deep=True)
                for job in self._jobs.values()
                if job.status not in _TERMINAL_JOB_STATES
            ]

    def has_deployment_or_job(self, deployment_id: str) -> bool:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            return safe_id in self._deployments or any(
                job.deployment_id == safe_id for job in self._jobs.values()
            )

    def require_not_busy(self, deployment_id: str) -> None:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            if any(
                job.deployment_id == safe_id and job.status not in _TERMINAL_JOB_STATES
                for job in self._jobs.values()
            ):
                raise McpDeploymentBusyError("MCP deployment has an active job")

    def create_job(self, request: McpDeploymentUpsertRequest) -> McpDeploymentJob:
        with self._lock:
            current = self._deployments.get(request.id)
            current_version = current.deployment_version if current else 0
            if request.expected_deployment_version != current_version:
                raise McpDeploymentVersionConflictError(
                    request.id,
                    request.expected_deployment_version,
                    current_version,
                )
            if any(
                job.deployment_id == request.id
                and job.status
                not in _TERMINAL_JOB_STATES
                for job in self._jobs.values()
            ):
                raise McpDeploymentBusyError("MCP deployment already has an active job")
            now = self.now()
            job = McpDeploymentJob.model_validate(
                {
                    "id": f"job_{uuid.uuid4().hex}",
                    "deploymentId": request.id,
                    "status": "queued",
                    "request": request,
                    "createdAt": now,
                    "updatedAt": now,
                }
            )
            self._jobs[job.id] = job
            self._save()
            return job.model_copy(deep=True)

    def update_job(self, job_id: str, **updates: Any) -> McpDeploymentJob:
        safe_id = _safe_id(job_id)
        with self._lock:
            current = self._jobs.get(safe_id)
            if current is None:
                raise KeyError(safe_id)
            payload = current.model_dump(mode="python")
            payload.update(updates)
            payload["updated_at"] = self.now()
            job = McpDeploymentJob.model_validate(payload)
            self._jobs[safe_id] = job
            self._save()
            return job.model_copy(deep=True)

    def commit_job(self, job_id: str, *, config_version: int) -> McpDeploymentRecord:
        safe_id = _safe_id(job_id)
        with self._lock:
            job = self._jobs.get(safe_id)
            if job is None:
                raise KeyError(safe_id)
            if job.status != "promoting" or job.manifest is None or not job.revision:
                raise ValueError("deployment job is not ready to commit")
            current = self._deployments.get(job.deployment_id)
            record = McpDeploymentRecord.model_validate(
                {
                    "id": job.deployment_id,
                    "source": job.request.source,
                    "manifest": job.manifest,
                    "registration": job.request.registration,
                    "deploymentVersion": (current.deployment_version + 1 if current else 1),
                    "configVersion": config_version,
                    "status": "running" if job.request.registration.enabled else "stopped",
                    "activeRevision": job.revision,
                    "previousRevision": current.active_revision if current else "",
                    "activeImage": job.image,
                    "previousImage": current.active_image if current else "",
                    "previousSource": current.source if current else None,
                    "previousManifest": current.manifest if current else None,
                    "previousRegistration": current.registration if current else None,
                    "internalPort": job.internal_port,
                    "updatedAt": self.now(),
                }
            )
            self._deployments[record.id] = record
            self._jobs[safe_id] = job.model_copy(
                update={"status": "running", "updated_at": self.now()}
            )
            self._save()
            return record.model_copy(deep=True)

    def update_deployment(self, deployment_id: str, **updates: Any) -> McpDeploymentRecord:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            current = self._deployments.get(safe_id)
            if current is None:
                raise KeyError(safe_id)
            payload = current.model_dump(mode="python")
            payload.update(updates)
            payload["updated_at"] = self.now()
            payload["deployment_version"] = current.deployment_version + 1
            record = McpDeploymentRecord.model_validate(payload)
            self._deployments[safe_id] = record
            self._save()
            return record.model_copy(deep=True)

    def delete_deployment(self, deployment_id: str, expected_version: int) -> McpDeploymentRecord:
        safe_id = _safe_id(deployment_id)
        with self._lock:
            current = self._deployments.get(safe_id)
            if current is None:
                raise KeyError(safe_id)
            if current.deployment_version != expected_version:
                raise McpDeploymentVersionConflictError(
                    safe_id,
                    expected_version,
                    current.deployment_version,
                )
            removed = self._deployments.pop(safe_id)
            self._save()
            return removed.model_copy(deep=True)

    def _load(self) -> tuple[dict[str, McpDeploymentRecord], dict[str, McpDeploymentJob]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = McpDeploymentFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            deployments = {item.id: item for item in payload.deployments}
            jobs = {item.id: item for item in payload.jobs}
            if len(deployments) != len(payload.deployments) or len(jobs) != len(payload.jobs):
                raise ValueError("duplicate deployment or job identifier")
            return deployments, jobs
        except (OSError, ValueError, ValidationError) as exc:
            raise McpDeploymentError("MCP deployment metadata is invalid") from exc

    def _save(self) -> None:
        payload = McpDeploymentFile.model_validate(
            {
                "schemaVersion": _SCHEMA_VERSION,
                "deployments": [self._deployments[item] for item in sorted(self._deployments)],
                "jobs": [self._jobs[item] for item in sorted(self._jobs)][-512:],
            }
        ).model_dump(mode="json", by_alias=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                temp.chmod(0o600)
            except OSError:
                pass
            temp.replace(self.path)
        except OSError as exc:
            raise McpDeploymentError("MCP deployment metadata is unavailable") from exc
        finally:
            temp.unlink(missing_ok=True)


class McpDeployerClient:
    """Small authenticated HTTP-over-Unix-Socket client used by the Gateway."""

    def __init__(self, socket_path: Path, token: str, timeout_seconds: float = 30.0) -> None:
        self.socket_path = Path(socket_path)
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        headers = {"X-ActionDesign-Deployer-Token": self.token}
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://mcp-deployer",
                timeout=timeout_seconds or self.timeout_seconds,
            ) as client:
                response = await client.request(method, path, headers=headers, json=json_body)
        except (httpx.HTTPError, OSError) as exc:
            raise McpDeployerUnavailableError("MCP Deployer is unavailable") from exc
        if response.status_code == 404:
            raise KeyError(path)
        if response.status_code == 409:
            detail = response.json().get("detail", {})
            code = detail.get("code", "") if isinstance(detail, dict) else ""
            if code == "MCP_DEPLOYMENT_BUSY":
                raise McpDeploymentBusyError("MCP deployment is busy")
            raise McpDeploymentVersionConflictError(
                str(detail.get("id", "deployment")),
                int(detail.get("expected", 0)),
                int(detail.get("current", 0)),
            )
        if response.status_code >= 400:
            raise McpDeploymentError(f"MCP Deployer rejected request ({response.status_code})")
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise McpDeploymentError("MCP Deployer returned an invalid response")
        return payload


def deployment_revision(source: McpOciSource | McpGitSource, manifest: McpDeploymentManifest) -> str:
    canonical = json.dumps(
        {
            "source": source.model_dump(mode="json", by_alias=True),
            "manifest": manifest.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "McpDeployerClient",
    "McpDeployerUnavailableError",
    "McpDeploymentBusyError",
    "McpDeploymentError",
    "McpDeploymentFile",
    "McpDeploymentJob",
    "McpDeploymentManifest",
    "McpDeploymentRecord",
    "McpDeploymentRef",
    "McpDeploymentRegistration",
    "McpDeploymentSecretBinding",
    "McpDeploymentStore",
    "McpDeploymentUpsertRequest",
    "McpDeploymentVersionConflictError",
    "deployment_revision",
]
