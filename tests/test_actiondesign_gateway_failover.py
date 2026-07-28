import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway import app as gateway_app
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings


CONVERSATION_ID = "66666666-6666-4666-8666-666666666666"
RUN_ID = "run_66666666-6666-4666-8666-666666666666"


def make_settings(tmp_path, **overrides):
    values = {
        "default_provider": "mimo",
        "allow_origins": ["https://agent.example.test"],
        "log_root": tmp_path / "audit",
        "full_conversation_log_enabled": True,
        "full_conversation_log_root": tmp_path / "conversations",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "mimo_api_key": "",
        "claude_code_api_key": "",
        "profile_store_path": tmp_path / "profiles.json",
        "profile_admin_token": "legacy-admin-token",
        "admin_password": "ailxhmm12",
        "admin_allow_remote": True,
        "browser_api_key_store_path": tmp_path / "secrets" / "browser-key.json",
        "profile_allowed_hosts": ["api.example.test", "api.anthropic.com", "token-plan-cn.xiaomimimo.com"],
        "langsmith_enabled": False,
        "langsmith_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def make_client(tmp_path, **overrides):
    return TestClient(
        create_app(
            make_settings(tmp_path, **overrides),
            profile_secret_store=MemoryProfileSecretStore(),
        )
    )


def admin_headers():
    return {"Authorization": "Bearer legacy-admin-token"}


def add_profile(client, profile_id, *, priority, models, default_model=None):
    response = client.put(
        f"/api/actiondesign-agent/admin/profiles/{profile_id}",
        headers=admin_headers(),
        json={
            "id": profile_id,
            "provider": profile_id,
            "displayName": profile_id,
            "protocol": "openai",
            "baseUrl": "https://api.example.test/v1",
            "apiKey": f"secret-{profile_id}",
            "defaultModel": default_model or models[0]["id"],
            "models": models,
            "authMode": "bearer",
            "timeoutSeconds": 30,
            "priority": priority,
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text


def model(model_id, priority):
    return {
        "id": model_id,
        "name": model_id,
        "priority": priority,
        "supportsImages": True,
        "contextWindowTokens": 200000,
        "maxOutputTokens": 8192,
        "supportsNativeTools": True,
        "supportsThinking": True,
    }


def chat_payload(**overrides):
    payload = {
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "systemPrompt": "system secret-free instructions",
        "prompt": "full user request",
        "maxTokens": 1024,
    }
    payload.update(overrides)
    return payload


def identity_headers(**extra):
    return {
        "X-Conversation-Id": CONVERSATION_ID,
        "X-Agent-Run-Id": RUN_ID,
        **extra,
    }


def success(req, profile):
    return {
        "provider": profile.provider,
        "model": req.model,
        "content": f"ok:{profile.id}:{req.model}",
        "tool_calls": [],
        "success": True,
        "error": None,
        "code": None,
        "duration_ms": 1,
        "usage": {},
    }


def test_explicit_route_fails_over_same_channel_then_other_channel(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(
        client,
        "channel-a",
        priority=1000,
        models=[model("a-primary", 500), model("a-secondary", 400)],
    )
    add_profile(
        client,
        "channel-b",
        priority=900,
        models=[model("b-default", 100)],
    )
    calls = []

    async def fake_call(req, profile):
        calls.append((profile.id, req.model))
        if len(calls) < 3:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PROVIDER_TIMEOUT",
                    "message": "temporary upstream failure",
                    "retryable": True,
                    "failoverScope": "model",
                },
            )
        return success(req, profile)

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(),
        json=chat_payload(provider="channel-a", profileId="channel-a", model="a-primary"),
    )

    assert response.status_code == 200
    assert calls == [
        ("channel-a", "a-primary"),
        ("channel-a", "a-secondary"),
        ("channel-b", "b-default"),
    ]
    assert response.headers["x-route-attempts"] == "3"
    assert response.headers["x-actual-profile"] == "channel-b"
    log_response = client.get(
        "/api/actiondesign-agent/admin/request-logs",
        headers=admin_headers(),
    )
    log_items = log_response.json()["items"]
    assert {item["attempt"] for item in log_items} == {1, 2, 3}
    request_id = response.headers["x-request-id"]
    assert {item["requestId"] for item in log_items} == {request_id}
    detail = client.get(
        f"/api/actiondesign-agent/admin/request-logs/{request_id}",
        headers=admin_headers(),
    ).json()
    assert detail["requestId"] == request_id
    assert detail["attemptCount"] == 3
    assert [item["attempt"] for item in detail["attempts"]] == [1, 2, 3]
    assert {item["requestId"] for item in detail["attempts"]} == {request_id}
    conversation_file = next((tmp_path / "conversations").glob("*.jsonl"))
    events = [json.loads(line) for line in conversation_file.read_text(encoding="utf-8").splitlines()]
    assert [item["turnStatus"] for item in events] == [
        "running",
        "fallback",
        "running",
        "fallback",
        "running",
        "completed",
    ]
    assert events[0]["request"]["prompt"] == "full user request"


def test_auth_failure_skips_remaining_models_for_same_profile(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(client, "channel-a", priority=1000, models=[model("a1", 20), model("a2", 10)])
    add_profile(client, "channel-b", priority=900, models=[model("b1", 10)])
    calls = []

    async def fake_call(req, profile):
        calls.append((profile.id, req.model))
        if profile.id == "channel-a":
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "PROVIDER_AUTH_FAILED",
                    "message": "bad provider credential",
                    "retryable": True,
                    "failoverScope": "profile",
                },
            )
        return success(req, profile)

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(),
        json=chat_payload(provider="channel-a", model="a1"),
    )

    assert response.status_code == 200
    assert calls == [("channel-a", "a1"), ("channel-b", "b1")]


def test_deterministic_upstream_error_does_not_fail_over(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(client, "channel-a", priority=1000, models=[model("a1", 20), model("a2", 10)])
    calls = []

    async def fake_call(req, profile):
        calls.append((profile.id, req.model))
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PROVIDER_HTTP_ERROR",
                "message": "provider rejected invalid parameters",
                "retryable": False,
                "failoverScope": "none",
            },
        )

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(),
        json=chat_payload(provider="channel-a", model="a1"),
    )

    assert response.status_code == 502
    assert calls == [("channel-a", "a1")]


def test_auto_route_uses_highest_profile_default_then_model_priority(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(client, "channel-a", priority=100, models=[model("a1", 100)])
    add_profile(
        client,
        "channel-b",
        priority=200,
        models=[model("b-default", 10), model("b-high", 500)],
        default_model="b-default",
    )
    calls = []

    async def fake_call(req, profile):
        calls.append((profile.id, req.model))
        return success(req, profile)

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(),
        json=chat_payload(),
    )
    models_response = client.get("/api/actiondesign-agent/models").json()

    assert response.status_code == 200
    assert calls == [("channel-b", "b-default")]
    assert models_response["defaultProvider"] == "channel-b"
    assert models_response["defaultModel"] == "b-default"
    assert models_response["routingOrder"][0] == {
        "position": 1,
        "provider": "channel-b",
        "profileId": "channel-b",
        "protocol": "openai",
        "model": "b-default",
        "profilePriority": 200,
        "modelPriority": 10,
        "isDefaultProfile": True,
        "isDefaultModel": True,
    }


def test_stream_fails_over_only_before_first_visible_delta(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(client, "channel-a", priority=1000, models=[model("a1", 10)])
    add_profile(client, "channel-b", priority=900, models=[model("b1", 10)])
    calls = []

    async def fake_stream(req, profile):
        calls.append((profile.id, req.model))
        if profile.id == "channel-a":
            raise HTTPException(
                status_code=504,
                detail={
                    "code": "PROVIDER_TIMEOUT",
                    "message": "before first byte",
                    "retryable": True,
                    "failoverScope": "model",
                },
            )
        yield {"kind": "delta", "content": "fallback output"}
        yield {"kind": "completed", "payload": success(req, profile)}

    monkeypatch.setattr(gateway_app, "stream_provider", fake_stream)
    response = client.post(
        "/api/actiondesign-agent/chat/stream",
        headers=identity_headers(),
        json=chat_payload(provider="channel-a", model="a1", stream=True),
    )

    assert response.status_code == 200
    assert calls == [("channel-a", "a1"), ("channel-b", "b1")]
    assert "fallback output" in response.text
    assert '"type": "model.completed"' in response.text


def test_stream_does_not_switch_after_visible_delta(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    add_profile(client, "channel-a", priority=1000, models=[model("a1", 10)])
    add_profile(client, "channel-b", priority=900, models=[model("b1", 10)])
    calls = []

    async def fake_stream(req, profile):
        calls.append((profile.id, req.model))
        yield {"kind": "delta", "content": "partial"}
        raise HTTPException(
            status_code=504,
            detail={
                "code": "PROVIDER_TIMEOUT",
                "message": "after output",
                "retryable": True,
                "failoverScope": "model",
            },
        )

    monkeypatch.setattr(gateway_app, "stream_provider", fake_stream)
    response = client.post(
        "/api/actiondesign-agent/chat/stream",
        headers=identity_headers(),
        json=chat_payload(provider="channel-a", model="a1", stream=True),
    )

    assert calls == [("channel-a", "a1")]
    assert "partial" in response.text
    assert '"type": "gateway.error"' in response.text


def test_admin_login_browser_key_audit_and_conversation_endpoints(tmp_path, monkeypatch):
    client = make_client(
        tmp_path,
        mimo_api_key="mimo-secret",
        mimo_default_model="mimo-v2.5",
    )

    async def fake_mimo(req, profile):
        return success(req, profile)

    monkeypatch.setattr(gateway_app, "call_mimo", fake_mimo)
    login = client.post(
        "/api/actiondesign-agent/admin/login",
        json={"password": "ailxhmm12"},
    )
    assert login.status_code == 200
    session = login.json()["sessionToken"]
    session_headers = {"Authorization": f"Bearer {session}"}
    rotated = client.post(
        "/api/actiondesign-agent/admin/access-key/rotate",
        headers=session_headers,
        json={},
    )
    assert rotated.status_code == 200
    static_key = rotated.json()["apiKey"]
    key_file = tmp_path / "secrets" / "browser-key.json"
    assert static_key not in key_file.read_text(encoding="utf-8")
    status = client.get(
        "/api/actiondesign-agent/admin/access-key",
        headers=session_headers,
    ).json()
    assert status == {"configured": True, "masked": rotated.json()["masked"]}

    denied = client.post(
        "/api/actiondesign-agent/chat",
        json=chat_payload(provider="mimo", model="mimo-v2.5"),
    )
    accepted = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(**{"X-ActionDesign-Api-Key": session}),
        json=chat_payload(provider="mimo", model="mimo-v2.5"),
    )
    assert denied.status_code == 401
    assert accepted.status_code == 200

    conversations = client.get(
        "/api/actiondesign-agent/admin/conversations",
        headers=session_headers,
    ).json()
    assert conversations["items"][0]["status"] == "open"
    detail = client.get(
        f"/api/actiondesign-agent/admin/conversations/{CONVERSATION_ID}",
        headers=session_headers,
    ).json()
    assert detail["items"][0]["request"]["prompt"] == "full user request"
    sealed = client.post(
        f"/api/actiondesign-agent/admin/conversations/{CONVERSATION_ID}/seal",
        headers=session_headers,
        json={"status": "completed"},
    )
    assert sealed.json() == {"success": True, "status": "completed"}
    conversations = client.get(
        "/api/actiondesign-agent/admin/conversations",
        headers=session_headers,
    ).json()
    assert conversations["items"][0]["status"] == "completed"

    audit = client.get(
        "/api/actiondesign-agent/admin/audit-logs",
        headers=session_headers,
    )
    audit_text = audit.text
    assert audit.status_code == 200
    assert "admin_login" in audit_text
    assert "browser_api_key_rotate" in audit_text
    assert "ailxhmm12" not in audit_text
    assert static_key not in audit_text


def test_admin_login_rate_limits_after_five_failures(tmp_path):
    client = make_client(tmp_path)
    for _ in range(5):
        response = client.post(
            "/api/actiondesign-agent/admin/login",
            json={"password": "wrong-password"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/actiondesign-agent/admin/login",
        json={"password": "ailxhmm12"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "ADMIN_LOGIN_RATE_LIMITED"


def test_admin_remote_access_is_disabled_by_default(tmp_path):
    client = make_client(tmp_path, admin_allow_remote=False)

    login = client.post(
        "/api/actiondesign-agent/admin/login",
        json={"password": "ailxhmm12"},
    )
    profiles = client.get(
        "/api/actiondesign-agent/admin/profiles",
        headers=admin_headers(),
    )

    assert login.status_code == 403
    assert login.json()["detail"]["code"] == "ADMIN_REMOTE_DISABLED"
    assert profiles.status_code == 403


def test_admin_login_is_disabled_without_server_password(tmp_path):
    client = make_client(tmp_path, admin_password="")

    response = client.post(
        "/api/actiondesign-agent/admin/login",
        json={"password": "ailxhmm12"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ADMIN_LOGIN_DISABLED"
