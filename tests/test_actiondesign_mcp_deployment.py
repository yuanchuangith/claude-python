from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.mcp_deployer import create_deployer_app
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.mcp_deployment import (
    McpDeploymentBusyError,
    McpDeploymentManifest,
    McpDeploymentStore,
    McpDeploymentUpsertRequest,
    McpDeploymentVersionConflictError,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings


DIGEST = "sha256:" + "a" * 64


def request_payload(*, expected: int = 0) -> dict[str, Any]:
    return {
        "id": "deployed.http",
        "source": {
            "type": "oci",
            "image": f"ghcr.io/example/mcp@{DIGEST}",
            "credentialSecretId": "",
        },
        "manifest": {
            "apiVersion": "actiondesign.mcp/v1",
            "transport": "streamable_http",
            "http": {
                "containerPort": 8000,
                "mcpPath": "/mcp",
                "healthCheck": {"type": "http", "path": "/health"},
            },
            "resources": {"cpu": 0.5, "memoryMiB": 256, "pids": 128},
        },
        "registration": {
            "displayName": "Deployed HTTP",
            "enabled": True,
            "staticHeaders": {},
            "secretBindings": [],
        },
        "expectedDeploymentVersion": expected,
        "expectedConfigVersion": 0,
    }


def test_manifest_rejects_shell_shape_and_reserved_headers() -> None:
    with pytest.raises(ValidationError):
        McpDeploymentManifest.model_validate(
            {
                "apiVersion": "actiondesign.mcp/v1",
                "transport": "stdio",
                "stdio": {"command": "python -m unsafe"},
            }
        )
    payload = request_payload()
    payload["registration"]["staticHeaders"] = {"MCP-Protocol-Version": "bad"}
    with pytest.raises(ValidationError):
        McpDeploymentUpsertRequest.model_validate(payload)

    git_payload = request_payload()
    git_payload["source"] = {
        "type": "git",
        "repository": "https://github.com/example/mcp.git",
        "commit": "b" * 40,
        "dockerfilePath": "deploy/Dockerfile",
        "contextPath": ".",
        "manifestPath": "mcp-deploy.yaml",
        "credentialSecretId": "",
    }
    git_payload["manifest"] = None
    with pytest.raises(ValidationError):
        McpDeploymentUpsertRequest.model_validate(git_payload)


def test_deployment_store_uses_optimistic_lock_and_persists_jobs(tmp_path: Path) -> None:
    store = McpDeploymentStore(tmp_path / "deployments.json")
    request = McpDeploymentUpsertRequest.model_validate(request_payload())
    job = store.create_job(request)
    store.update_job(
        job.id,
        status="promoting",
        manifest=request.manifest,
        revision="b" * 64,
        image=request.source.image,
    )
    record = store.commit_job(job.id, config_version=1)
    assert record.deployment_version == 1
    assert record.config_version == 1
    reloaded = McpDeploymentStore(tmp_path / "deployments.json")
    assert reloaded.get_deployment(request.id)["activeRevision"] == "b" * 64
    stale = McpDeploymentUpsertRequest.model_validate(request_payload(expected=0))
    with pytest.raises(McpDeploymentVersionConflictError):
        reloaded.create_job(stale)
    next_payload = request_payload(expected=1)
    next_payload["expectedConfigVersion"] = 1
    reloaded.create_job(McpDeploymentUpsertRequest.model_validate(next_payload))
    with pytest.raises(McpDeploymentBusyError):
        reloaded.require_not_busy(request.id)


class FakeBackend:
    def __init__(self) -> None:
        self.prepared = False
        self.promoted = False
        self.committed = False

    async def prepare(self, job):
        self.prepared = True
        return {
            "status": "awaiting_browser_validation",
            "manifest": job.request.manifest,
            "revision": "c" * 64,
            "image": job.request.source.image,
            "internal_port": 40123,
            "candidate_path": "/mcp-candidate/token/mcp",
        }

    async def promote(self, _job):
        self.promoted = True
        return {"status": "promoting"}

    async def rollback_job(self, _job):
        return None

    async def commit(self, _job, _previous):
        self.committed = True

    async def set_enabled(self, record, enabled):
        return {"enabled": enabled, "internalPort": record.internal_port}

    async def delete(self, _record):
        return None

    async def rollback_deployment(self, record):
        return {
            "activeRevision": record.previous_revision,
            "activeImage": record.previous_image,
            "previousRevision": record.active_revision,
            "previousImage": record.active_image,
            "internalPort": record.internal_port,
        }

    def logs(self, _deployment_id):
        return "bounded log"


def test_deployer_api_runs_async_job_promote_and_commit(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        token="d" * 32,
        state_path=tmp_path / "deployments.json",
        browser_validation_timeout_seconds=300,
    )
    store = McpDeploymentStore(settings.state_path)
    backend = FakeBackend()
    with TestClient(create_deployer_app(settings, store=store, backend=backend)) as client:
        headers = {"X-ActionDesign-Deployer-Token": settings.token}
        created = client.put(
            "/v1/deployments/deployed.http",
            headers=headers,
            json=request_payload(),
        )
        assert created.status_code == 202
        job_id = created.json()["jobId"]
        for _ in range(50):
            job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
            if job["status"] == "awaiting_browser_validation":
                break
        assert job["status"] == "awaiting_browser_validation"
        assert client.post(f"/v1/jobs/{job_id}/promote", headers=headers).status_code == 200
        committed = client.post(
            f"/v1/jobs/{job_id}/commit?configVersion=1",
            headers=headers,
        )
        assert committed.status_code == 200
        assert committed.json()["status"] == "running"
        assert backend.prepared and backend.promoted and backend.committed
        assert client.get("/v1/deployments", headers=headers).json()[0]["id"] == "deployed.http"
        stopped = client.put(
            "/v1/deployments/deployed.http/enabled",
            headers=headers,
            json={
                "enabled": False,
                "expectedDeploymentVersion": 1,
                "nextConfigVersion": 2,
            },
        )
        assert stopped.status_code == 200
        assert stopped.json()["registration"]["enabled"] is False
        assert client.get(
            "/v1/deployments/deployed.http/logs",
            headers=headers,
        ).json()["log"] == "bounded log"
        assert client.get("/v1/deployments", headers={}).status_code == 401


class FakeDeployerClient:
    def __init__(self) -> None:
        self.job: dict[str, Any] | None = None
        self.record: dict[str, Any] | None = None

    async def request(self, method, path, *, json_body=None, timeout_seconds=None):
        if method == "PUT" and path == "/v1/deployments/deployed.http":
            request = McpDeploymentUpsertRequest.model_validate(json_body)
            self.job = {
                "id": "job_gateway",
                "deploymentId": request.id,
                "status": "awaiting_browser_validation",
                "request": request.model_dump(mode="json", by_alias=True),
                "manifest": request.manifest.model_dump(mode="json", by_alias=True),
                "revision": "e" * 64,
                "image": request.source.image,
                "internalPort": 40123,
                "candidatePath": "/mcp-candidate/token/mcp",
                "createdAt": "2026-08-10T00:00:00+00:00",
                "updatedAt": "2026-08-10T00:00:00+00:00",
                "errorCode": "",
            }
            return {"id": request.id, "jobId": "job_gateway", "status": "queued"}
        if method == "GET" and path == "/v1/jobs/job_gateway":
            return dict(self.job)
        if method == "POST" and path == "/v1/jobs/job_gateway/promote":
            self.job["status"] = "promoting"
            return dict(self.job)
        if method == "POST" and path.startswith("/v1/jobs/job_gateway/commit"):
            self.record = {
                "id": "deployed.http",
                "source": self.job["request"]["source"],
                "manifest": self.job["manifest"],
                "registration": self.job["request"]["registration"],
                "deploymentVersion": 1,
                "configVersion": 1,
                "status": "running",
                "activeRevision": self.job["revision"],
                "previousRevision": "",
                "activeImage": self.job["image"],
                "previousImage": "",
                "internalPort": 40123,
                "updatedAt": "2026-08-10T00:00:01+00:00",
                "lastErrorCode": "",
            }
            return dict(self.record)
        if method == "GET" and path == "/v1/deployments":
            return [dict(self.record)] if self.record else []
        if method == "PUT" and path == "/v1/deployments/deployed.http/enabled":
            self.record["deploymentVersion"] += 1
            self.record["configVersion"] = json_body["nextConfigVersion"]
            self.record["status"] = "running" if json_body["enabled"] else "stopped"
            return dict(self.record)
        if method == "DELETE" and path.startswith("/v1/deployments/deployed.http"):
            if "finalize=true" in path:
                self.record = None
                return {"id": "deployed.http", "deleted": True}
            self.record["deploymentVersion"] += 1
            self.record["status"] = "stopped"
            return dict(self.record)
        if method == "POST" and path.endswith("/reject"):
            return {"status": "rolled_back"}
        raise AssertionError((method, path, json_body, timeout_seconds))


def gateway_settings(tmp_path: Path) -> Settings:
    return Settings(
        allow_origins=["https://actiondesign.example.test"],
        service_auth_mode="required",
        service_auth_token="service-" + "x" * 32,
        profile_admin_token="admin-token",
        admin_allow_remote=True,
        admin_require_https_remote=False,
        log_root=tmp_path / "logs",
        request_log_path=tmp_path / "requests.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        profile_store_path=tmp_path / "profiles.json",
        profile_secret_store_backend="file",
        profile_secret_store_path=tmp_path / "profile-secrets.json",
        managed_secret_store_path=tmp_path / "managed-secrets.json",
        mcp_server_store_path=tmp_path / "mcp-servers.json",
        browser_api_key_store_path=tmp_path / "browser-keys.json",
        client_api_key_store_path=tmp_path / "client-keys.json",
        mcp_proxy_enabled=True,
        public_browser_mcp_config=True,
        mcp_deployments_enabled=True,
        mcp_deployer_token="d" * 32,
        mcp_deployer_socket_path=Path("C:/actiondesign/deployer.sock"),
        mcp_stdio_bridge_path=Path("C:/actiondesign/mcp-stdio-bridge.exe"),
        mcp_deployment_public_base_url="https://43-135-137-212.sslip.io",
        mcp_servers=[],
        mcp_allowed_http_urls=[],
        mcp_allowed_executable_paths=[],
        mimo_api_key="",
        claude_code_api_key="",
    )


def test_gateway_deploys_promotes_and_publicly_stops_and_deletes(tmp_path: Path) -> None:
    deployer = FakeDeployerClient()
    client = TestClient(
        create_app(
            gateway_settings(tmp_path),
            profile_secret_store=MemoryProfileSecretStore(),
            mcp_deployer_client=deployer,
        )
    )
    admin = {"Authorization": "Bearer admin-token"}
    started = client.put(
        "/api/actiondesign-agent/admin/mcp-deployments/deployed.http",
        headers=admin,
        json=request_payload(),
    )
    assert started.status_code == 202, started.text
    candidate = client.get(
        "/api/actiondesign-agent/admin/mcp-deployment-jobs/job_gateway/candidate-config",
        headers=admin,
    )
    assert candidate.status_code == 200
    assert candidate.headers["cache-control"] == "no-store"
    assert candidate.json()["streamableHttp"]["url"].endswith("/mcp-candidate/token/mcp")
    promoted = client.post(
        "/api/actiondesign-agent/admin/mcp-deployment-jobs/job_gateway/browser-validation",
        headers=admin,
        json={"success": True, "toolListHash": "sha256:test"},
    )
    assert promoted.status_code == 200, promoted.text
    public_headers = {"Origin": "https://actiondesign.example.test"}
    projected = client.get(
        "/api/actiondesign-agent/mcp-browser/servers/deployed.http",
        headers=public_headers,
    )
    assert projected.status_code == 200
    assert projected.json()["managedBy"] == "deployment"
    assert projected.json()["deploymentVersion"] == 1
    admin_stopped = client.put(
        "/api/actiondesign-agent/admin/mcp-deployments/deployed.http/enabled",
        headers=admin,
        json={
            "enabled": False,
            "expectedConfigVersion": 1,
            "expectedDeploymentVersion": 1,
        },
    )
    assert admin_stopped.status_code == 200, admin_stopped.text
    assert admin_stopped.json()["deploymentVersion"] == 2
    public_started = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/deployed.http/enabled",
        headers=public_headers,
        json={
            "enabled": True,
            "expectedConfigVersion": 2,
            "expectedDeploymentVersion": 2,
        },
    )
    assert public_started.status_code == 200, public_started.text
    assert public_started.json()["deploymentVersion"] == 3
    stopped = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/deployed.http/enabled",
        headers=public_headers,
        json={
            "enabled": False,
            "expectedConfigVersion": 3,
            "expectedDeploymentVersion": 3,
        },
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["enabled"] is False
    assert stopped.json()["deploymentVersion"] == 4
    deleted = client.request(
        "DELETE",
        "/api/actiondesign-agent/mcp-browser/servers/deployed.http",
        headers=public_headers,
        json={"expectedConfigVersion": 4, "expectedDeploymentVersion": 4},
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(
        "/api/actiondesign-agent/mcp-browser/servers/deployed.http",
        headers=public_headers,
    ).status_code == 404
