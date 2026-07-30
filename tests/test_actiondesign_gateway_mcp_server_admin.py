from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.mcp_proxy import McpProxy  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.mcp_server_store import (  # noqa: E402
    McpServerStore,
    McpServerStoreError,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
SERVICE_TOKEN = "service-token-" + ("x" * 32)
REQUEST_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "allow_origins": ["https://actiondesign.example.test"],
        "service_auth_mode": "required",
        "service_auth_token": SERVICE_TOKEN,
        "profile_admin_token": "admin-token",
        "admin_allow_remote": True,
        "admin_require_https_remote": False,
        "log_root": tmp_path / "logs",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "full_conversation_log_root": tmp_path / "conversations",
        "profile_store_path": tmp_path / "profiles.json",
        "managed_secret_store_path": tmp_path / "managed-secrets.json",
        "mcp_server_store_path": tmp_path / "managed-mcp-servers.json",
        "browser_api_key_store_path": tmp_path / "browser-key.json",
        "profile_allowed_hosts": [
            "api.example.test",
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
        "mimo_api_key": "",
        "claude_code_api_key": "",
        "mcp_proxy_enabled": True,
        "mcp_servers": [],
        "mcp_allowed_hosts": ["mcp.example.test"],
        "mcp_allowed_http_paths": ["/mcp"],
        "mcp_allowed_executable_paths": [],
    }
    values.update(overrides)
    return Settings(**values)


def managed_http_server(server_id: str = "managed.server-1") -> dict:
    return {
        "id": server_id,
        "displayName": "Managed MCP Server",
        "transport": "streamable_http",
        "streamableHttp": {
            "url": "https://mcp.example.test/mcp",
            "secretBindings": [
                {
                    "secretId": "mcp.primary",
                    "target": "header",
                    "name": "Authorization",
                    "valuePrefix": "Bearer ",
                }
            ],
        },
        "expectedServerName": "managed-server",
        "timeoutSeconds": 20.0,
        "enabled": True,
    }


def test_managed_mcp_server_crud_hot_reloads_proxy_and_never_persists_secret(
    tmp_path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    @asynccontextmanager
    async def fake_session(_self, server):
        class FakeSession:
            async def list_tools(self):
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="echo.value",
                            description="Echo a value",
                            inputSchema={
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                            outputSchema=None,
                        )
                    ]
                )

        assert server.id == "managed.server-1"
        yield FakeSession(), {
            "serverName": "managed-server",
            "serverVersion": "1.0.0",
        }, ()

    monkeypatch.setattr(McpProxy, "_session", fake_session)
    initial_proxy = client.app.state.mcp_proxy

    assert client.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    ).json() == []

    secret_value = "managed-mcp-secret-1234"
    secret = client.put(
        "/api/actiondesign-agent/admin/secrets/mcp.primary",
        headers=ADMIN_HEADERS,
        json={
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Managed MCP key",
            "value": secret_value,
        },
    )
    assert secret.status_code == 200

    created = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=managed_http_server(),
    )
    assert created.status_code == 200, created.text
    assert created.json()["source"] == "managed"
    assert created.json()["proxyEnabled"] is True
    assert created.json()["secretBindingStatus"] == [
        {"secretId": "mcp.primary", "configured": True}
    ]
    active_proxy = client.app.state.mcp_proxy
    assert active_proxy is not initial_proxy
    persisted = settings.mcp_server_store_path.read_text(encoding="utf-8")
    assert "managed.server-1" in persisted
    assert "mcp.primary" in persisted
    assert secret_value not in persisted

    runtime_headers = {
        "X-ActionDesign-Service-Token": SERVICE_TOKEN,
        "X-Request-Id": REQUEST_ID,
        "X-Correlation-Id": CORRELATION_ID,
    }
    listed = client.get(
        "/api/actiondesign-agent/mcp/managed.server-1/tools",
        headers=runtime_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["tools"][0]["name"] == "echo.value"

    rejected_update = managed_http_server()
    rejected_update["streamableHttp"]["url"] = "https://unapproved.example.test/mcp"
    rejected = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=rejected_update,
    )
    assert rejected.status_code == 422
    assert client.app.state.mcp_proxy is active_proxy
    assert client.get(
        "/api/actiondesign-agent/mcp/managed.server-1/tools",
        headers=runtime_headers,
    ).status_code == 200

    tested = client.post(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1/connection-test",
        headers=ADMIN_HEADERS,
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "healthy"
    assert tested.json()["serverName"] == "managed-server"
    assert tested.json()["toolCount"] == 1
    assert tested.json()["errorCode"] == ""

    disabled = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1/enabled",
        headers=ADMIN_HEADERS,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    unavailable = client.get(
        "/api/actiondesign-agent/mcp/managed.server-1/tools",
        headers=runtime_headers,
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "MCP_SERVER_DISABLED"

    assert client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1/enabled",
        headers=ADMIN_HEADERS,
        json={"enabled": True},
    ).status_code == 200
    deleted = client.delete(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": "managed.server-1", "deleted": True}
    missing = client.get(
        "/api/actiondesign-agent/mcp/managed.server-1/tools",
        headers=runtime_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "MCP_SERVER_NOT_FOUND"
    assert json.loads(persisted := settings.mcp_server_store_path.read_text(encoding="utf-8"))[
        "servers"
    ] == []
    assert secret_value not in persisted


def test_environment_mcp_servers_are_read_only_and_managed_ids_use_full_contract(
    tmp_path,
):
    environment = {
        "id": "environment.server-1",
        "transport": "streamable_http",
        "url": "https://mcp.example.test/mcp",
        "secretBindings": [],
    }
    settings = make_settings(tmp_path, mcp_servers=[environment])
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    listed = client.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "environment.server-1"
    assert listed.json()[0]["source"] == "environment"

    replacement = managed_http_server("environment.server-1")
    for response in (
        client.put(
            "/api/actiondesign-agent/admin/mcp-servers/environment.server-1",
            headers=ADMIN_HEADERS,
            json=replacement,
        ),
        client.put(
            "/api/actiondesign-agent/admin/mcp-servers/environment.server-1/enabled",
            headers=ADMIN_HEADERS,
            json={"enabled": False},
        ),
        client.delete(
            "/api/actiondesign-agent/admin/mcp-servers/environment.server-1",
            headers=ADMIN_HEADERS,
        ),
    ):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "MCP_SERVER_READ_ONLY"


def test_mcp_proxy_can_start_enabled_without_preconfigured_servers(tmp_path):
    settings = make_settings(tmp_path, mcp_servers=[])
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    health = client.get("/api/actiondesign-agent/health")
    assert health.status_code == 200
    assert health.json()["mcpProxyEnabled"] is True
    assert client.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    ).json() == []


def test_managed_mcp_server_rejects_non_allowlisted_transport_without_persisting(
    tmp_path,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    payload = managed_http_server()
    payload["streamableHttp"]["url"] = "https://unapproved.example.test/mcp"
    response = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MCP_SERVER_CONFIGURATION_INVALID"
    assert not settings.mcp_server_store_path.exists()


def test_mcp_server_admin_requires_bearer_and_strict_128_character_id(tmp_path):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    denied = client.get("/api/actiondesign-agent/admin/mcp-servers")
    assert denied.status_code == 401
    invalid = client.get(
        "/api/actiondesign-agent/admin/mcp-servers/invalid%2Fserver",
        headers=ADMIN_HEADERS,
    )
    assert invalid.status_code in {404, 422}
    schema = client.get("/openapi.json").json()
    assert schema["info"]["version"] == "3.1.0"
    for path in (
        "/api/actiondesign-agent/admin/mcp-servers",
        "/api/actiondesign-agent/admin/mcp-servers/{server_id}",
        "/api/actiondesign-agent/admin/mcp-servers/{server_id}/enabled",
        "/api/actiondesign-agent/admin/mcp-servers/{server_id}/connection-test",
    ):
        assert path in schema["paths"]


def test_missing_bound_secret_is_visible_as_status_and_fails_runtime_before_transport(
    tmp_path,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    created = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=managed_http_server(),
    )
    assert created.status_code == 200
    assert created.json()["secretBindingStatus"] == [
        {"secretId": "mcp.primary", "configured": False}
    ]
    reloaded = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    assert reloaded.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    ).json()[0]["id"] == "managed.server-1"
    runtime = client.get(
        "/api/actiondesign-agent/mcp/managed.server-1/tools",
        headers={
            "X-ActionDesign-Service-Token": SERVICE_TOKEN,
            "X-Request-Id": REQUEST_ID,
            "X-Correlation-Id": CORRELATION_ID,
        },
    )
    assert runtime.status_code == 503
    assert runtime.json()["detail"]["code"] == "MCP_SECRET_UNAVAILABLE"
    assert "mcp.primary" not in runtime.text


def test_corrupt_managed_mcp_server_store_fails_closed(tmp_path):
    settings = make_settings(tmp_path)
    settings.mcp_server_store_path.write_text(
        json.dumps(
            {
                "schemaVersion": "1",
                "servers": [
                    {
                        "id": "bad.server",
                        "displayName": "Bad Server",
                        "transport": "streamable_http",
                        "streamableHttp": {
                            "url": "https://mcp.example.test/mcp?secret=value"
                        },
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(McpServerStoreError, match="managed MCP Server metadata is invalid"):
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())


def test_environment_http_allowlist_uses_exact_authority_including_port(tmp_path):
    server = {
        "id": "environment.server-port",
        "transport": "streamable_http",
        "url": "https://mcp.example.test:8443/mcp",
    }
    settings = make_settings(
        tmp_path,
        mcp_servers=[server],
        mcp_allowed_hosts=["mcp.example.test:8443"],
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    assert client.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    ).json()[0]["id"] == "environment.server-port"

    with pytest.raises(ValueError, match="MCP server host is not allowlisted"):
        make_settings(
            tmp_path,
            mcp_servers=[server],
            mcp_allowed_hosts=["mcp.example.test"],
        )


def test_store_serializes_persistence_and_runtime_publish_in_commit_order(tmp_path):
    store = McpServerStore(tmp_path / "managed-mcp-servers.json", [])
    first_build_entered = Event()
    release_first_build = Event()
    second_build_entered = Event()
    published: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def build_runtime(configs):
        runtime = tuple(server.id for server in configs)
        if runtime == ("server.a",):
            first_build_entered.set()
            assert release_first_build.wait(5)
        elif runtime == ("server.a", "server.b"):
            second_build_entered.set()
        return runtime

    def publish_runtime(runtime):
        published.append(runtime)

    def save(server_id):
        try:
            store.upsert(
                {
                    **managed_http_server(server_id),
                    "streamableHttp": {
                        "url": "https://mcp.example.test/mcp",
                        "secretBindings": [],
                    },
                },
                build_runtime,
                publish_runtime,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = Thread(target=save, args=("server.a",))
    second = Thread(target=save, args=("server.b",))
    first.start()
    assert first_build_entered.wait(5)
    second.start()
    assert not second_build_entered.wait(0.05)
    release_first_build.set()
    first.join(5)
    second.join(5)

    assert not errors
    assert not first.is_alive()
    assert not second.is_alive()
    assert published == [("server.a",), ("server.a", "server.b")]
    assert tuple(server.id for server in store.list_configs()) == published[-1]
    reloaded = McpServerStore(store.path, [])
    assert tuple(server.id for server in reloaded.list_configs()) == published[-1]
