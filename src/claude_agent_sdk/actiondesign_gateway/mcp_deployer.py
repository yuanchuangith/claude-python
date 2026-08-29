from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from pydantic import ValidationError

from .mcp_deployment import (
    McpDeploymentBusyError,
    McpDeploymentError,
    McpDeploymentJob,
    McpDeploymentManifest,
    McpDeploymentRecord,
    McpDeploymentRegistration,
    McpDeploymentStore,
    McpDeploymentUpsertRequest,
    McpDeploymentVersionConflictError,
    deployment_revision,
)
from .profile_store import FileProfileSecretStore, KeyringProfileSecretStore
from .secret_store import ManagedSecretStore, ManagedSecretStoreError


_TERMINAL_JOBS = {"running", "failed", "rolled_back", "rollback_failed"}
_SAFE_CONTAINER_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_CONTEXT_BYTES = 100 * 1024 * 1024
_MAX_CONTEXT_FILES = 10_000
_MAX_LOG_BYTES = 10 * 1024 * 1024


class McpDeployerSettings:
    def __init__(self) -> None:
        self.token = os.environ.get("ACTIONDESIGN_MCP_DEPLOYER_TOKEN", "")
        self.state_path = Path(
            os.environ.get(
                "ACTIONDESIGN_MCP_DEPLOYMENT_STORE_PATH",
                "/var/lib/actiondesign-mcp/deployments.json",
            )
        )
        self.work_root = Path(
            os.environ.get("ACTIONDESIGN_MCP_DEPLOY_WORK_ROOT", "/var/lib/actiondesign-mcp/work")
        )
        self.log_root = Path(
            os.environ.get("ACTIONDESIGN_MCP_DEPLOY_LOG_ROOT", "/var/log/actiondesign-mcp")
        )
        self.nginx_root = Path(
            os.environ.get(
                "ACTIONDESIGN_MCP_NGINX_SNIPPET_ROOT",
                "/etc/nginx/actiondesign-mcp-runtime",
            )
        )
        self.public_base_url = os.environ.get(
            "ACTIONDESIGN_MCP_PUBLIC_BASE_URL",
            "https://43-135-137-212.sslip.io",
        ).rstrip("/")
        self.origins = tuple(
            item.strip()
            for item in os.environ.get("ACTIONDESIGN_AGENT_ALLOW_ORIGINS", "").split(",")
            if item.strip()
        )
        self.allowed_git_hosts = frozenset(
            item.lower()
            for item in os.environ.get(
                "ACTIONDESIGN_MCP_DEPLOY_ALLOWED_GIT_HOSTS",
                "github.com,gitlab.com",
            ).split(",")
            if item
        )
        self.allowed_registries = frozenset(
            item.lower()
            for item in os.environ.get(
                "ACTIONDESIGN_MCP_DEPLOY_ALLOWED_REGISTRIES",
                "docker.io,ghcr.io",
            ).split(",")
            if item
        )
        self.max_http_runtimes = int(
            os.environ.get("ACTIONDESIGN_MCP_DEPLOY_MAX_HTTP_RUNTIMES", "2")
        )
        self.build_timeout_seconds = int(
            os.environ.get("ACTIONDESIGN_MCP_DEPLOY_BUILD_TIMEOUT_SECONDS", "900")
        )
        self.browser_validation_timeout_seconds = int(
            os.environ.get("ACTIONDESIGN_MCP_BROWSER_VALIDATION_TIMEOUT_SECONDS", "300")
        )
        self.stdio_socket_path = Path(
            os.environ.get(
                "ACTIONDESIGN_MCP_STDIO_BROKER_SOCKET",
                "/run/actiondesign-mcp-deployer/stdio.sock",
            )
        )
        self.stdio_allowed_uid = int(
            os.environ.get("ACTIONDESIGN_MCP_STDIO_ALLOWED_UID", "-1")
        )
        self.stdio_socket_gid = int(
            os.environ.get("ACTIONDESIGN_MCP_STDIO_SOCKET_GID", "-1")
        )
        self.stdio_max_session_seconds = int(
            os.environ.get("ACTIONDESIGN_MCP_STDIO_MAX_SESSION_SECONDS", "600")
        )
        managed_secret_path = Path(
            os.environ.get(
                "ACTIONDESIGN_MANAGED_SECRET_STORE_PATH",
                "config/actiondesign-managed-secrets.json",
            )
        )
        backend_name = os.environ.get(
            "ACTIONDESIGN_PROFILE_SECRET_STORE_BACKEND",
            "keyring",
        )
        if backend_name == "file":
            backend = FileProfileSecretStore(
                Path(
                    os.environ.get(
                        "ACTIONDESIGN_PROFILE_SECRET_STORE_PATH",
                        "config/actiondesign-api-profile-secrets.json",
                    )
                )
            )
        else:
            backend = KeyringProfileSecretStore()
        self.secret_store = ManagedSecretStore(managed_secret_path, backend)
        if len(self.token) < 32:
            raise ValueError("ACTIONDESIGN_MCP_DEPLOYER_TOKEN must contain at least 32 characters")
        if not self.origins or "*" in self.origins:
            raise ValueError("MCP Deployer requires explicit browser Origins")
        for origin in self.origins:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.hostname
                or parsed_origin.username
                or parsed_origin.password
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
                or any(
                    ord(character) <= 32 or ord(character) == 127
                    for character in origin
                )
            ):
                raise ValueError("MCP Deployer requires valid exact browser Origins")
        parsed_public = urlsplit(self.public_base_url)
        if parsed_public.scheme != "https" or not parsed_public.hostname:
            raise ValueError("MCP public base URL must use absolute HTTPS")


class DeployerBackend(Protocol):
    async def prepare(self, job: McpDeploymentJob) -> dict[str, Any]: ...
    async def promote(self, job: McpDeploymentJob) -> dict[str, Any]: ...
    async def rollback_job(self, job: McpDeploymentJob) -> None: ...
    async def commit(self, job: McpDeploymentJob, previous: McpDeploymentRecord | None) -> None: ...
    async def set_enabled(self, record: McpDeploymentRecord, enabled: bool) -> dict[str, Any]: ...
    async def delete(self, record: McpDeploymentRecord) -> None: ...
    async def rollback_deployment(self, record: McpDeploymentRecord) -> dict[str, Any]: ...
    def logs(self, deployment_id: str) -> str: ...


class DockerNginxDeployerBackend:
    def __init__(self, settings: McpDeployerSettings) -> None:
        self.settings = settings
        self.store: McpDeploymentStore | None = None
        self._build_lock = asyncio.Lock()
        self.settings.work_root.mkdir(parents=True, exist_ok=True)
        self.settings.log_root.mkdir(parents=True, exist_ok=True)
        self.settings.nginx_root.mkdir(parents=True, exist_ok=True)

    def attach_store(self, store: McpDeploymentStore) -> None:
        self.store = store

    async def prepare(self, job: McpDeploymentJob) -> dict[str, Any]:
        async with self._build_lock:
            manifest, image = await asyncio.to_thread(self._prepare_image, job)
        revision = deployment_revision(job.request.source, manifest)
        previous = self.store.get_record(job.deployment_id) if self.store else None
        if self.store:
            self.store.update_job(
                job.id,
                status="starting",
                manifest=manifest,
                revision=revision,
                image=image,
            )
        if manifest.transport == "streamable_http":
            if previous and previous.status == "running":
                await asyncio.to_thread(self._stop_container, previous)
            try:
                port = await asyncio.to_thread(
                    self._start_http_container,
                    job.deployment_id,
                    revision,
                    image,
                    manifest,
                )
                await self._wait_healthy(port, manifest)
                candidate_path = f"/mcp-candidate/{secrets.token_urlsafe(24)}/mcp"
                await asyncio.to_thread(
                    self._write_candidate_route,
                    job.deployment_id,
                    candidate_path,
                    port,
                    manifest,
                )
            except Exception:
                await asyncio.to_thread(
                    self._capture_container_logs,
                    job.deployment_id,
                    revision,
                    manifest,
                    job.request.registration,
                )
                await asyncio.to_thread(
                    self._remove_container,
                    job.deployment_id,
                    revision,
                )
                if previous and previous.status == "running":
                    previous_port = await asyncio.to_thread(
                        self._start_existing_container,
                        previous,
                    )
                    if previous.manifest.transport == "streamable_http":
                        await self._wait_healthy(previous_port, previous.manifest)
                        await asyncio.to_thread(
                            self._write_stable_route,
                            previous.id,
                            previous_port,
                            previous.manifest,
                        )
                raise
            return {
                "manifest": manifest,
                "revision": revision,
                "image": image,
                "internal_port": port,
                "candidate_path": candidate_path,
                "status": "awaiting_browser_validation",
            }
        return {
            "manifest": manifest,
            "revision": revision,
            "image": image,
            "internal_port": 0,
            "candidate_path": "",
            "status": "awaiting_browser_validation",
        }

    async def promote(self, job: McpDeploymentJob) -> dict[str, Any]:
        if job.manifest is None:
            raise ValueError("deployment manifest is unavailable")
        if job.manifest.transport == "streamable_http":
            await asyncio.to_thread(
                self._write_stable_route,
                job.deployment_id,
                job.internal_port,
                job.manifest,
            )
        return {"status": "promoting"}

    async def rollback_job(self, job: McpDeploymentJob) -> None:
        previous = self.store.get_record(job.deployment_id) if self.store else None
        await asyncio.to_thread(self._remove_candidate_route, job.deployment_id)
        await asyncio.to_thread(self._remove_container, job.deployment_id, job.revision)
        if previous and previous.status == "running":
            previous_port = await asyncio.to_thread(self._start_existing_container, previous)
            if previous.manifest.transport == "streamable_http":
                await self._wait_healthy(previous_port, previous.manifest)
                await asyncio.to_thread(
                    self._write_stable_route,
                    previous.id,
                    previous_port,
                    previous.manifest,
                )
        elif previous and previous.manifest.transport == "streamable_http":
            await asyncio.to_thread(self._remove_stable_route, previous.id)
        elif previous is None:
            await asyncio.to_thread(self._remove_stable_route, job.deployment_id)

    async def commit(self, job: McpDeploymentJob, previous: McpDeploymentRecord | None) -> None:
        await asyncio.to_thread(self._remove_candidate_route, job.deployment_id)
        if (
            job.manifest is not None
            and job.manifest.transport == "streamable_http"
            and not job.request.registration.enabled
        ):
            await asyncio.to_thread(self._remove_stable_route, job.deployment_id)
            await asyncio.to_thread(
                self._run,
                [
                    "docker",
                    "stop",
                    "--time",
                    "10",
                    self._container_name(job.deployment_id, job.revision),
                ],
                check=False,
                deployment_id=job.deployment_id,
            )
        if previous and previous.active_revision != job.revision:
            await asyncio.to_thread(
                self._remove_container,
                previous.id,
                previous.active_revision,
            )
        await asyncio.to_thread(self._cleanup_labeled_images, job.deployment_id, job.image, previous)

    async def set_enabled(self, record: McpDeploymentRecord, enabled: bool) -> dict[str, Any]:
        if record.manifest.transport == "stdio":
            return {"enabled": enabled, "internalPort": 0}
        if enabled:
            port = await asyncio.to_thread(self._start_existing_container, record)
            await self._wait_healthy(port, record.manifest)
            await asyncio.to_thread(self._write_stable_route, record.id, port, record.manifest)
            return {"enabled": True, "internalPort": port}
        await asyncio.to_thread(self._stop_container, record)
        await asyncio.to_thread(self._remove_stable_route, record.id)
        return {"enabled": False, "internalPort": record.internal_port}

    async def delete(self, record: McpDeploymentRecord) -> None:
        await asyncio.to_thread(self._remove_stable_route, record.id)
        await asyncio.to_thread(self._remove_candidate_route, record.id)
        await asyncio.to_thread(self._remove_container, record.id, record.active_revision)
        if record.previous_revision:
            await asyncio.to_thread(self._remove_container, record.id, record.previous_revision)
        await asyncio.to_thread(self._remove_deployment_images, record)

    async def rollback_deployment(self, record: McpDeploymentRecord) -> dict[str, Any]:
        if (
            not record.previous_revision
            or not record.previous_image
            or record.previous_manifest is None
            or record.previous_source is None
            or record.previous_registration is None
        ):
            raise ValueError("deployment has no previous revision")
        manifest = record.previous_manifest
        port = 0
        await asyncio.to_thread(self._stop_container, record)
        try:
            if manifest.transport == "streamable_http" and record.status == "running":
                port = await asyncio.to_thread(
                    self._start_http_container,
                    record.id,
                    record.previous_revision,
                    record.previous_image,
                    manifest,
                )
                await self._wait_healthy(port, manifest)
                await asyncio.to_thread(
                    self._write_stable_route,
                    record.id,
                    port,
                    manifest,
                )
        except Exception:
            if (
                record.manifest.transport == "streamable_http"
                and record.status == "running"
            ):
                current_port = await asyncio.to_thread(
                    self._start_existing_container,
                    record,
                )
                await self._wait_healthy(current_port, record.manifest)
                await asyncio.to_thread(
                    self._write_stable_route,
                    record.id,
                    current_port,
                    record.manifest,
                )
            raise
        registration = record.previous_registration.model_copy(
            update={"enabled": record.status == "running"}
        )
        return {
            "active_revision": record.previous_revision,
            "active_image": record.previous_image,
            "previous_revision": record.active_revision,
            "previous_image": record.active_image,
            "source": record.previous_source,
            "manifest": record.previous_manifest,
            "registration": registration,
            "previous_source": record.source,
            "previous_manifest": record.manifest,
            "previous_registration": record.registration,
            "internal_port": port,
        }

    def logs(self, deployment_id: str) -> str:
        if self.store:
            record = self.store.get_record(deployment_id)
            if record and record.manifest.transport == "streamable_http":
                self._capture_container_logs(
                    record.id,
                    record.active_revision,
                    record.manifest,
                    record.registration,
                )
        path = self._log_path(deployment_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-_MAX_LOG_BYTES:]

    def _capture_container_logs(
        self,
        deployment_id: str,
        revision: str,
        manifest: McpDeploymentManifest,
        registration: McpDeploymentRegistration | None = None,
    ) -> None:
        if manifest.transport != "streamable_http" or not revision:
            return
        redact: list[str] = []
        try:
            self.settings.secret_store.reload()
            for binding in manifest.environment:
                value = self.settings.secret_store.resolve_value(binding.secret_id)
                redact.append(f"{binding.value_prefix}{value}")
            if registration:
                for binding in registration.secret_bindings:
                    value = self.settings.secret_store.resolve_value(binding.secret_id)
                    redact.append(f"{binding.value_prefix}{value}")
        except Exception:
            pass
        self._run(
            [
                "docker",
                "logs",
                "--tail",
                "500",
                self._container_name(deployment_id, revision),
            ],
            check=False,
            deployment_id=deployment_id,
            redact=tuple(redact),
        )

    def _prepare_image(self, job: McpDeploymentJob) -> tuple[McpDeploymentManifest, str]:
        self.settings.secret_store.reload()
        source = job.request.source
        if source.type == "oci":
            self._require_registry_allowed(source.image)
            self._pull_image(source.image, source.credential_secret_id, job.deployment_id)
            assert job.request.manifest is not None
            manifest = job.request.manifest
            image = source.image
        else:
            self._require_git_allowed(source.repository)
            checkout = self.settings.work_root / job.id / "source"
            checkout.parent.mkdir(parents=True, exist_ok=False)
            try:
                self._clone_git(source, checkout, job.deployment_id)
                manifest_path = (checkout / source.manifest_path).resolve()
                if checkout.resolve() not in manifest_path.parents:
                    raise ValueError("manifest path escapes checkout")
                raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                manifest = McpDeploymentManifest.model_validate(raw)
                context = (checkout / source.context_path).resolve()
                dockerfile = (checkout / source.dockerfile_path).resolve()
                self._validate_build_context(checkout, context, dockerfile)
                self._normalize_build_context_modes(context)
                if self.store:
                    self.store.update_job(job.id, status="building", manifest=manifest)
                image = f"actiondesign-mcp/{self._safe_name(job.deployment_id)}:{job.id[-12:]}"
                self._run(
                    [
                        "docker", "buildx", "build", "--builder", "actiondesign-mcp-builder",
                        "--load", "--network", "default",
                        "--label", f"com.actiondesign.mcp.id={job.deployment_id}",
                        "--label", "com.centurylinklabs.watchtower.enable=false",
                        "-t", image, "-f", str(dockerfile), str(context),
                    ],
                    timeout=self.settings.build_timeout_seconds,
                    deployment_id=job.deployment_id,
                )
            finally:
                shutil.rmtree(checkout.parent, ignore_errors=True)
        self._validate_image_user(image, job.deployment_id)
        return manifest, image

    def _clone_git(self, source: Any, checkout: Path, deployment_id: str) -> None:
        env = dict(os.environ)
        credential_value = ""
        askpass: Path | None = None
        if source.credential_secret_id:
            if urlsplit(source.repository).scheme != "https":
                raise ValueError("private Git credentials require an HTTPS repository")
            self.settings.secret_store.require_exposure(
                source.credential_secret_id,
                {"deployment_only"},
            )
            credential_value = self.settings.secret_store.resolve_value(source.credential_secret_id)
            credential = json.loads(credential_value)
            if not isinstance(credential, dict) or not credential.get("password"):
                raise ValueError("Git credential Secret must contain username/password JSON")
            env["ACTIONDESIGN_GIT_USERNAME"] = str(credential.get("username") or "oauth2")
            env["ACTIONDESIGN_GIT_PASSWORD"] = str(credential["password"])
            askpass = checkout.parent / "git-askpass.sh"
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in *sername*) printf '%s' \"$ACTIONDESIGN_GIT_USERNAME\";; *) printf '%s' \"$ACTIONDESIGN_GIT_PASSWORD\";; esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
        checkout.mkdir(parents=True)
        try:
            self._run(["git", "init", str(checkout)], deployment_id=deployment_id, env=env)
            self._run(
                ["git", "-C", str(checkout), "remote", "add", "origin", source.repository],
                deployment_id=deployment_id,
                env=env,
            )
            self._run(
                ["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", source.commit],
                timeout=300,
                deployment_id=deployment_id,
                env=env,
                redact=(credential_value,),
            )
            self._run(
                ["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"],
                deployment_id=deployment_id,
                env=env,
            )
            actual = self._run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                deployment_id=deployment_id,
                env=env,
            ).strip()
            if actual != source.commit:
                raise ValueError("Git checkout does not match pinned commit")
        finally:
            if askpass:
                askpass.unlink(missing_ok=True)
            env.pop("ACTIONDESIGN_GIT_PASSWORD", None)

    def _pull_image(self, image: str, credential_id: str, deployment_id: str) -> None:
        env = dict(os.environ)
        docker_config: Path | None = None
        credential_value = ""
        if credential_id:
            self.settings.secret_store.require_exposure(credential_id, {"deployment_only"})
            credential_value = self.settings.secret_store.resolve_value(credential_id)
            credential = json.loads(credential_value)
            if not isinstance(credential, dict) or not credential.get("username") or not credential.get("password"):
                raise ValueError("Registry credential Secret must contain username/password JSON")
            registry = self._registry(image)
            docker_config = Path(tempfile.mkdtemp(prefix="actiondesign-docker-config-"))
            auth = base64.b64encode(
                f"{credential['username']}:{credential['password']}".encode()
            ).decode()
            (docker_config / "config.json").write_text(
                json.dumps({"auths": {registry: {"auth": auth}}}),
                encoding="utf-8",
            )
            env["DOCKER_CONFIG"] = str(docker_config)
        try:
            self._run(
                ["docker", "pull", image],
                timeout=300,
                deployment_id=deployment_id,
                env=env,
                redact=(credential_value,),
            )
        finally:
            if docker_config:
                shutil.rmtree(docker_config, ignore_errors=True)

    def _validate_build_context(self, checkout: Path, context: Path, dockerfile: Path) -> None:
        root = checkout.resolve()
        if not context.is_dir() or root not in context.parents and context != root:
            raise ValueError("build context is invalid")
        if not dockerfile.is_file() or root not in dockerfile.parents:
            raise ValueError("Dockerfile is invalid")
        count = 0
        size = 0
        for path in context.rglob("*"):
            count += 1
            if count > _MAX_CONTEXT_FILES:
                raise ValueError("Git build context contains too many files")
            if path.is_symlink():
                resolved = path.resolve()
                if root not in resolved.parents and resolved != root:
                    raise ValueError("Git build context symlink escapes checkout")
            elif path.is_file():
                size += path.stat().st_size
                if size > _MAX_CONTEXT_BYTES:
                    raise ValueError("Git build context exceeds 100 MiB")

    @staticmethod
    def _normalize_build_context_modes(context: Path) -> None:
        # The hardened service uses UMask=0077. Git checkout honors that umask,
        # while Docker COPY preserves source modes; without this normalization a
        # required non-root image cannot read ordinary checked-out files.
        for path in (context, *context.rglob("*")):
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                path.chmod(
                    mode
                    | stat.S_IRGRP
                    | stat.S_IROTH
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )
            elif path.is_file():
                path.chmod(mode | stat.S_IRGRP | stat.S_IROTH)

    def _validate_image_user(self, image: str, deployment_id: str) -> None:
        user = self._run(
            ["docker", "image", "inspect", "--format", "{{.Config.User}}", image],
            deployment_id=deployment_id,
        ).strip()
        if not user or user.lower() == "root" or user.split(":", 1)[0] == "0":
            raise ValueError("MCP image must declare a non-root USER")

    def _start_http_container(
        self,
        deployment_id: str,
        revision: str,
        image: str,
        manifest: McpDeploymentManifest,
    ) -> int:
        assert manifest.http is not None
        self.settings.secret_store.reload()
        active = self._run(
            ["docker", "ps", "-q", "--filter", "label=com.actiondesign.mcp.transport=http"],
            deployment_id=deployment_id,
        ).splitlines()
        if len([item for item in active if item]) >= self.settings.max_http_runtimes:
            raise McpDeploymentBusyError("maximum active HTTP MCP runtime count reached")
        name = self._container_name(deployment_id, revision)
        self._run(["docker", "rm", "-f", name], check=False, deployment_id=deployment_id)
        args = [
            "docker", "run", "-d", "--name", name,
            "--label", f"com.actiondesign.mcp.id={deployment_id}",
            "--label", f"com.actiondesign.mcp.revision={revision}",
            "--label", "com.actiondesign.mcp.transport=http",
            "--label", "com.centurylinklabs.watchtower.enable=false",
            "--restart", "unless-stopped", "--network", "bridge",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(manifest.resources.pids),
            "--cpus", str(manifest.resources.cpu),
            "--memory", f"{manifest.resources.memory_mib}m",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--tmpfs", "/run:rw,noexec,nosuid,size=16m",
            "-p", f"127.0.0.1::{manifest.http.container_port}",
        ]
        secret_values: list[str] = []
        container_env = dict(os.environ)
        for binding in manifest.environment:
            self.settings.secret_store.require_exposure(
                binding.secret_id,
                {"deployment_only"},
            )
            value = self.settings.secret_store.resolve_value(binding.secret_id)
            expanded = f"{binding.value_prefix}{value}"
            secret_values.append(expanded)
            container_env[binding.name] = expanded
            args.extend(["-e", binding.name])
        args.append(image)
        try:
            self._run(
                args,
                deployment_id=deployment_id,
                env=container_env,
                redact=tuple(secret_values),
            )
        finally:
            for binding in manifest.environment:
                container_env.pop(binding.name, None)
        output = self._run(
            ["docker", "port", name, f"{manifest.http.container_port}/tcp"],
            deployment_id=deployment_id,
        ).strip()
        port = int(output.rsplit(":", 1)[1])
        return port

    async def _wait_healthy(self, port: int, manifest: McpDeploymentManifest) -> None:
        assert manifest.http is not None
        deadline = asyncio.get_running_loop().time() + manifest.startup_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                if manifest.http.health_check.type == "tcp":
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    return
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(
                        f"http://127.0.0.1:{port}{manifest.http.health_check.path}"
                    )
                if 200 <= response.status_code < 300:
                    return
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(1)
        raise TimeoutError("MCP container readiness timed out") from last_error

    def _start_existing_container(self, record: McpDeploymentRecord) -> int:
        if record.manifest.transport == "stdio":
            return 0
        name = self._container_name(record.id, record.active_revision)
        return_code, _output = self._run_result(
            ["docker", "container", "inspect", name],
            check=False,
            deployment_id=record.id,
        )
        if return_code == 0:
            self._run(["docker", "start", name], deployment_id=record.id)
            assert record.manifest.http is not None
            output = self._run(
                ["docker", "port", name, f"{record.manifest.http.container_port}/tcp"],
                deployment_id=record.id,
            ).strip()
            return int(output.rsplit(":", 1)[1])
        return self._start_http_container(
            record.id,
            record.active_revision,
            record.active_image,
            record.manifest,
        )

    def _stop_container(self, record: McpDeploymentRecord) -> None:
        if record.manifest.transport == "streamable_http" and record.active_revision:
            self._run(
                ["docker", "stop", "--time", "10", self._container_name(record.id, record.active_revision)],
                check=False,
                deployment_id=record.id,
            )

    def _remove_container(self, deployment_id: str, revision: str) -> None:
        if revision:
            self._run(
                ["docker", "rm", "-f", self._container_name(deployment_id, revision)],
                check=False,
                deployment_id=deployment_id,
            )

    def _write_candidate_route(
        self,
        deployment_id: str,
        path: str,
        port: int,
        manifest: McpDeploymentManifest,
    ) -> None:
        assert manifest.http is not None
        content = self._location(path, port, manifest.http.mcp_path, access_log=False)
        self._write_nginx_file(self._candidate_file(deployment_id), content)

    def _write_stable_route(
        self,
        deployment_id: str,
        port: int,
        manifest: McpDeploymentManifest,
    ) -> None:
        assert manifest.http is not None
        path = f"/mcp-runtime/{deployment_id}/mcp"
        content = self._location(path, port, manifest.http.mcp_path, access_log=True)
        self._write_nginx_file(self._stable_file(deployment_id), content)

    def _location(self, public_path: str, port: int, upstream_path: str, *, access_log: bool) -> str:
        origin_rules = "\n".join(
            f'    if ($http_origin = "{origin}") {{ set $actiondesign_mcp_origin $http_origin; }}'
            for origin in self.settings.origins
        )
        return f"""location = {public_path} {{
    set $actiondesign_mcp_origin "";
{origin_rules}
    {'access_log off;' if not access_log else ''}
    if ($request_method = OPTIONS) {{ return 204; }}
    limit_except GET POST DELETE OPTIONS {{ deny all; }}
    proxy_pass http://127.0.0.1:{port}{upstream_path};
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_hide_header Access-Control-Allow-Origin;
    proxy_hide_header Access-Control-Allow-Headers;
    proxy_hide_header Access-Control-Expose-Headers;
    add_header Access-Control-Allow-Origin $actiondesign_mcp_origin always;
    add_header Access-Control-Allow-Methods "GET, POST, DELETE, OPTIONS" always;
    add_header Access-Control-Allow-Headers $http_access_control_request_headers always;
    add_header Access-Control-Expose-Headers "Mcp-Session-Id" always;
    add_header Cache-Control "no-store" always;
}}
"""

    def _write_nginx_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(path)
        try:
            self._run(["nginx", "-t"], deployment_id="nginx")
            self._run(["nginx", "-s", "reload"], deployment_id="nginx")
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous, encoding="utf-8")
            raise

    def _remove_candidate_route(self, deployment_id: str) -> None:
        self._remove_nginx_file(self._candidate_file(deployment_id))

    def _remove_stable_route(self, deployment_id: str) -> None:
        self._remove_nginx_file(self._stable_file(deployment_id))

    def _remove_nginx_file(self, path: Path) -> None:
        if not path.exists():
            return
        previous = path.read_text(encoding="utf-8")
        path.unlink()
        try:
            self._run(["nginx", "-t"], deployment_id="nginx")
            self._run(["nginx", "-s", "reload"], deployment_id="nginx")
        except Exception:
            path.write_text(previous, encoding="utf-8")
            raise

    def _cleanup_labeled_images(
        self,
        deployment_id: str,
        active_image: str,
        previous: McpDeploymentRecord | None,
    ) -> None:
        keep = {active_image}
        if previous and previous.active_image:
            keep.add(previous.active_image)
        images = self._run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", f"label=com.actiondesign.mcp.id={deployment_id}"],
            deployment_id=deployment_id,
        ).splitlines()
        for image in images:
            if image and image not in keep:
                self._run(["docker", "image", "rm", image], check=False, deployment_id=deployment_id)

    def _remove_deployment_images(self, record: McpDeploymentRecord) -> None:
        for image in {record.active_image, record.previous_image}:
            if image and image.startswith("actiondesign-mcp/"):
                self._run(["docker", "image", "rm", image], check=False, deployment_id=record.id)

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        check: bool = True,
        deployment_id: str,
        env: dict[str, str] | None = None,
        redact: Sequence[str] = (),
    ) -> str:
        _return_code, output = self._run_result(
            args,
            timeout=timeout,
            check=check,
            deployment_id=deployment_id,
            env=env,
            redact=redact,
        )
        return output

    def _run_result(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        check: bool = True,
        deployment_id: str,
        env: dict[str, str] | None = None,
        redact: Sequence[str] = (),
    ) -> tuple[int, str]:
        completed = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
            check=False,
        )
        output = completed.stdout[-_MAX_LOG_BYTES:]
        for value in redact:
            if value:
                output = output.replace(value, "[REDACTED]")
        self._append_log(deployment_id, output)
        if check and completed.returncode != 0:
            raise McpDeploymentError(f"deployment command failed ({args[0]})")
        return completed.returncode, output

    def _append_log(self, deployment_id: str, text: str) -> None:
        if not text:
            return
        path = self._log_path(deployment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        path.write_text((existing + text)[-_MAX_LOG_BYTES:], encoding="utf-8")
        path.chmod(0o600)

    def _require_git_allowed(self, repository: str) -> None:
        host = (urlsplit(repository).hostname or "").lower()
        if host not in self.settings.allowed_git_hosts:
            raise ValueError("Git repository host is not allowlisted")

    def _require_registry_allowed(self, image: str) -> None:
        if self._registry(image) not in self.settings.allowed_registries:
            raise ValueError("OCI registry is not allowlisted")

    @staticmethod
    def _registry(image: str) -> str:
        first = image.split("/", 1)[0].lower()
        return first if "." in first or ":" in first or first == "localhost" else "docker.io"

    @staticmethod
    def _safe_name(value: str) -> str:
        return _SAFE_CONTAINER_RE.sub("-", value.lower())[:80]

    def _container_name(self, deployment_id: str, revision: str) -> str:
        return f"actiondesign-mcp-{self._safe_name(deployment_id)}-{revision[:12]}"

    def _stable_file(self, deployment_id: str) -> Path:
        return self.settings.nginx_root / f"{self._safe_name(deployment_id)}.conf"

    def _candidate_file(self, deployment_id: str) -> Path:
        return self.settings.nginx_root / f"{self._safe_name(deployment_id)}.candidate.conf"

    def _log_path(self, deployment_id: str) -> Path:
        return self.settings.log_root / f"{self._safe_name(deployment_id)}.log"


class McpStdioBroker:
    def __init__(
        self,
        settings: McpDeployerSettings,
        store: McpDeploymentStore,
    ) -> None:
        self.settings = settings
        self.store = store
        self.server: asyncio.AbstractServer | None = None
        self._semaphore = asyncio.Semaphore(1)

    async def start(self) -> None:
        self.settings.stdio_socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.stdio_socket_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.settings.stdio_socket_path),
        )
        self.settings.stdio_socket_path.chmod(0o660)
        if self.settings.stdio_socket_gid >= 0:
            os.chown(self.settings.stdio_socket_path, -1, self.settings.stdio_socket_gid)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.settings.stdio_socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        process: asyncio.subprocess.Process | None = None
        container_name = ""
        try:
            if self.settings.stdio_allowed_uid >= 0 and hasattr(socket, "SO_PEERCRED"):
                sock = writer.get_extra_info("socket")
                credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
                if peer_uid != self.settings.stdio_allowed_uid:
                    raise PermissionError(f"stdio bridge peer uid is not allowed ({peer_pid})")
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(line)
            deployment_id = str(request.get("deploymentId") or "")
            revision = str(request.get("revision") or "")
            record = self.store.get_record(deployment_id)
            job_id = str(request.get("jobId") or "")
            job = self.store.get_job_record(job_id) if job_id else None
            if job is not None and job.revision != revision:
                job = None
            if record and record.active_revision == revision:
                manifest = record.manifest
                image = record.active_image
            elif job and job.manifest is not None:
                manifest = job.manifest
                image = job.image
            else:
                raise KeyError("stdio deployment revision is unavailable")
            if manifest.transport != "stdio" or manifest.stdio is None:
                raise ValueError("deployment is not stdio")
            self.settings.secret_store.reload()
            container_name = (
                f"actiondesign-mcp-stdio-"
                f"{DockerNginxDeployerBackend._safe_name(deployment_id)}-"
                f"{secrets.token_hex(6)}"
            )
            args = [
                "docker", "run", "--rm", "-i",
                "--name",
                container_name,
                "--label", f"com.actiondesign.mcp.id={deployment_id}",
                "--label", "com.actiondesign.mcp.transport=stdio",
                "--label", "com.centurylinklabs.watchtower.enable=false",
                "--network", "bridge", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--pids-limit", str(manifest.resources.pids),
                "--cpus", str(manifest.resources.cpu),
                "--memory", f"{manifest.resources.memory_mib}m",
                "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            ]
            container_env = dict(os.environ)
            secret_values: list[str] = []
            for binding in manifest.environment:
                self.settings.secret_store.require_exposure(
                    binding.secret_id,
                    {"stdio_env"},
                )
                value = self.settings.secret_store.resolve_value(binding.secret_id)
                expanded = f"{binding.value_prefix}{value}"
                secret_values.append(expanded)
                container_env[binding.name] = expanded
                args.extend(["-e", binding.name])
            args.append(image)
            args.extend(manifest.stdio.command)
            async with self._semaphore:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=container_env,
                    )
                finally:
                    for binding in manifest.environment:
                        container_env.pop(binding.name, None)
                pipe_in = asyncio.create_task(self._pipe_to_process(reader, process))
                pipe_out = asyncio.create_task(self._pipe_from_process(process, writer))
                pipe_error = asyncio.create_task(
                    self._drain_stderr(
                        process,
                        deployment_id,
                        tuple(secret_values),
                    )
                )
                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=self.settings.stdio_max_session_seconds,
                    )
                finally:
                    pipe_in.cancel()
                    await asyncio.gather(
                        pipe_in,
                        pipe_out,
                        pipe_error,
                        return_exceptions=True,
                    )
        except Exception:
            try:
                writer.write(b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"stdio deployment unavailable"},"id":null}\n')
                await writer.drain()
            except Exception:
                pass
        finally:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
            if container_name:
                try:
                    cleanup = await asyncio.create_subprocess_exec(
                        "docker",
                        "rm",
                        "-f",
                        container_name,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(cleanup.wait(), timeout=10)
                except Exception:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _pipe_to_process(reader: asyncio.StreamReader, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None
        while chunk := await reader.read(65536):
            process.stdin.write(chunk)
            await process.stdin.drain()
        process.stdin.close()

    @staticmethod
    async def _pipe_from_process(process: asyncio.subprocess.Process, writer: asyncio.StreamWriter) -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            writer.write(chunk)
            await writer.drain()

    async def _drain_stderr(
        self,
        process: asyncio.subprocess.Process,
        deployment_id: str,
        redact: tuple[str, ...],
    ) -> None:
        assert process.stderr is not None
        data = bytearray()
        while chunk := await process.stderr.read(65536):
            data.extend(chunk)
            if len(data) > _MAX_LOG_BYTES:
                del data[:-_MAX_LOG_BYTES]
        if data:
            text = bytes(data).decode("utf-8", errors="replace")
            for value in redact:
                if value:
                    text = text.replace(value, "[REDACTED]")
            path = self.settings.log_root / f"{DockerNginxDeployerBackend._safe_name(deployment_id)}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text[-_MAX_LOG_BYTES:], encoding="utf-8")
            path.chmod(0o600)


def create_deployer_app(
    settings: McpDeployerSettings | None = None,
    *,
    store: McpDeploymentStore | None = None,
    backend: DeployerBackend | None = None,
) -> FastAPI:
    deployer_settings = settings or McpDeployerSettings()
    deployment_store = store or McpDeploymentStore(deployer_settings.state_path)
    deployment_backend = backend or DockerNginxDeployerBackend(deployer_settings)
    if isinstance(deployment_backend, DockerNginxDeployerBackend):
        deployment_backend.attach_store(deployment_store)
    tasks: set[asyncio.Task[Any]] = set()
    broker = McpStdioBroker(deployer_settings, deployment_store) if backend is None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if broker:
            await broker.start()
        for interrupted in deployment_store.active_job_records():
            deployment_store.update_job(
                interrupted.id,
                status="rolling_back",
                error_code="MCP_DEPLOYER_RESTARTED",
            )
            try:
                await deployment_backend.rollback_job(interrupted)
                deployment_store.update_job(interrupted.id, status="rolled_back")
            except Exception:
                deployment_store.update_job(
                    interrupted.id,
                    status="rollback_failed",
                    error_code="MCP_DEPLOY_ROLLBACK_FAILED",
                )
        try:
            yield
        finally:
            for task in list(tasks):
                task.cancel()
            if broker:
                await broker.close()

    app = FastAPI(title="ActionDesign MCP Deployer", version="1.0.0", lifespan=lifespan)
    app.state.settings = deployer_settings
    app.state.store = deployment_store
    app.state.backend = deployment_backend

    def require_token(
        x_actiondesign_deployer_token: str = Header(default=""),
    ) -> None:
        if not secrets.compare_digest(
            x_actiondesign_deployer_token,
            deployer_settings.token,
        ):
            raise HTTPException(status_code=401, detail={"code": "DEPLOYER_UNAUTHORIZED"})

    def spawn(coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def process_job(job_id: str) -> None:
        try:
            job = deployment_store.get_job_record(job_id)
            assert job is not None
            deployment_store.update_job(
                job_id,
                status="pulling" if job.request.source.type == "oci" else "cloning",
            )
            result = await deployment_backend.prepare(job)
            deployment_store.update_job(job_id, **result)
            spawn(validation_timeout(job_id))
        except Exception as exc:
            current = deployment_store.get_job_record(job_id)
            if current:
                try:
                    await deployment_backend.rollback_job(current)
                    deployment_store.update_job(
                        job_id,
                        status="failed",
                        error_code=f"MCP_DEPLOY_{type(exc).__name__.upper()}",
                    )
                except Exception:
                    deployment_store.update_job(
                        job_id,
                        status="rollback_failed",
                        error_code="MCP_DEPLOY_ROLLBACK_FAILED",
                    )

    async def validation_timeout(job_id: str) -> None:
        await asyncio.sleep(deployer_settings.browser_validation_timeout_seconds)
        job = deployment_store.get_job_record(job_id)
        if job and job.status == "awaiting_browser_validation":
            deployment_store.update_job(job_id, status="rolling_back", error_code="MCP_BROWSER_VALIDATION_TIMEOUT")
            try:
                await deployment_backend.rollback_job(job)
                deployment_store.update_job(job_id, status="rolled_back")
            except Exception:
                deployment_store.update_job(job_id, status="rollback_failed", error_code="MCP_DEPLOY_ROLLBACK_FAILED")

    @app.get("/health", dependencies=[Depends(require_token)])
    async def health() -> dict[str, Any]:
        return {"status": "ok", "runtime": "mcp-deployer"}

    @app.get("/v1/deployments", dependencies=[Depends(require_token)])
    async def list_deployments() -> list[dict[str, Any]]:
        return deployment_store.list_deployments()

    @app.get("/v1/deployments/{deployment_id}", dependencies=[Depends(require_token)])
    async def get_deployment(deployment_id: str) -> dict[str, Any]:
        item = deployment_store.get_deployment(deployment_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        return item

    @app.put("/v1/deployments/{deployment_id}", status_code=202, dependencies=[Depends(require_token)])
    async def deploy(deployment_id: str, body: McpDeploymentUpsertRequest) -> dict[str, Any]:
        if body.id != deployment_id:
            raise HTTPException(status_code=422, detail={"code": "MCP_DEPLOYMENT_ID_MISMATCH"})
        try:
            job = deployment_store.create_job(body)
        except McpDeploymentVersionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "MCP_DEPLOYMENT_VERSION_CONFLICT", "id": exc.deployment_id, "expected": exc.expected, "current": exc.current},
            ) from exc
        except McpDeploymentBusyError as exc:
            raise HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_BUSY"}) from exc
        spawn(process_job(job.id))
        return {"id": deployment_id, "jobId": job.id, "status": job.status}

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    async def get_job(job_id: str) -> dict[str, Any]:
        item = deployment_store.get_job(job_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_JOB_NOT_FOUND"})
        return item

    @app.post("/v1/jobs/{job_id}/promote", dependencies=[Depends(require_token)])
    async def promote_job(job_id: str) -> dict[str, Any]:
        job = deployment_store.get_job_record(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_JOB_NOT_FOUND"})
        if job.status != "awaiting_browser_validation":
            raise HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_JOB_NOT_READY"})
        result = await deployment_backend.promote(job)
        return deployment_store.update_job(job_id, **result).model_dump(mode="json", by_alias=True)

    @app.post("/v1/jobs/{job_id}/commit", dependencies=[Depends(require_token)])
    async def commit_job(job_id: str, config_version: int = Query(alias="configVersion", ge=1)) -> dict[str, Any]:
        job = deployment_store.get_job_record(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_JOB_NOT_FOUND"})
        previous = deployment_store.get_record(job.deployment_id)
        record = deployment_store.commit_job(job_id, config_version=config_version)
        try:
            await deployment_backend.commit(job, previous)
        except Exception:
            deployment_store.update_deployment(record.id, status="degraded", last_error_code="MCP_DEPLOY_COMMIT_CLEANUP_FAILED")
        return record.model_dump(mode="json", by_alias=True)

    @app.post("/v1/jobs/{job_id}/reject", dependencies=[Depends(require_token)])
    async def reject_job(job_id: str) -> dict[str, Any]:
        job = deployment_store.get_job_record(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_JOB_NOT_FOUND"})
        deployment_store.update_job(job_id, status="rolling_back", error_code="MCP_BROWSER_VALIDATION_FAILED")
        try:
            await deployment_backend.rollback_job(job)
            updated = deployment_store.update_job(job_id, status="rolled_back")
        except Exception:
            updated = deployment_store.update_job(job_id, status="rollback_failed", error_code="MCP_DEPLOY_ROLLBACK_FAILED")
        return updated.model_dump(mode="json", by_alias=True)

    @app.put("/v1/deployments/{deployment_id}/enabled", dependencies=[Depends(require_token)])
    async def set_enabled(deployment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        record = deployment_store.get_record(deployment_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        try:
            deployment_store.require_not_busy(deployment_id)
        except McpDeploymentBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "MCP_DEPLOYMENT_BUSY"},
            ) from exc
        expected = int(body.get("expectedDeploymentVersion", -1))
        if expected != record.deployment_version:
            raise HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_VERSION_CONFLICT", "id": deployment_id, "expected": expected, "current": record.deployment_version})
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail={"code": "MCP_DEPLOYMENT_ENABLED_INVALID"})
        result = await deployment_backend.set_enabled(record, enabled)
        registration = record.registration.model_copy(update={"enabled": enabled})
        return deployment_store.update_deployment(
            deployment_id,
            status="running" if enabled else "stopped",
            internal_port=int(result.get("internalPort", record.internal_port)),
            config_version=int(body.get("nextConfigVersion", record.config_version)),
            registration=registration,
        ).model_dump(mode="json", by_alias=True)

    @app.post("/v1/deployments/{deployment_id}/rollback", dependencies=[Depends(require_token)])
    async def rollback(deployment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        record = deployment_store.get_record(deployment_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        try:
            deployment_store.require_not_busy(deployment_id)
        except McpDeploymentBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "MCP_DEPLOYMENT_BUSY"},
            ) from exc
        expected = int(body.get("expectedDeploymentVersion", -1))
        if expected != record.deployment_version:
            raise HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_VERSION_CONFLICT", "id": deployment_id, "expected": expected, "current": record.deployment_version})
        result = await deployment_backend.rollback_deployment(record)
        result["config_version"] = int(body.get("nextConfigVersion", record.config_version))
        return deployment_store.update_deployment(deployment_id, **result).model_dump(mode="json", by_alias=True)

    @app.delete("/v1/deployments/{deployment_id}", dependencies=[Depends(require_token)])
    async def delete_deployment(
        deployment_id: str,
        expected_deployment_version: int = Query(alias="expectedDeploymentVersion", ge=1),
        finalize: bool = Query(default=False),
    ) -> dict[str, Any]:
        record = deployment_store.get_record(deployment_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        try:
            deployment_store.require_not_busy(deployment_id)
        except McpDeploymentBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "MCP_DEPLOYMENT_BUSY"},
            ) from exc
        if record.deployment_version != expected_deployment_version:
            raise HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_VERSION_CONFLICT", "id": deployment_id, "expected": expected_deployment_version, "current": record.deployment_version})
        if not finalize:
            result = await deployment_backend.set_enabled(record, False)
            prepared = deployment_store.update_deployment(
                deployment_id,
                status="stopped",
                internal_port=int(result.get("internalPort", record.internal_port)),
            )
            return prepared.model_dump(mode="json", by_alias=True)
        await deployment_backend.delete(record)
        deployment_store.delete_deployment(deployment_id, expected_deployment_version)
        return {"id": deployment_id, "deleted": True}

    @app.get("/v1/deployments/{deployment_id}/logs", dependencies=[Depends(require_token)])
    async def logs(deployment_id: str, response: Response) -> dict[str, Any]:
        if not deployment_store.has_deployment_or_job(deployment_id):
            raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        response.headers["Cache-Control"] = "no-store"
        return {"id": deployment_id, "log": deployment_backend.logs(deployment_id)}

    return app


__all__ = [
    "DeployerBackend",
    "DockerNginxDeployerBackend",
    "McpDeployerSettings",
    "McpStdioBroker",
    "create_deployer_app",
]
