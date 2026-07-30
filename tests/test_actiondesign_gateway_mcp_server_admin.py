from __future__ import annotations

import ipaddress
import json
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway import (
    mcp_proxy as mcp_proxy_module,  # noqa: E402
)
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


@pytest.fixture(autouse=True)
def _stable_private_mcp_dns(monkeypatch):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (ipaddress.ip_address("10.20.30.40"),),
    )


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
        "mcp_allowed_http_urls": ["https://mcp.example.test/mcp"],
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
    assert set(secret.json()) == {
        "id",
        "kind",
        "displayName",
        "configured",
        "createdAt",
        "updatedAt",
        "rotatedAt",
        "revokedAt",
    }

    created = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=managed_http_server(),
    )
    assert created.status_code == 200, created.text
    assert created.json()["source"] == "managed"
    assert created.json()["proxyEnabled"] is True
    assert created.json()["browserConfigurable"] is True
    assert created.json()["secretBindingStatus"] == [
        {"secretId": "mcp.primary", "configured": True}
    ]
    assert set(created.json()) == {
        "id",
        "displayName",
        "transport",
        "expectedServerName",
        "timeoutSeconds",
        "enabled",
        "source",
        "proxyEnabled",
        "browserConfigurable",
        "executionMode",
        "authMode",
        "browserDirectUrl",
        "configVersion",
        "activeVersion",
        "connectionStatus",
        "secretBindingStatus",
    }
    assert all(
        key not in created.json()
        for key in ("stdio", "streamableHttp", "url", "headers", "command", "cwd", "args")
    )
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
    assert listed.json()[0]["browserConfigurable"] is False
    assert "streamableHttp" not in listed.json()[0]
    assert "url" not in listed.text

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


def test_legacy_host_and_path_allowlists_are_rejected_when_proxy_is_enabled(tmp_path):
    with pytest.raises(ValueError, match="legacy ignored settings"):
        make_settings(
            tmp_path,
            mcp_allowed_hosts=["mcp.example.test"],
            mcp_allowed_http_paths=["/mcp"],
        )


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


def test_public_https_mcp_server_does_not_require_static_url_allowlist(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (
            ipaddress.ip_address("8.8.8.8"),
            ipaddress.ip_address("2606:4700:4700::1111"),
        ),
    )
    settings = make_settings(tmp_path, mcp_allowed_http_urls=[])
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    async def fake_list_tools(_self, server_id):
        return {
            "serverId": server_id,
            "serverName": "public-server",
            "serverVersion": "1.0.0",
            "tools": [],
        }

    monkeypatch.setattr(McpProxy, "list_tools", fake_list_tools)
    payload = managed_http_server()
    payload["streamableHttp"]["url"] = "https://public.example.test/mcp"

    created = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=payload,
    )

    assert created.status_code == 200, created.text
    assert "streamableHttp" not in created.json()
    assert "public.example.test" not in created.text


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


def test_missing_bound_secret_can_be_saved_disabled_but_cannot_be_activated(
    tmp_path,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    payload = managed_http_server()
    payload["enabled"] = False
    created = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=payload,
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
    active_proxy = client.app.state.mcp_proxy
    enabled = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1/enabled",
        headers=ADMIN_HEADERS,
        json={"enabled": True},
    )
    assert enabled.status_code == 503
    assert enabled.json()["detail"]["code"] == "MCP_SECRET_UNAVAILABLE"
    assert "mcp.primary" not in enabled.text
    assert client.app.state.mcp_proxy is active_proxy
    assert client.get(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
    ).json()["enabled"] is False


@pytest.mark.parametrize("operation", ["upsert", "rotate", "revoke"])
def test_secret_candidate_publish_failure_restores_old_secret_via_api(
    tmp_path,
    monkeypatch,
    operation,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    @asynccontextmanager
    async def fake_session(_self, server):
        class FakeSession:
            async def list_tools(self):
                return SimpleNamespace(tools=[])

        yield FakeSession(), {
            "serverName": server.expected_server_name,
            "serverVersion": "1.0.0",
        }, ()

    monkeypatch.setattr(McpProxy, "_session", fake_session)
    original_value = "old-managed-secret"
    created_secret = client.put(
        "/api/actiondesign-agent/admin/secrets/mcp.primary",
        headers=ADMIN_HEADERS,
        json={
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP key",
            "value": original_value,
        },
    )
    assert created_secret.status_code == 200
    created_server = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=managed_http_server(),
    )
    assert created_server.status_code == 200, created_server.text
    old_proxy = client.app.state.mcp_proxy
    old_metadata = client.app.state.managed_secrets.get("mcp.primary")

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("simulated candidate publish failure")

    monkeypatch.setattr(
        client.app.state.mcp_server_store,
        "commit_candidate",
        fail_commit,
    )
    if operation == "rotate":
        failed = client.post(
            "/api/actiondesign-agent/admin/secrets/mcp.primary/rotate",
            headers=ADMIN_HEADERS,
            json={"value": "new-managed-secret"},
        )
    elif operation == "upsert":
        failed = client.put(
            "/api/actiondesign-agent/admin/secrets/mcp.primary",
            headers=ADMIN_HEADERS,
            json={
                "id": "mcp.primary",
                "kind": "mcp_key",
                "displayName": "Candidate MCP key",
                "value": "new-managed-secret",
            },
        )
    else:
        failed = client.delete(
            "/api/actiondesign-agent/admin/secrets/mcp.primary",
            headers=ADMIN_HEADERS,
        )

    assert failed.status_code == 500
    assert client.app.state.mcp_proxy is old_proxy
    assert client.app.state.managed_secrets.get("mcp.primary") == old_metadata
    assert (
        client.app.state.managed_secrets.resolve_value("mcp.primary")
        == original_value
    )
    persisted = settings.managed_secret_store_path.read_text(encoding="utf-8")
    assert "Candidate MCP key" not in persisted
    assert "new-managed-secret" not in persisted


def test_secret_revoke_swaps_to_fail_closed_proxy_and_old_proxy_can_drain(
    tmp_path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    original_session = McpProxy._session

    @asynccontextmanager
    async def fake_session(_self, server):
        class FakeSession:
            async def list_tools(self):
                return SimpleNamespace(tools=[])

        yield FakeSession(), {
            "serverName": server.expected_server_name,
            "serverVersion": "1.0.0",
        }, ()

    monkeypatch.setattr(McpProxy, "_session", fake_session)
    old_value = "old-managed-secret"
    assert client.put(
        "/api/actiondesign-agent/admin/secrets/mcp.primary",
        headers=ADMIN_HEADERS,
        json={
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP key",
            "value": old_value,
        },
    ).status_code == 200
    assert client.put(
        "/api/actiondesign-agent/admin/mcp-servers/managed.server-1",
        headers=ADMIN_HEADERS,
        json=managed_http_server(),
    ).status_code == 200
    old_proxy = client.app.state.mcp_proxy
    monkeypatch.setattr(McpProxy, "_session", original_session)

    revoked = client.delete(
        "/api/actiondesign-agent/admin/secrets/mcp.primary",
        headers=ADMIN_HEADERS,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["configured"] is False
    new_proxy = client.app.state.mcp_proxy
    assert new_proxy is not old_proxy
    assert old_proxy._secret_store.resolve_value("mcp.primary") == old_value
    with pytest.raises(KeyError):
        new_proxy._secret_store.resolve_value("mcp.primary")

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


def test_environment_http_allowlist_uses_exact_authority_and_path(tmp_path):
    server = {
        "id": "environment.server-port",
        "transport": "streamable_http",
        "url": "https://mcp.example.test:8443/mcp",
    }
    settings = make_settings(
        tmp_path,
        mcp_servers=[server],
        mcp_allowed_http_urls=["https://mcp.example.test:8443/mcp"],
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    assert client.get(
        "/api/actiondesign-agent/admin/mcp-servers",
        headers=ADMIN_HEADERS,
    ).json()[0]["id"] == "environment.server-port"

    crossed = {
        **server,
        "url": "https://mcp.example.test:8443/other",
    }
    with pytest.raises(ValueError, match="non-global"):
        create_app(
            make_settings(
                tmp_path,
                mcp_servers=[crossed],
                mcp_allowed_http_urls=[
                    "https://mcp.example.test:8443/mcp",
                    "https://other.example.test:8443/other",
                ],
            ),
            profile_secret_store=MemoryProfileSecretStore(),
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
