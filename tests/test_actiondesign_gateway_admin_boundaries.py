import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway import app as gateway_app
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings

ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
CONVERSATION_ID = "77777777-7777-4777-8777-777777777777"
RUN_ID = "run_77777777-7777-4777-8777-777777777777"
REQUEST_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "default_provider": "mimo",
        "allow_origins": ["*"],
        "log_root": tmp_path / "audit",
        "full_conversation_log_root": tmp_path / "conversations",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "profile_store_path": tmp_path / "profiles.json",
        "managed_secret_store_path": tmp_path / "managed-secrets.json",
        "profile_admin_token": "admin-token",
        "admin_allow_remote": True,
        "admin_require_https_remote": False,
        "mimo_api_key": "mimo-environment-secret",
        "mimo_messages_url": "https://provider.example.test/anthropic/v1/messages",
        "mimo_default_model": "mimo-v2.5",
        "claude_code_api_key": "",
        "claude_code_messages_url": "https://provider.example.test/v1/messages",
        "claude_code_default_model": "claude-test",
        "profile_allowed_hosts": ["provider.example.test"],
    }
    values.update(overrides)
    return Settings(**values)


def make_client(tmp_path: Path, **overrides) -> tuple[TestClient, Settings]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    return TestClient(app), settings


def provider_payload(secret: str = "provider-secret-1234") -> dict:
    return {
        "id": "team-openai",
        "provider": "openai",
        "displayName": "Team OpenAI",
        "protocol": "openai",
        "baseUrl": "https://provider.example.test/proxy",
        "apiKey": secret,
        "defaultModel": "model-a",
        "models": [
            {
                "id": "model-a",
                "supportsImages": True,
                "supportsNativeTools": True,
                "supportsThinking": False,
                "supportsStreaming": True,
            }
        ],
        "authMode": "bearer",
        "timeoutSeconds": 30,
        "priority": 200,
        "enabled": True,
    }


def test_provider_model_default_and_profile_secret_lifecycle(tmp_path):
    client, _settings = make_client(tmp_path)
    original_secret = "provider-secret-1234"

    assert client.get("/api/actiondesign-agent/admin/providers").status_code == 401
    created = client.put(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
        json=provider_payload(original_secret),
    )
    assert created.status_code == 200
    assert created.json()["providerPath"] == "/v1/chat/completions"
    assert created.json()["secretMasked"] == "****1234"
    assert original_secret not in created.text

    listed = client.get(
        "/api/actiondesign-agent/admin/providers",
        headers=ADMIN_HEADERS,
    )
    fetched = client.get(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
    )
    assert listed.status_code == fetched.status_code == 200
    assert original_secret not in listed.text + fetched.text
    model_b = {
        "id": "model-b",
        "name": "Model B",
        "priority": 50,
        "supportsImages": False,
        "contextWindowTokens": 65536,
        "maxOutputTokens": 4096,
        "supportsNativeTools": True,
        "supportsThinking": True,
        "supportsStreaming": True,
    }
    created_model = client.post(
        "/api/actiondesign-agent/admin/profiles/team-openai/models",
        headers=ADMIN_HEADERS,
        json=model_b,
    )
    assert created_model.status_code == 200
    assert created_model.json()["supportsThinking"] is True

    defaulted = client.put(
        "/api/actiondesign-agent/admin/profiles/team-openai/default-model",
        headers=ADMIN_HEADERS,
        json={"modelId": "model-b"},
    )
    assert defaulted.status_code == 200
    assert defaulted.json()["defaultModel"] == "model-b"
    assert client.delete(
        "/api/actiondesign-agent/admin/profiles/team-openai/models/model-b",
        headers=ADMIN_HEADERS,
    ).status_code == 409

    replacement = "replacement-provider-secret-5678"
    rotated = "rotated-provider-secret-9012"
    updated = client.put(
        "/api/actiondesign-agent/admin/profiles/team-openai/secret",
        headers=ADMIN_HEADERS,
        json={"value": replacement},
    )
    rotation = client.post(
        "/api/actiondesign-agent/admin/profiles/team-openai/secret/rotate",
        headers=ADMIN_HEADERS,
        json={"value": rotated},
    )
    revoked = client.delete(
        "/api/actiondesign-agent/admin/profiles/team-openai/secret",
        headers=ADMIN_HEADERS,
    )
    assert updated.json()["masked"] == "****5678"
    assert rotation.json()["masked"] == "****9012"
    assert revoked.json()["configured"] is False
    combined = updated.text + rotation.text + revoked.text
    assert replacement not in combined
    assert rotated not in combined

    assert client.put(
        "/api/actiondesign-agent/admin/profiles/team-openai/default-model",
        headers=ADMIN_HEADERS,
        json={"modelId": "model-a"},
    ).status_code == 200
    assert client.delete(
        "/api/actiondesign-agent/admin/profiles/team-openai/models/model-b",
        headers=ADMIN_HEADERS,
    ).json() == {"success": True}
    assert client.delete(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
    ).json() == {"success": True}


def test_environment_provider_can_be_deleted_and_stays_deleted_after_reload(tmp_path):
    client, _settings = make_client(tmp_path)

    deleted = client.delete(
        "/api/actiondesign-agent/admin/providers/default-mimo",
        headers=ADMIN_HEADERS,
    )
    listed = client.get(
        "/api/actiondesign-agent/admin/providers",
        headers=ADMIN_HEADERS,
    )
    reloaded, _ = make_client(tmp_path)
    listed_after_reload = reloaded.get(
        "/api/actiondesign-agent/admin/providers",
        headers=ADMIN_HEADERS,
    )

    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert "default-mimo" not in {item["id"] for item in listed.json()}
    assert "default-mimo" not in {item["id"] for item in listed_after_reload.json()}


def test_provider_admin_rejects_non_fixed_endpoint_suffix(tmp_path):
    client, _settings = make_client(tmp_path)
    payload = provider_payload()
    payload["baseUrl"] = "https://provider.example.test/chat/completions"
    response = client.put(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
        json=payload,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PROVIDER_PATH_NOT_ALLOWED"


def test_generic_secret_api_never_returns_plaintext(tmp_path):
    client, settings = make_client(tmp_path)
    first = "generic-mcp-secret-1234"
    second = "generic-mcp-secret-5678"

    created = client.put(
        "/api/actiondesign-agent/admin/secrets/mcp-main",
        headers=ADMIN_HEADERS,
        json={
            "id": "mcp-main",
            "kind": "mcp-api-key",
            "displayName": "Main MCP key",
            "value": first,
        },
    )
    assert created.status_code == 200
    expected_keys = {
        "id",
        "kind",
        "displayName",
        "configured",
        "createdAt",
        "updatedAt",
        "rotatedAt",
        "revokedAt",
    }
    assert set(created.json()) == expected_keys
    assert first not in created.text

    listed = client.get(
        "/api/actiondesign-agent/admin/secrets",
        headers=ADMIN_HEADERS,
    )
    fetched = client.get(
        "/api/actiondesign-agent/admin/secrets/mcp-main",
        headers=ADMIN_HEADERS,
    )
    rotated = client.post(
        "/api/actiondesign-agent/admin/secrets/mcp-main/rotate",
        headers=ADMIN_HEADERS,
        json={"value": second},
    )
    revoked = client.delete(
        "/api/actiondesign-agent/admin/secrets/mcp-main",
        headers=ADMIN_HEADERS,
    )
    assert (
        listed.status_code
        == fetched.status_code
        == rotated.status_code
        == revoked.status_code
        == 200
    )
    assert all(set(item) == expected_keys for item in listed.json())
    assert set(fetched.json()) == set(rotated.json()) == set(revoked.json()) == expected_keys
    assert revoked.json()["configured"] is False
    public_text = listed.text + fetched.text + rotated.text + revoked.text
    assert first not in public_text
    assert second not in public_text
    assert first not in settings.managed_secret_store_path.read_text(encoding="utf-8")
    audit_text = (settings.log_root / "system" / "admin-audit.jsonl").read_text(encoding="utf-8")
    assert first not in audit_text
    assert second not in audit_text


def test_connection_test_updates_health_without_returning_provider_error(tmp_path, monkeypatch):
    client, _settings = make_client(tmp_path)
    assert client.put(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
        json=provider_payload(),
    ).status_code == 200

    async def healthy(_profile, _model):
        return None

    monkeypatch.setattr(gateway_app, "probe_provider", healthy)
    healthy_response = client.post(
        "/api/actiondesign-agent/admin/profiles/team-openai/connection-test",
        headers=ADMIN_HEADERS,
        json={"model": "model-a"},
    )
    assert healthy_response.status_code == 200
    assert healthy_response.json()["status"] == "healthy"

    leaked = "provider-secret-1234"

    async def unhealthy(_profile, _model):
        raise HTTPException(
            status_code=502,
            detail={"code": "PROVIDER_AUTH_FAILED", "message": f"bad {leaked}"},
        )

    monkeypatch.setattr(gateway_app, "probe_provider", unhealthy)
    unhealthy_response = client.post(
        "/api/actiondesign-agent/admin/profiles/team-openai/connection-test",
        headers=ADMIN_HEADERS,
        json={"model": "model-a"},
    )
    assert unhealthy_response.status_code == 200
    assert unhealthy_response.json()["status"] == "unhealthy"
    assert unhealthy_response.json()["errorCode"] == "PROVIDER_AUTH_FAILED"
    assert leaked not in unhealthy_response.text
    health = client.get(
        "/api/actiondesign-agent/admin/channels/health",
        headers=ADMIN_HEADERS,
    )
    assert health.status_code == 200
    assert any(item["profileId"] == "team-openai" for item in health.json())


def test_generic_mcp_routes_are_transport_only_and_have_no_fixed_business_contract(tmp_path):
    service_token = "service-token-" + ("x" * 32)
    executable = Path(sys.executable).resolve()
    client, _settings = make_client(
        tmp_path,
        allow_origins=["https://actiondesign.example.test"],
        service_auth_mode="required",
        service_auth_token=service_token,
        mcp_proxy_enabled=True,
        mcp_servers=[
            {
                "id": "generic-tools",
                "transport": "stdio",
                "executablePath": str(executable),
                "args": [],
            }
        ],
        mcp_allowed_executable_paths=[str(executable)],
    )

    class FakeProxy:
        async def list_tools(self, server_id):
            assert server_id == "generic-tools"
            return {
                "serverId": server_id,
                "serverName": "generic-server",
                "serverVersion": "1.0",
                "tools": [
                    {
                        "name": "echo.value",
                        "description": "Echo an arbitrary value",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }

        async def call_tool(self, server_id, tool_name, arguments):
            assert (server_id, tool_name) == ("generic-tools", "echo.value")
            return {
                "serverId": server_id,
                "serverName": "generic-server",
                "serverVersion": "1.0",
                "toolName": tool_name,
                "durationMs": 2,
                "result": {
                    "content": [{"type": "text", "text": arguments["value"]}],
                    "structuredContent": {
                        "echo": arguments["value"],
                        "details": "generic MCP result " * 3_000,
                    },
                    "isError": False,
                },
            }

    client.app.state.mcp_proxy = FakeProxy()
    headers = {
        "X-ActionDesign-Service-Token": service_token,
        "X-Request-Id": REQUEST_ID,
        "X-Correlation-Id": CORRELATION_ID,
    }
    missing_identity = client.get(
        "/api/actiondesign-agent/mcp/generic-tools/tools",
        headers={"X-ActionDesign-Service-Token": service_token},
    )
    listed = client.get(
        "/api/actiondesign-agent/mcp/generic-tools/tools",
        headers=headers,
    )
    assert missing_identity.status_code == 422
    assert listed.status_code == 200
    listed_request_id = listed.headers["x-request-id"]
    assert listed_request_id == REQUEST_ID
    assert listed.headers["x-correlation-id"] == CORRELATION_ID
    assert (tmp_path / "audit" / listed_request_id / "mcp-tools.jsonl").is_file()
    payload = {
        "toolName": "echo.value",
        "arguments": {"value": "opaque-result"},
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "requestId": REQUEST_ID,
        "correlationId": CORRELATION_ID,
    }
    call_headers = {
        **headers,
        "X-Conversation-Id": CONVERSATION_ID,
        "X-Agent-Run-Id": RUN_ID,
    }
    called = client.post(
        "/api/actiondesign-agent/mcp/generic-tools/call",
        headers=call_headers,
        json=payload,
    )
    assert called.status_code == 200
    assert called.headers["x-request-id"] == REQUEST_ID
    assert called.json()["result"]["structuredContent"]["echo"] == "opaque-result"
    assert called.json()["result"]["isError"] is False
    assert "skill" not in called.text.lower()
    transcript = client.get(
        f"/api/actiondesign-agent/admin/conversations/{CONVERSATION_ID}/transcript",
        headers=ADMIN_HEADERS,
    )
    assert transcript.status_code == 200
    tool_messages = transcript.json()["messages"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["role"] == "tool"
    assert tool_messages[0]["toolResultRefs"][0].startswith("artifact:sha256:")
    restored = json.loads(tool_messages[0]["content"])
    assert restored["structuredContent"]["details"] == "generic MCP result " * 3_000
    assert tool_messages[0]["incompleteReason"] == ""

    schema_text = json.dumps(client.get("/openapi.json").json(), ensure_ascii=False).lower()
    for forbidden in (
        "gxp-lowcode-readonly",
        "readonly_sql",
        "contracthash",
        "/agent/plan",
        "/review",
        "/knowledge/",
        "/canvas/",
    ):
        assert forbidden not in schema_text


def test_invalid_admin_audit_request_id_is_rejected_before_provider_mutation(tmp_path):
    client, _settings = make_client(tmp_path)
    headers = {**ADMIN_HEADERS, "X-Request-Id": "../unsafe"}
    response = client.put(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=headers,
        json=provider_payload(),
    )
    lookup = client.get(
        "/api/actiondesign-agent/admin/providers/team-openai",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422
    assert lookup.status_code == 404
