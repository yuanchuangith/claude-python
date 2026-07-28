import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway import app as gateway_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.models import AgentChatRequest  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
    ProviderProfile,
)
from claude_agent_sdk.actiondesign_gateway.request_log import RequestLogStore  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


def make_settings(tmp_path, **overrides):
    values = {
        "default_provider": "mimo",
        "allow_origins": ["*"],
        "log_root": tmp_path / "audit",
        "full_conversation_log_enabled": True,
        "full_conversation_log_root": tmp_path / "conversations",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "request_log_max_bytes": 10 * 1024 * 1024,
        "request_log_backup_count": 5,
        "mimo_api_key": "mimo-secret",
        "mimo_messages_url": "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages",
        "mimo_default_model": "mimo-v2.5",
        "claude_code_api_key": "claude-secret",
        "claude_code_messages_url": "https://api.anthropic.com/v1/messages",
        "claude_code_default_model": "claude-sonnet-4-5",
        "claude_code_models": ["claude-sonnet-4-5"],
        "profile_store_path": tmp_path / "profiles.json",
        "profile_admin_token": "admin-token",
        "profile_allowed_hosts": [
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
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


def chat_payload(**overrides):
    payload = {
        "provider": "mimo",
        "conversationId": "conv_request_log",
        "runId": "run_request_log",
        "agentMode": "normal",
        "systemPrompt": "System instructions",
        "prompt": "Private business prompt",
        "stream": False,
        "protocolVersion": "1.0",
    }
    payload.update(overrides)
    return payload


def request_model(index: int) -> AgentChatRequest:
    return AgentChatRequest.model_validate(
        chat_payload(
            conversationId=f"conv_{index}",
            runId=f"run_{index}",
            prompt=f"Private prompt {index}",
        )
    )


def provider_profile() -> ProviderProfile:
    return ProviderProfile(
        id="default-mimo",
        provider="mimo",
        base_url="https://token-plan-cn.xiaomimimo.com/anthropic",
        api_key="not-persisted",
        default_model="mimo-v2.5",
        auth_mode="api-key",
        timeout_seconds=90,
        enabled=True,
        source="builtin",
        protocol="claude",
    )


def test_request_log_persists_only_bounded_usage_and_error_fields(tmp_path):
    path = tmp_path / "request-logs.jsonl"
    store = RequestLogStore(path)

    entry = store.record(
        request_id="req_safe",
        request=request_model(1),
        profile=provider_profile(),
        started=time.monotonic(),
        status="error",
        response={
            "model": "mimo-v2.5",
            "content": "response body must not be stored",
            "tool_calls": [],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "finish_reason": "stop",
                "completion_tokens_details": {"body": "usage-nested-secret"},
                "unknown_body": "usage-top-level-secret",
                "total_tokens": 10**20,
                "prompt_tokens": "12",
            },
        },
        error={
            "code": "PROVIDER_HTTP_ERROR",
            "message": "authorization=Bearer upstream-secret",
            "response": {"body": "error-nested-secret"},
        },
        http_status=502,
        stream=False,
    )

    assert entry["usage"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "finish_reason": "stop",
    }
    assert entry["finishReason"] == "stop"
    assert entry["errorCode"] == "PROVIDER_HTTP_ERROR"
    assert entry["errorMessage"].startswith("authorization=[REDACTED]")
    assert "upstream-secret" not in entry["errorMessage"]
    untrusted_error = store.record(
        request_id="req_untrusted_error",
        request=request_model(2),
        profile=provider_profile(),
        started=time.monotonic(),
        status="error",
        error={
            "code": "secret-token-value",
            "message": {"body": "nested-error-message-secret"},
        },
        http_status=502,
        stream=False,
    )
    assert untrusted_error["errorCode"] == ""
    assert untrusted_error["errorMessage"] == ""
    persisted = path.read_text(encoding="utf-8")
    assert "response body must not be stored" not in persisted
    assert "usage-nested-secret" not in persisted
    assert "usage-top-level-secret" not in persisted
    assert "error-nested-secret" not in persisted
    assert "nested-error-message-secret" not in persisted
    assert "secret-token-value" not in persisted
    assert "upstream-secret" not in persisted


def test_request_log_rotates_and_lists_newest_without_path_read_text(tmp_path, monkeypatch):
    path = tmp_path / "request-logs.jsonl"
    store = RequestLogStore(path, max_bytes=1, backup_count=2)
    profile = provider_profile()
    for index in range(1, 5):
        store.record(
            request_id=f"req_{index}",
            request=request_model(index),
            profile=profile,
            started=time.monotonic(),
            status="success",
            response={"model": "mimo-v2.5", "usage": {"input_tokens": index}},
            stream=False,
        )

    assert path.is_file()
    assert path.with_name("request-logs.jsonl.1").is_file()
    assert path.with_name("request-logs.jsonl.2").is_file()

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("request-log listing must not load a whole file with read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    items, total = store.list(limit=2, offset=1)

    assert total == 3
    assert [item["requestId"] for item in items] == ["req_3", "req_2"]
    assert store.get("req_4")["requestId"] == "req_4"
    assert store.get("req_1") is None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
    ],
)
def test_request_log_admin_requires_valid_token(tmp_path, headers):
    response = make_client(tmp_path).get(
        "/api/actiondesign-agent/admin/request-logs",
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "PROFILE_ADMIN_AUTH_REQUIRED"


def test_request_log_admin_is_unavailable_when_token_is_disabled(tmp_path):
    response = make_client(tmp_path, profile_admin_token="").get(
        "/api/actiondesign-agent/admin/request-logs",
        headers={"Authorization": "Bearer ignored"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PROFILE_ADMIN_DISABLED"


def test_request_log_admin_returns_sanitized_detail_and_not_found(tmp_path, monkeypatch):
    async def fake_call(_req, _profile):
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "private response",
            "tool_calls": [],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 8,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "stop_reason": "end_turn",
                "provider_details": {"body": "nested-provider-body"},
            },
        }

    monkeypatch.setattr(gateway_app, "call_mimo", fake_call)
    client = make_client(tmp_path)
    chat = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json=chat_payload(),
    )
    request_id = chat.headers["x-request-id"]
    headers = {"Authorization": "Bearer admin-token"}

    detail = client.get(
        f"/api/actiondesign-agent/admin/request-logs/{request_id}",
        headers=headers,
    )
    missing = client.get(
        "/api/actiondesign-agent/admin/request-logs/req_missing",
        headers=headers,
    )

    assert chat.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["requestId"] == request_id
    assert detail.json()["usage"] == {
        "input_tokens": 10,
        "output_tokens": 4,
        "stop_reason": "end_turn",
    }
    assert "Private business prompt" not in detail.text
    assert "private response" not in detail.text
    assert "nested-provider-body" not in detail.text
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "REQUEST_LOG_NOT_FOUND"


def test_request_log_settings_have_bounded_rotation_defaults(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.request_log_max_bytes == 10 * 1024 * 1024
    assert settings.request_log_backup_count == 5
