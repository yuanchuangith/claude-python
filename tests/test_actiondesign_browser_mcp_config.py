from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.mcp_server_store import (  # noqa: E402
    McpServerStore,
)


ORIGIN = "https://actiondesign.example.test"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "allow_origins": [ORIGIN],
        "service_auth_mode": "disabled",
        "service_auth_token": "",
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
        "client_api_key_store_path": tmp_path / "client-keys.json",
        "profile_allowed_hosts": [
            "api.anthropic.com",
            "token-plan-cn.xiaomimimo.com",
        ],
        "mimo_api_key": "",
        "claude_code_api_key": "",
        "mcp_proxy_enabled": False,
        "public_browser_mcp_config": True,
        "mcp_servers": [],
        "mcp_allowed_http_urls": [],
        "mcp_allowed_executable_paths": [],
    }
    values.update(overrides)
    return Settings(**values)


def browser_server(
    server_id: str = "browser.server-1",
    *,
    secret_id: str = "mcp.shared",
    secret_value: str = "browser-secret-value-1234",
    expected_version: int = 0,
) -> dict[str, object]:
    return {
        "id": server_id,
        "displayName": f"Browser MCP {server_id}",
        "transport": "streamable_http",
        "expectedConfigVersion": expected_version,
        "expectedServerName": "",
        "timeoutSeconds": 30.0,
        "enabled": True,
        "streamableHttp": {
            "url": "https://mcp.example.test/mcp",
            "staticHeaders": {"X-Tenant": "quality"},
            "secretBindings": [
                {
                    "secretId": secret_id,
                    "name": "Authorization",
                    "valuePrefix": "Bearer ",
                    "value": secret_value,
                }
            ],
            "sseReadTimeoutSeconds": 300.0,
        },
    }


def make_client(tmp_path: Path, **overrides: object) -> TestClient:
    return TestClient(
        create_app(
            make_settings(tmp_path, **overrides),
            profile_secret_store=MemoryProfileSecretStore(),
        )
    )


def test_public_browser_mcp_crud_returns_complete_secret_without_api_key(tmp_path):
    client = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    secret = "browser-secret-value-1234"

    created = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.server-1",
        headers=headers,
        json=browser_server(secret_value=secret),
    )

    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["configVersion"] == 1
    assert created.json()["executionMode"] == "browser_direct"
    assert created.json()["streamableHttp"]["headers"] == {
        "X-Tenant": "quality",
        "Authorization": f"Bearer {secret}",
    }
    assert set(created.json()) == {
        "id",
        "displayName",
        "source",
        "editable",
        "transport",
        "executionMode",
        "browserSupported",
        "expectedServerName",
        "timeoutSeconds",
        "enabled",
        "configVersion",
        "configStatus",
        "errorCode",
        "streamableHttp",
    }
    assert set(created.json()["streamableHttp"]) == {
        "url",
        "headers",
        "protocolVersion",
        "sseReadTimeoutSeconds",
    }

    listed = client.get(
        "/api/actiondesign-agent/mcp-browser/servers",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json() == [created.json()]
    assert "Authorization" not in headers

    metadata = (tmp_path / "managed-mcp-servers.json").read_text(encoding="utf-8")
    assert secret not in metadata
    audit = (tmp_path / "logs" / "system" / "admin-audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in audit


def test_public_browser_mcp_uses_optimistic_versions_for_all_mutations(tmp_path):
    client = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    path = "/api/actiondesign-agent/mcp-browser/servers/browser.server-1"
    assert client.put(path, headers=headers, json=browser_server()).status_code == 200

    stale = browser_server(expected_version=0)
    stale["displayName"] = "stale"
    conflict = client.put(path, headers=headers, json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "MCP_CONFIG_VERSION_CONFLICT",
        "serverId": "browser.server-1",
        "expectedConfigVersion": 0,
        "currentConfigVersion": 1,
    }

    enabled = client.put(
        f"{path}/enabled",
        headers=headers,
        json={"enabled": False, "expectedConfigVersion": 1},
    )
    assert enabled.status_code == 200
    assert enabled.json()["configVersion"] == 2
    assert enabled.json()["enabled"] is False

    stale_delete = client.request(
        "DELETE",
        path,
        headers=headers,
        json={"expectedConfigVersion": 1},
    )
    assert stale_delete.status_code == 409
    deleted = client.request(
        "DELETE",
        path,
        headers=headers,
        json={"expectedConfigVersion": 2},
    )
    assert deleted.status_code == 200
    assert client.get(path, headers=headers).status_code == 404


def test_shared_secret_change_increments_each_server_once(tmp_path):
    client = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    first = browser_server(server_id="browser.first")
    second = browser_server(server_id="browser.second")
    assert client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.first",
        headers=headers,
        json=first,
    ).status_code == 200
    assert client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.second",
        headers=headers,
        json=second,
    ).status_code == 200

    updated = browser_server(
        server_id="browser.first",
        secret_value="rotated-browser-secret-5678",
        expected_version=1,
    )
    response = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.first",
        headers=headers,
        json=updated,
    )
    assert response.status_code == 200, response.text

    listed = {
        item["id"]: item
        for item in client.get(
            "/api/actiondesign-agent/mcp-browser/servers",
            headers=headers,
        ).json()
    }
    assert listed["browser.first"]["configVersion"] == 2
    assert listed["browser.second"]["configVersion"] == 2
    assert listed["browser.second"]["streamableHttp"]["headers"]["Authorization"] == (
        "Bearer rotated-browser-secret-5678"
    )


def test_http_gateway_runtime_is_rejected_and_connection_test_does_not_connect(tmp_path):
    client = make_client(tmp_path, mcp_proxy_enabled=False)
    headers = {"Origin": ORIGIN}
    path = "/api/actiondesign-agent/mcp-browser/servers/browser.server-1"
    assert client.put(path, headers=headers, json=browser_server()).status_code == 200

    checked = client.post(f"{path}/connection-test", headers=headers)
    assert checked.status_code == 200
    assert checked.json()["status"] == "valid"
    assert checked.json()["checks"] == [
        "strict_schema",
        "http_url",
        "header_names",
        "secret_bindings",
        "config_version",
    ]

    runtime = client.get(
        "/api/actiondesign-agent/mcp/browser.server-1/tools",
        headers={
            "X-Request-Id": "88888888-8888-4888-8888-888888888888",
            "X-Correlation-Id": "99999999-9999-4999-8999-999999999999",
        },
    )
    assert runtime.status_code == 409
    assert runtime.json()["detail"]["code"] == "MCP_BROWSER_DIRECT_REQUIRED"


def test_public_browser_mcp_feature_flag_and_origin_fail_closed(tmp_path):
    disabled = make_client(tmp_path / "disabled", public_browser_mcp_config=False)
    assert disabled.get("/api/actiondesign-agent/mcp-browser/servers").status_code == 404

    client = make_client(tmp_path / "enabled")
    denied = client.get(
        "/api/actiondesign-agent/mcp-browser/servers",
        headers={"Origin": "https://evil.example.test"},
    )
    assert denied.status_code == 403


def test_browser_write_rejects_stdio_and_reserved_headers(tmp_path):
    client = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    payload = browser_server()
    payload["transport"] = "stdio"
    assert client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.server-1",
        headers=headers,
        json=payload,
    ).status_code == 422

    reserved = browser_server()
    reserved["streamableHttp"]["staticHeaders"] = {"Mcp-Session-Id": "bad"}
    assert client.put(
        "/api/actiondesign-agent/mcp-browser/servers/browser.server-1",
        headers=headers,
        json=reserved,
    ).status_code == 422


def test_browser_final_headers_accept_http_and_store_sensitive_values_as_secrets(
    tmp_path,
):
    client = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    secret = "browser-final-token-1234"
    payload = {
        "id": "gxp-lowcode-readonly",
        "displayName": "GXP low-code readonly",
        "transport": "streamable_http",
        "expectedConfigVersion": 0,
        "expectedServerName": "gxp-lowcode-readonly",
        "timeoutSeconds": 30.0,
        "enabled": True,
        "streamableHttp": {
            "url": "http://43.135.137.212:8890/mcp",
            "headers": {
                "X-Tenant": "quality",
                "Authorization": secret,
            },
            "sseReadTimeoutSeconds": 120.0,
        },
    }

    created = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/gxp-lowcode-readonly",
        headers=headers,
        json=payload,
    )

    assert created.status_code == 200, created.text
    streamable_http = created.json()["streamableHttp"]
    assert streamable_http["url"] == "http://43.135.137.212:8890/mcp"
    assert streamable_http["headers"] == {
        "X-Tenant": "quality",
        "Authorization": secret,
    }
    assert streamable_http["protocolVersion"] == "2025-06-18"
    assert streamable_http["sseReadTimeoutSeconds"] == 120.0
    assert set(streamable_http) == {
        "url",
        "headers",
        "protocolVersion",
        "sseReadTimeoutSeconds",
    }
    metadata = (tmp_path / "managed-mcp-servers.json").read_text(encoding="utf-8")
    assert secret not in metadata
    reloaded = McpServerStore(tmp_path / "managed-mcp-servers.json", [])
    saved = reloaded.get_config("gxp-lowcode-readonly")
    assert saved is not None
    assert saved.streamable_http is not None
    assert saved.streamable_http.url == "http://43.135.137.212:8890/mcp"

    mixed = dict(payload)
    mixed["expectedConfigVersion"] = 1
    mixed["streamableHttp"] = dict(payload["streamableHttp"])
    mixed["streamableHttp"]["staticHeaders"] = {"X-Legacy": "invalid"}
    rejected = client.put(
        "/api/actiondesign-agent/mcp-browser/servers/gxp-lowcode-readonly",
        headers=headers,
        json=mixed,
    )
    assert rejected.status_code == 422


def test_admin_and_browser_routes_edit_the_same_managed_server_with_one_version(
    tmp_path,
):
    client = make_client(tmp_path)
    public_headers = {"Origin": ORIGIN}
    path = "/api/actiondesign-agent/mcp-browser/servers/browser.server-1"
    created = client.put(path, headers=public_headers, json=browser_server())
    assert created.status_code == 200

    admin_read = client.get(
        "/api/actiondesign-agent/admin/mcp-servers/browser.server-1",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert admin_read.status_code == 200
    assert admin_read.json()["id"] == created.json()["id"]
    assert admin_read.json()["configVersion"] == created.json()["configVersion"]
    assert admin_read.json()["streamableHttp"]["headers"] == (
        created.json()["streamableHttp"]["headers"]
    )

    admin_update = browser_server(expected_version=1)
    admin_update["displayName"] = "Updated by administrator"
    updated = client.put(
        "/api/actiondesign-agent/admin/mcp-servers/browser.server-1",
        headers={"Authorization": "Bearer admin-token"},
        json=admin_update,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["configVersion"] == 2
    assert updated.json()["displayName"] == "Updated by administrator"

    stale_browser = client.put(
        path,
        headers=public_headers,
        json=browser_server(expected_version=1),
    )
    assert stale_browser.status_code == 409
    assert stale_browser.json()["detail"]["currentConfigVersion"] == 2


def test_revoked_secret_invalidates_projection_without_partial_headers(tmp_path):
    client = make_client(tmp_path)
    public_headers = {"Origin": ORIGIN}
    path = "/api/actiondesign-agent/mcp-browser/servers/browser.server-1"
    assert client.put(path, headers=public_headers, json=browser_server()).status_code == 200

    revoked = client.delete(
        "/api/actiondesign-agent/admin/secrets/mcp.shared",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert revoked.status_code == 200
    projected = client.get(path, headers=public_headers)
    assert projected.status_code == 200
    assert projected.json()["configVersion"] == 2
    assert projected.json()["configStatus"] == "invalid"
    assert projected.json()["errorCode"] == "MCP_SECRET_UNAVAILABLE"
    assert projected.json()["streamableHttp"] is None


def test_v1_http_gateway_metadata_migrates_to_v3_browser_direct(tmp_path):
    path = tmp_path / "managed-mcp-servers.json"
    path.write_text(
        """{
  "schemaVersion": "1",
  "servers": [{
    "id": "legacy.http",
    "displayName": "Legacy HTTP",
    "transport": "streamable_http",
    "executionMode": "gateway_proxy",
    "authMode": "none",
    "timeoutSeconds": 30.0,
    "enabled": true,
    "streamableHttp": {
      "url": "https://mcp.example.test/mcp",
      "headers": {},
      "secretBindings": [],
      "sseReadTimeoutSeconds": 300.0
    }
  }]
}""",
        encoding="utf-8",
    )

    store = McpServerStore(path, [])
    assert store.get_config("legacy.http").execution_mode == "browser_direct"
    migrated = path.read_text(encoding="utf-8")
    assert '"schemaVersion": "3"' in migrated
    assert '"executionMode": "browser_direct"' in migrated
