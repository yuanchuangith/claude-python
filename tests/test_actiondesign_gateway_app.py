import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from claude_agent_sdk.actiondesign_gateway import app as gateway_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.models import (  # noqa: E402
    GatewayEventEnvelope,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.session_log import (  # noqa: E402
    append_conversation_event,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_CONVERSATION_ID = "22222222-2222-4222-8222-222222222222"
SERVICE_AUTH_CONVERSATION_ID = "33333333-3333-4333-8333-333333333333"
SEAL_CONVERSATION_ID = "44444444-4444-4444-8444-444444444444"
FORWARDED_CONVERSATION_ID = "55555555-5555-4555-8555-555555555555"
RUN_ID = "run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_RUN_ID = "run_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SERVICE_AUTH_RUN_ID = "run_cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SEAL_RUN_ID = "run_dddddddd-dddd-4ddd-8ddd-dddddddddddd"
FORWARDED_RUN_ID = "run_eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def make_settings(tmp_path, **overrides):
    values = {
        "default_provider": "mimo",
        "allow_origins": ["*"],
        "log_root": tmp_path / "audit",
        "full_conversation_log_enabled": True,
        "full_conversation_log_root": tmp_path / "conversations",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "mimo_api_key": "mimo-secret",
        "mimo_messages_url": "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages",
        "mimo_default_model": "mimo-v2.5",
        "claude_code_api_key": "claude-secret",
        "claude_code_messages_url": "https://api.anthropic.com/v1/messages",
        "claude_code_default_model": "claude-sonnet-4-5",
        "claude_code_models": ["claude-sonnet-4-5"],
        "profile_store_path": tmp_path / "profiles.json",
        "profile_admin_token": "admin-token",
        "admin_allow_remote": True,
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


def conversation_events(root, conversation_id):
    matches = list(root.glob(f"????????_??????_{conversation_id}.jsonl"))
    assert len(matches) == 1
    return [json.loads(line) for line in matches[0].read_text(encoding="utf-8").splitlines()]


def audit_events(root, audit_id, filename):
    path = root / audit_id / filename
    assert path.is_file()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def chat_payload(**overrides):
    payload = {
        "provider": "mimo",
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "agentMode": "normal",
        "systemPrompt": "You are the ActionDesign system agent.",
        "prompt": "Create a node",
        "stream": False,
        "protocolVersion": "1.0",
        "graphVersion": "1.1.0",
        "promptManifestVersion": "2.0.0",
        "promptManifestHash": "sha256:" + ("a" * 64),
        "toolCatalogVersion": "1.1.0",
        "toolCatalogHash": "sha256:" + ("b" * 64),
        "tools": [
            {
                "name": "create_node",
                "description": "Create one canvas node",
                "input_schema": {
                    "type": "object",
                    "properties": {"elementKey": {"type": "string"}},
                    "required": ["elementKey"],
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


def identity_headers(payload, **extra):
    return {
        "X-Conversation-Id": payload["conversationId"],
        "X-Agent-Run-Id": payload["runId"],
        **extra,
    }


def test_models_expose_only_native_tool_capabilities(tmp_path):
    response = make_client(tmp_path).get("/api/actiondesign-agent/models")

    assert response.status_code == 200
    body = response.json()
    assert body["supportedProtocolVersions"] == ["1.0"]
    assert body["providers"]["mimo"]["status"] == "ready"
    assert all(
        model["supportsNativeTools"] is True
        for model in body["providers"]["mimo"]["models"]
    )


def test_health_identifies_thin_gateway(tmp_path):
    response = make_client(tmp_path).get("/api/actiondesign-agent/health")

    assert response.status_code == 200
    assert response.json()["runtime"] == "thin-gateway"
    assert response.json()["protocolVersion"] == "1.0"
    assert response.json()["fullConversationLogEnabled"] is True

    disabled = make_client(
        tmp_path / "disabled-log",
        full_conversation_log_enabled=False,
    ).get("/api/actiondesign-agent/health")
    assert disabled.status_code == 200
    assert disabled.json()["fullConversationLogEnabled"] is False


def test_service_auth_is_explicit_and_protects_sensitive_transport_routes(tmp_path):
    service_token = "service-token-" + ("x" * 32)
    client = make_client(
        tmp_path,
        service_auth_mode="required",
        service_auth_token=service_token,
    )
    payload = {
        "conversationId": SERVICE_AUTH_CONVERSATION_ID,
        "runId": SERVICE_AUTH_RUN_ID,
        "toolCallId": "call_service_auth",
        "toolName": "create_node",
        "arguments": {"elementKey": "ExitAction"},
        "status": "success",
        "result": {"nodeKey": "node_service_auth"},
    }

    assert client.get("/api/actiondesign-agent/health").status_code == 200
    assert client.get("/api/actiondesign-agent/models").status_code == 200

    missing = client.post("/api/actiondesign-agent/tool-result", json=payload)
    wrong = client.post(
        "/api/actiondesign-agent/tool-result",
        headers={"X-ActionDesign-Service-Token": "wrong-token"},
        json=payload,
    )
    accepted = client.post(
        "/api/actiondesign-agent/tool-result",
        headers=identity_headers(
            payload,
            **{"X-ActionDesign-Service-Token": service_token},
        ),
        json=payload,
    )

    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "GATEWAY_SERVICE_AUTH_REQUIRED"
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    events = conversation_events(
        tmp_path / "conversations",
        SERVICE_AUTH_CONVERSATION_ID,
    )
    assert len(events) == 1
    assert service_token not in json.dumps(events, ensure_ascii=False)

    admin_only = client.get(
        "/api/actiondesign-agent/admin/profiles",
        headers={"X-ActionDesign-Service-Token": service_token},
    )
    assert admin_only.status_code == 401
    assert admin_only.json()["detail"]["code"] == "PROFILE_ADMIN_AUTH_REQUIRED"

    preflight = client.options(
        "/api/actiondesign-agent/chat",
        headers={
            "Origin": "https://actiondesign.example.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200


def test_service_auth_openapi_covers_only_sensitive_transport_routes(tmp_path):
    schema = make_client(tmp_path).get("/openapi.json").json()
    assert schema["info"]["version"] == "2.2.0"
    protected = {
        ("/api/actiondesign-agent/chat", "post"),
        ("/api/actiondesign-agent/chat/stream", "post"),
        ("/api/actiondesign-agent/mimo/chat", "post"),
        ("/api/actiondesign-agent/mimo/chat/stream", "post"),
        ("/api/actiondesign-agent/claude-code/chat", "post"),
        ("/api/actiondesign-agent/claude-code/chat/stream", "post"),
        ("/api/actiondesign-agent/tool-result", "post"),
        ("/api/actiondesign-agent/conversations/{conversation_id}/seal", "post"),
        ("/api/actiondesign-agent/mcp/gxp-lowcode-readonly/tools", "get"),
        ("/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call", "post"),
        ("/api/actiondesign-agent/telemetry/langsmith/runs/batch", "post"),
    }
    for path, method in protected:
        parameters = schema["paths"][path][method].get("parameters", [])
        assert any(
            item.get("in") == "header"
            and item.get("name") == "X-ActionDesign-Service-Token"
            for item in parameters
        ), (path, method, parameters)

    for path, method in {
        ("/api/actiondesign-agent/health", "get"),
        ("/api/actiondesign-agent/models", "get"),
        ("/api/actiondesign-agent/admin/profiles", "get"),
    }:
        parameters = schema["paths"][path][method].get("parameters", [])
        assert all(
            item.get("name") != "X-ActionDesign-Service-Token"
            for item in parameters
        )


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/actiondesign-agent/chat", chat_payload()),
        ("/api/actiondesign-agent/chat/stream", chat_payload(stream=True)),
        (
            "/api/actiondesign-agent/tool-result",
            {
                "conversationId": CONVERSATION_ID,
                "runId": RUN_ID,
                "toolCallId": "call_identity",
                "toolName": "create_node",
                "arguments": {"elementKey": "ExitAction"},
                "status": "success",
                "result": {"nodeKey": "node_identity"},
            },
        ),
    ],
)
def test_request_identity_headers_are_required_and_must_match_body(tmp_path, path, payload):
    client = make_client(tmp_path)

    missing = client.post(path, json=payload)
    legacy_run_header = client.post(
        path,
        headers={
            "X-Conversation-Id": payload["conversationId"],
            "X-Run-Id": payload["runId"],
        },
        json=payload,
    )
    mismatched_conversation = client.post(
        path,
        headers=identity_headers(
            payload,
            **{"X-Conversation-Id": OTHER_CONVERSATION_ID},
        ),
        json=payload,
    )
    mismatched_run = client.post(
        path,
        headers=identity_headers(payload, **{"X-Agent-Run-Id": OTHER_RUN_ID}),
        json=payload,
    )

    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "REQUEST_IDENTITY_REQUIRED"
    assert legacy_run_header.status_code == 422
    assert legacy_run_header.json()["detail"] == {
        "code": "REQUEST_IDENTITY_REQUIRED",
        "fields": ["X-Agent-Run-Id"],
    }
    assert mismatched_conversation.status_code == 409
    assert mismatched_conversation.json()["detail"] == {
        "code": "REQUEST_IDENTITY_MISMATCH",
        "fields": ["conversationId"],
    }
    assert mismatched_run.status_code == 409
    assert mismatched_run.json()["detail"] == {
        "code": "REQUEST_IDENTITY_MISMATCH",
        "fields": ["runId"],
    }


def test_request_identity_body_fields_are_required_and_conversation_id_is_uuid4(tmp_path):
    client = make_client(tmp_path)
    missing_body = chat_payload()
    del missing_body["conversationId"]
    invalid_body = chat_payload(conversationId="conv:lossy")

    missing = client.post(
        "/api/actiondesign-agent/chat",
        headers={
            "X-Conversation-Id": CONVERSATION_ID,
            "X-Agent-Run-Id": RUN_ID,
        },
        json=missing_body,
    )
    invalid = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(invalid_body),
        json=invalid_body,
    )

    assert missing.status_code == 422
    assert "conversationId" in missing.text
    assert invalid.status_code == 422
    assert "conversationId" in invalid.text


@pytest.mark.parametrize(
    "invalid_conversation_id",
    [
        "conv_test",
        "11111111-1111-1111-8111-111111111111",
        "11111111111141118111111111111111",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_request_identity_rejects_noncanonical_or_non_v4_conversation_ids(
    tmp_path,
    invalid_conversation_id,
):
    client = make_client(tmp_path)
    body_invalid = chat_payload(conversationId=invalid_conversation_id)
    invalid_body = client.post(
        "/api/actiondesign-agent/chat",
        headers={
            "X-Conversation-Id": invalid_conversation_id,
            "X-Agent-Run-Id": body_invalid["runId"],
        },
        json=body_invalid,
    )
    valid_body = chat_payload()
    invalid_header = client.post(
        "/api/actiondesign-agent/chat",
        headers={
            "X-Conversation-Id": invalid_conversation_id,
            "X-Agent-Run-Id": valid_body["runId"],
        },
        json=valid_body,
    )

    assert invalid_body.status_code == 422
    assert invalid_header.status_code == 422
    assert invalid_header.json()["detail"]["code"] == "REQUEST_IDENTITY_INVALID"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/actiondesign-agent/chat", chat_payload()),
        ("/api/actiondesign-agent/chat/stream", chat_payload(stream=True)),
        (
            "/api/actiondesign-agent/tool-result",
            {
                "conversationId": CONVERSATION_ID,
                "runId": RUN_ID,
                "toolCallId": "call_run_identity",
                "toolName": "create_node",
            },
        ),
    ],
)
def test_runtime_routes_require_canonical_run_uuid4(tmp_path, path, payload):
    client = make_client(tmp_path)
    invalid_payload = {**payload, "runId": "run_test"}
    invalid_body = client.post(
        path,
        headers=identity_headers(invalid_payload),
        json=invalid_payload,
    )
    invalid_header = client.post(
        path,
        headers=identity_headers(payload, **{"X-Agent-Run-Id": "run_test"}),
        json=payload,
    )

    assert invalid_body.status_code == 422
    assert invalid_header.status_code == 422
    assert invalid_header.json()["detail"]["code"] == "REQUEST_IDENTITY_INVALID"


def test_sse_envelope_uses_runtime_identity_contract():
    envelope = {
        "protocolVersion": "1.0",
        "sequence": 1,
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "traceId": "trace_test",
        "timestamp": "2026-07-28T00:00:00Z",
        "type": "model.delta",
        "payload": {"content": "ok"},
    }

    assert GatewayEventEnvelope.model_validate(envelope).run_id == RUN_ID
    with pytest.raises(ValidationError):
        GatewayEventEnvelope.model_validate(
            {**envelope, "conversationId": "legacy_conversation"}
        )
    with pytest.raises(ValidationError):
        GatewayEventEnvelope.model_validate({**envelope, "runId": "run_test"})


def test_service_auth_seal_is_explicit_and_turn_failure_does_not_seal(tmp_path, monkeypatch):
    service_token = "service-token-" + ("s" * 32)

    async def failing_call(_req, _profile):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PROVIDER_TIMEOUT",
                "message": "temporary upstream failure",
                "retryable": True,
                "failoverScope": "none",
            },
        )

    monkeypatch.setattr(gateway_app, "call_mimo", failing_call)
    client = make_client(
        tmp_path,
        service_auth_mode="required",
        service_auth_token=service_token,
    )
    payload = chat_payload(
        conversationId=SEAL_CONVERSATION_ID,
        runId=SEAL_RUN_ID,
    )
    service_headers = identity_headers(
        payload,
        **{"X-ActionDesign-Service-Token": service_token},
    )

    failed_turn = client.post(
        "/api/actiondesign-agent/mimo/chat",
        headers=service_headers,
        json=payload,
    )
    before_seal = conversation_events(
        tmp_path / "conversations",
        SEAL_CONVERSATION_ID,
    )
    denied = client.post(
        f"/api/actiondesign-agent/conversations/{SEAL_CONVERSATION_ID}/seal",
        json={"status": "completed"},
    )
    sealed = client.post(
        f"/api/actiondesign-agent/conversations/{SEAL_CONVERSATION_ID}/seal",
        headers={"X-ActionDesign-Service-Token": service_token},
        json={"status": "completed"},
    )

    assert failed_turn.status_code == 502
    assert all(event["type"] != "conversation_sealed" for event in before_seal)
    assert denied.status_code == 401
    assert sealed.status_code == 200
    assert sealed.headers["x-request-id"].startswith("req_")
    assert sealed.json() == {"success": True, "status": "completed"}
    assert conversation_events(
        tmp_path / "conversations",
        SEAL_CONVERSATION_ID,
    )[-1]["type"] == "conversation_sealed"

    invalid_version = client.post(
        "/api/actiondesign-agent/conversations/"
        "11111111-1111-1111-8111-111111111111/seal",
        headers={"X-ActionDesign-Service-Token": service_token},
        json={"status": "completed"},
    )
    assert invalid_version.status_code == 422


def test_admin_conversation_routes_keep_legacy_safe_id_compatibility(tmp_path):
    settings = make_settings(tmp_path)
    append_conversation_event(
        settings,
        "legacy_conversation",
        {"type": "gateway_request", "runId": "legacy_run"},
    )
    client = TestClient(
        create_app(
            settings,
            profile_secret_store=MemoryProfileSecretStore(),
        )
    )
    headers = {"Authorization": "Bearer admin-token"}

    detail = client.get(
        "/api/actiondesign-agent/admin/conversations/legacy_conversation",
        headers=headers,
    )
    sealed = client.post(
        "/api/actiondesign-agent/admin/conversations/legacy_conversation/seal",
        headers=headers,
        json={"status": "completed"},
    )

    assert detail.status_code == 200
    assert detail.json()["conversationId"] == "legacy_conversation"
    assert sealed.status_code == 200
    assert sealed.json() == {"success": True, "status": "completed"}


def test_service_auth_required_rejects_missing_or_ambiguous_configuration(tmp_path):
    with pytest.raises(ValueError, match="at least 32 characters"):
        make_settings(
            tmp_path / "missing",
            service_auth_mode="required",
            service_auth_token="",
        )
    with pytest.raises(ValueError, match="must be empty"):
        make_settings(
            tmp_path / "ambiguous",
            service_auth_mode="disabled",
            service_auth_token="configured-but-disabled-secret",
        )


def test_chat_forwards_native_tool_schema_and_records_details(tmp_path, monkeypatch):
    captured = {}

    async def fake_call(req, profile):
        captured["request"] = req
        captured["profile"] = profile
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "name": "create_node", "arguments": {"elementKey": "ExitAction"}}
            ],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 8,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    monkeypatch.setattr(gateway_app, "call_mimo", fake_call)
    payload = chat_payload()
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/mimo/chat",
        headers=identity_headers(payload),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["tool_calls"][0]["name"] == "create_node"
    assert captured["request"].tools[0].input_schema["required"] == ["elementKey"]
    assert captured["profile"].id == "default-mimo"
    events = conversation_events(tmp_path / "conversations", CONVERSATION_ID)
    assert [event["type"] for event in events] == ["gateway_request", "gateway_response"]
    assert events[0]["request"]["graphVersion"] == "1.1.0"
    assert events[0]["request"]["prompt"] == "Create a node"
    assert events[0]["request"]["systemPrompt"] == "You are the ActionDesign system agent."
    assert events[1]["response"]["content"] == ""
    assert "apiKey" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"prompt": "   "}, "prompt"),
        ({"systemPrompt": "   "}, "systemPrompt"),
        ({"runId": ""}, "runId"),
        ({"maxTokens": 10}, "maxTokens"),
        ({"promptManifestHash": "not-a-hash"}, "promptManifestHash"),
        ({"tools": [{"name": "bad tool", "description": "x", "input_schema": {"type": "object", "properties": {}}}]}, "name"),
        ({"thinking": {"type": "enabled"}}, "budget_tokens"),
        ({"images": [{"media_type": "image/png", "data": "not-base64"}]}, "images"),
        ({"tools": [{"name": "bad_schema", "description": "x", "input_schema": {"type": "array"}}]}, "input_schema"),
    ],
)
def test_chat_rejects_invalid_request_shapes(tmp_path, overrides, field_name):
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/mimo/chat",
        json=chat_payload(**overrides),
    )

    assert response.status_code == 422
    assert field_name in response.text


def test_chat_rejects_tool_choice_not_present_in_tools(tmp_path):
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/mimo/chat",
        json=chat_payload(toolChoice={"type": "tool", "name": "review_result"}),
    )

    assert response.status_code == 422


def test_stream_uses_versioned_monotonic_sse_envelopes(tmp_path, monkeypatch):
    async def fake_stream(_req, _profile):
        yield {"kind": "delta", "content": "hello"}
        yield {
            "kind": "completed",
            "payload": {
                "provider": "mimo",
                "model": "mimo-v2.5",
                "content": "hello",
                "tool_calls": [],
                "success": True,
                "error": None,
                "code": None,
                "duration_ms": 5,
                "usage": {},
            },
        }

    monkeypatch.setattr(gateway_app, "stream_mimo", fake_stream)
    payload = chat_payload(stream=True, traceId="trace_test")
    with make_client(tmp_path).stream(
        "POST",
        "/api/actiondesign-agent/mimo/chat/stream",
        headers=identity_headers(payload),
        json=payload,
    ) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]

    assert response.status_code == 200
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["type"] for event in events] == ["model.delta", "model.completed"]
    assert all(event["protocolVersion"] == "1.0" for event in events)
    assert all(event["traceId"] == "trace_test" for event in events)


def test_stream_rejects_invalid_trace_header_before_opening_sse(tmp_path):
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/mimo/chat/stream",
        headers=identity_headers(
            chat_payload(stream=True),
            **{"X-Agent-Trace-Id": "invalid trace id"},
        ),
        json=chat_payload(stream=True),
    )

    assert response.status_code == 422
    assert "trace_id" in response.text


def test_stream_error_envelope_and_log_redact_secret_text(tmp_path, monkeypatch):
    async def fake_stream(_req, _profile):
        if False:
            yield {}
        raise RuntimeError("authorization=Bearer upstream-secret")

    monkeypatch.setattr(gateway_app, "stream_mimo", fake_stream)
    payload = chat_payload(stream=True, traceId="trace_secret")
    with make_client(tmp_path).stream(
        "POST",
        "/api/actiondesign-agent/mimo/chat/stream",
        headers=identity_headers(payload),
        json=payload,
    ) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0]["type"] == "gateway.error"
    serialized = json.dumps(events, ensure_ascii=False)
    assert "upstream-secret" not in serialized
    assert "[REDACTED]" in serialized
    persisted = json.dumps(
        conversation_events(tmp_path / "conversations", CONVERSATION_ID),
        ensure_ascii=False,
    )
    assert "upstream-secret" not in persisted


def test_tool_result_logging_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    payload = {
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "toolCallId": "call_1",
        "toolName": "create_node",
        "arguments": {
            "elementKey": "ExitAction",
            "apiKey": "tool-argument-secret",
        },
        "status": "success",
        "result": {
            "nodeKey": "node_1",
            "authorization": "Bearer tool-result-secret",
        },
    }

    first = client.post(
        "/api/actiondesign-agent/tool-result",
        headers=identity_headers(payload),
        json=payload,
    )
    second = client.post(
        "/api/actiondesign-agent/tool-result",
        headers=identity_headers(payload),
        json=payload,
    )

    assert first.status_code == 200
    assert first.headers["x-request-id"].startswith("req_")
    assert second.headers["x-request-id"].startswith("req_")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    events = conversation_events(tmp_path / "conversations", CONVERSATION_ID)
    assert [event["duplicate"] for event in events] == [False, True]
    assert [event["requestId"] for event in events] == [
        first.headers["x-request-id"],
        second.headers["x-request-id"],
    ]
    assert all(event["arguments"]["apiKey"] == "[REDACTED]" for event in events)
    assert all(
        event["result"]["authorization"] == "[REDACTED]" for event in events
    )
    persisted = json.dumps(events, ensure_ascii=False)
    assert "tool-argument-secret" not in persisted
    assert "tool-result-secret" not in persisted
    canonical = audit_events(
        tmp_path / "audit",
        CONVERSATION_ID,
        "tool-results.jsonl",
    )
    assert len(canonical) == 1
    assert canonical[0]["requestId"] == first.headers["x-request-id"]


def test_profile_admin_path_id_is_strictly_validated(tmp_path):
    client = make_client(tmp_path)
    response = client.delete(
        "/api/actiondesign-agent/admin/profiles/invalid.profile",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 422
    assert "profile_id" in response.text

    invalid_body = client.put(
        "/api/actiondesign-agent/admin/profiles/valid-profile",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "id": "invalid.profile",
            "provider": "mimo",
            "baseUrl": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "apiKey": "managed-secret",
            "defaultModel": "mimo-v2.5",
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )
    assert invalid_body.status_code == 422
    assert "id" in invalid_body.text


def test_profile_admin_requires_configured_bearer_token(tmp_path):
    client = make_client(tmp_path)

    missing = client.get("/api/actiondesign-agent/admin/profiles")
    wrong = client.get(
        "/api/actiondesign-agent/admin/profiles",
        headers={"Authorization": "Bearer wrong-token"},
    )
    disabled = make_client(
        tmp_path / "disabled",
        profile_admin_token="",
    ).get("/api/actiondesign-agent/admin/profiles")

    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "PROFILE_ADMIN_AUTH_REQUIRED"
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "PROFILE_ADMIN_AUTH_REQUIRED"
    assert disabled.status_code == 401
    assert disabled.json()["detail"]["code"] == "PROFILE_ADMIN_AUTH_REQUIRED"


def test_tool_result_rejects_coerced_and_unbounded_json(tmp_path):
    client = make_client(tmp_path)
    base = {
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "toolCallId": "call_1",
        "toolName": "create_node",
        "arguments": {"elementKey": "ExitAction"},
        "status": "success",
        "result": {"nodeKey": "node_1"},
    }

    coerced = client.post(
        "/api/actiondesign-agent/tool-result",
        json={**base, "turn": "1"},
    )
    oversized = client.post(
        "/api/actiondesign-agent/tool-result",
        json={**base, "result": {"content": "x" * 2_000_001}},
    )

    assert coerced.status_code == 422
    assert oversized.status_code == 422


def test_profile_admin_never_echoes_secret(tmp_path):
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer admin-token"}
    response = client.put(
        "/api/actiondesign-agent/admin/profiles/team-mimo",
        headers=headers,
        json={
            "id": "team-mimo",
            "provider": "mimo",
            "baseUrl": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "apiKey": "managed-secret",
            "defaultModel": "mimo-v2.5",
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["keyConfigured"] is True
    assert "managed-secret" not in response.text
    persisted_text = (tmp_path / "profiles.json").read_text(encoding="utf-8")
    assert "managed-secret" not in persisted_text
    assert "api_key" not in persisted_text
    assert json.loads(persisted_text)["schemaVersion"] == "4"
    listed = client.get("/api/actiondesign-agent/admin/profiles", headers=headers)
    assert listed.status_code == 200
    assert "managed-secret" not in listed.text

    deleted = client.delete(
        "/api/actiondesign-agent/admin/profiles/team-mimo",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}

    events = [
        json.loads(line)
        for line in (tmp_path / "audit" / "system" / "admin-audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["action"] for event in events] == ["profile_upsert", "profile_delete"]
    assert events[0]["details"]["profileId"] == "team-mimo"
    assert events[0]["details"]["keyUpdated"] is True
    assert events[1]["details"]["profileId"] == "team-mimo"
    assert events[1]["success"] is True
    audit_text = json.dumps(events, ensure_ascii=False)
    assert "managed-secret" not in audit_text


def test_dynamic_openai_channel_routes_through_generic_endpoint_and_logs_metadata(
    tmp_path,
    monkeypatch,
):
    captured = {}

    async def fake_call(req, profile):
        captured["request"] = req
        captured["profile"] = profile
        return {
            "provider": "team-openai",
            "model": "gpt-5.1",
            "content": "done",
            "tool_calls": [],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 12,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "finish_reason": "stop",
            },
        }

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer admin-token"}
    created = client.put(
        "/api/actiondesign-agent/admin/profiles/team-openai",
        headers=headers,
        json={
            "id": "team-openai",
            "provider": "team-openai",
            "displayName": "团队 OpenAI",
            "protocol": "openai",
            "baseUrl": "https://api.anthropic.com/openai/v1",
            "apiKey": "managed-secret",
            "defaultModel": "gpt-5.1",
            "models": [
                {
                    "id": "gpt-5.1",
                    "name": "GPT 5.1",
                    "supportsImages": True,
                    "contextWindowTokens": 200000,
                    "maxOutputTokens": 8192,
                    "supportsNativeTools": True,
                    "supportsThinking": True,
                }
            ],
            "authMode": "bearer",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )

    payload = chat_payload(
        provider="team-openai",
        model="gpt-5.1",
        prompt="sensitive business prompt",
    )
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(payload),
        json=payload,
    )
    logs = client.get(
        "/api/actiondesign-agent/admin/request-logs?provider=team-openai",
        headers=headers,
    )
    models = client.get("/api/actiondesign-agent/models")

    assert created.status_code == 200
    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req_")
    assert captured["profile"].id == "team-openai"
    assert captured["profile"].protocol == "openai"
    assert logs.status_code == 200
    assert logs.json()["total"] == 1
    entry = logs.json()["items"][0]
    assert entry["protocol"] == "openai"
    assert entry["promptCharacters"] == len("sensitive business prompt")
    assert entry["finishReason"] == "stop"
    assert "sensitive business prompt" not in (tmp_path / "request-logs.jsonl").read_text(encoding="utf-8")
    assert "managed-secret" not in (tmp_path / "request-logs.jsonl").read_text(encoding="utf-8")
    assert models.json()["providers"]["team-openai"]["protocol"] == "openai"
    assert models.json()["providers"]["team-openai"]["defaultProfileId"] == "team-openai"


def test_dynamic_managed_claude_channel_routes_through_generic_chat(
    tmp_path,
    monkeypatch,
):
    captured = {}

    async def fake_call(req, profile):
        captured["request"] = req
        captured["profile"] = profile
        return {
            "provider": "team-claude",
            "model": "claude-team-model",
            "content": "done",
            "tool_calls": [],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 10,
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer admin-token"}
    created = client.put(
        "/api/actiondesign-agent/admin/profiles/team-claude",
        headers=headers,
        json={
            "id": "team-claude",
            "provider": "team-claude",
            "displayName": "Team Claude",
            "protocol": "claude",
            "baseUrl": "https://api.anthropic.com/v1/messages",
            "apiKey": "managed-secret",
            "defaultModel": "claude-team-model",
            "models": [
                {
                    "id": "claude-team-model",
                    "name": "Claude Team",
                    "supportsImages": True,
                    "contextWindowTokens": 200000,
                    "maxOutputTokens": 8192,
                    "supportsNativeTools": True,
                    "supportsThinking": True,
                }
            ],
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )
    payload = chat_payload(
        provider="team-claude",
        profileId="team-claude",
        model="claude-team-model",
        thinking={"type": "enabled", "budget_tokens": 4096},
    )
    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(payload),
        json=payload,
    )

    assert created.status_code == 200
    assert response.status_code == 200
    assert captured["profile"].protocol == "claude"
    assert captured["request"].thinking.budget_tokens == 4096


@pytest.mark.parametrize(
    ("request_overrides", "expected_code"),
    [
        ({"model": "unknown-model"}, "PROFILE_MODEL_UNAVAILABLE"),
        (
            {"images": [{"media_type": "image/png", "data": "aGVsbG8="}]},
            "MODEL_DOES_NOT_SUPPORT_IMAGES",
        ),
        (
            {
                "tools": [
                    {
                        "name": "inspect_action",
                        "description": "Inspect",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ]
            },
            "MODEL_DOES_NOT_SUPPORT_NATIVE_TOOLS",
        ),
        ({"maxTokens": 2048}, "MODEL_MAX_OUTPUT_TOKENS_EXCEEDED"),
        (
            {"thinking": {"type": "enabled", "budget_tokens": 1024}},
            "MODEL_DOES_NOT_SUPPORT_THINKING",
        ),
    ],
)
def test_profile_model_capabilities_are_enforced(
    tmp_path,
    monkeypatch,
    request_overrides,
    expected_code,
):
    async def unexpected_call(_req, _profile):
        raise AssertionError("provider must not be called for invalid capabilities")

    monkeypatch.setattr(gateway_app, "call_provider", unexpected_call)
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer admin-token"}
    created = client.put(
        "/api/actiondesign-agent/admin/profiles/limited-channel",
        headers=headers,
        json={
            "id": "limited-channel",
            "provider": "limited-channel",
            "displayName": "Limited Channel",
            "protocol": "claude",
            "baseUrl": "https://api.anthropic.com/v1/messages",
            "apiKey": "managed-secret",
            "defaultModel": "limited-model",
            "models": [
                {
                    "id": "limited-model",
                    "name": "Limited Model",
                    "supportsImages": False,
                    "contextWindowTokens": 4096,
                    "maxOutputTokens": 1024,
                    "supportsNativeTools": False,
                    "supportsThinking": False,
                }
            ],
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )
    payload = chat_payload(
        provider="limited-channel",
        profileId="limited-channel",
        model="limited-model",
        maxTokens=1024,
        tools=[],
    )
    payload.update(request_overrides)

    response = client.post(
        "/api/actiondesign-agent/chat",
        headers=identity_headers(payload),
        json=payload,
    )

    assert created.status_code == 200
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.anthropic.com/v1/messages?api_key=not-allowed",
        "https://api.anthropic.com/v1/messages#not-allowed",
    ],
)
def test_profile_admin_rejects_url_query_parameters_and_fragments(tmp_path, base_url):
    response = make_client(tmp_path).put(
        "/api/actiondesign-agent/admin/profiles/unsafe-url",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "id": "unsafe-url",
            "provider": "unsafe-url",
            "protocol": "claude",
            "baseUrl": base_url,
            "apiKey": "managed-secret",
            "defaultModel": "safe-model",
            "models": [{"id": "safe-model"}],
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )

    assert response.status_code == 422


def test_profile_admin_validation_error_never_echoes_invalid_secret(tmp_path):
    invalid_secret = "short"
    response = make_client(tmp_path).put(
        "/api/actiondesign-agent/admin/profiles/team-mimo",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "id": "team-mimo",
            "provider": "mimo",
            "baseUrl": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "apiKey": invalid_secret,
            "defaultModel": "mimo-v2.5",
            "authMode": "api-key",
            "timeoutSeconds": 90,
            "enabled": True,
        },
    )

    assert response.status_code == 422
    assert "apiKey" in response.text
    assert invalid_secret not in response.text
    assert '"input"' not in response.text


def test_removed_agent_endpoints_are_not_registered(tmp_path):
    client = make_client(tmp_path)
    for path in [
        "/api/actiondesign-agent/mimo/review",
        "/api/actiondesign-agent/mimo/topic/classify",
        "/api/actiondesign-agent/knowledge/search",
        "/api/actiondesign-agent/knowledge/read",
        "/api/actiondesign-agent/knowledge/upload",
    ]:
        assert client.post(path, json={}).status_code == 404


def test_langsmith_disabled_does_not_block_agent_telemetry(tmp_path):
    trace_event = {
        "schemaVersion": "1.0",
        "eventId": "event_1",
        "traceId": "trace_1",
        "traceRunId": "trace_run_1",
        "agentRunId": RUN_ID,
        "name": "actiondesign-agent",
        "runType": "graph",
        "event": "start",
        "timestamp": "2026-07-25T00:00:00Z",
        "tags": ["actiondesign"],
        "metadata": {
            "environment": "test",
            "conversationId": CONVERSATION_ID,
            "graphVersion": "1.1.0",
            "protocolVersion": "1.0",
            "promptManifestVersion": "2.0.0",
            "promptManifestHash": "sha256:" + ("a" * 64),
            "toolCatalogVersion": "1.1.0",
            "toolCatalogHash": "sha256:" + ("b" * 64),
            "agentMode": "normal",
        },
    }
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/telemetry/langsmith/runs/batch",
        json={
            "schemaVersion": "1.0",
            "contentLevel": "sanitized",
            "sentAt": "2026-07-25T00:00:00Z",
            "events": [trace_event],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "disabled"
    assert body["rejected"] == 1
    events = audit_events(
        tmp_path / "audit",
        body["exportBatchId"],
        "langsmith-trace-batches.jsonl",
    )
    assert len(events) == 2
    assert events[0]["schemaVersion"] == "1.0"
    assert events[0]["events"][0]["eventId"] == "event_1"
    assert events[1] == {
        "type": "langsmith_trace_batch_result",
        **body,
    }


def test_langsmith_trace_rejects_unbounded_inputs(tmp_path):
    trace_event = {
        "schemaVersion": "1.0",
        "eventId": "event_1",
        "traceId": "trace_1",
        "traceRunId": "trace_run_1",
        "agentRunId": RUN_ID,
        "name": "actiondesign-agent",
        "runType": "graph",
        "event": "start",
        "timestamp": "2026-07-25T00:00:00Z",
        "inputs": {"content": "x" * 500_001},
        "metadata": {
            "environment": "test",
            "conversationId": CONVERSATION_ID,
            "graphVersion": "1.1.0",
            "protocolVersion": "1.0",
            "promptManifestVersion": "2.0.0",
            "promptManifestHash": "sha256:" + ("a" * 64),
            "toolCatalogVersion": "1.1.0",
            "toolCatalogHash": "sha256:" + ("b" * 64),
            "agentMode": "normal",
        },
    }

    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/telemetry/langsmith/runs/batch",
        json={
            "schemaVersion": "1.0",
            "contentLevel": "sanitized",
            "sentAt": "2026-07-25T00:00:00Z",
            "events": [trace_event],
        },
    )

    assert response.status_code == 422

    invalid_version = make_client(tmp_path).post(
        "/api/actiondesign-agent/telemetry/langsmith/runs/batch",
        json={
            "schemaVersion": "9.9",
            "contentLevel": "sanitized",
            "sentAt": "2026-07-25T00:00:00Z",
            "events": [trace_event],
        },
    )
    assert invalid_version.status_code == 422


@pytest.mark.parametrize(
    ("forwarded", "expected_status"),
    [((1, 0), "accepted"), ((0, 1), "partial")],
)
def test_langsmith_forward_result_is_written_to_audit_log(
    tmp_path,
    monkeypatch,
    forwarded,
    expected_status,
):
    async def fake_forward_trace_batch(_settings, _events):
        return forwarded

    monkeypatch.setattr(gateway_app, "forward_trace_batch", fake_forward_trace_batch)
    response = make_client(
        tmp_path,
        langsmith_enabled=True,
        langsmith_api_key="langsmith-secret",
    ).post(
        "/api/actiondesign-agent/telemetry/langsmith/runs/batch",
        json={
            "schemaVersion": "1.0",
            "contentLevel": "sanitized",
            "sentAt": "2026-07-25T00:00:00Z",
            "events": [
                {
                    "schemaVersion": "1.0",
                    "eventId": "event_forwarded",
                    "traceId": "trace_forwarded",
                    "traceRunId": "trace_forwarded",
                    "agentRunId": FORWARDED_RUN_ID,
                    "name": "actiondesign-agent",
                    "runType": "graph",
                    "event": "start",
                    "timestamp": "2026-07-25T00:00:00Z",
                    "tags": ["actiondesign"],
                    "metadata": {
                        "environment": "test",
                        "conversationId": FORWARDED_CONVERSATION_ID,
                        "graphVersion": "1.9.0",
                        "protocolVersion": "1.0",
                        "promptManifestVersion": "2.2.0",
                        "promptManifestHash": "sha256:" + ("a" * 64),
                        "toolCatalogVersion": "1.5.0",
                        "toolCatalogHash": "sha256:" + ("b" * 64),
                        "agentMode": "normal",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "exportBatchId": body["exportBatchId"],
        "accepted": forwarded[0],
        "rejected": forwarded[1],
        "status": expected_status,
    }
    events = audit_events(
        tmp_path / "audit",
        body["exportBatchId"],
        "langsmith-trace-batches.jsonl",
    )
    assert events[-1] == {
        "type": "langsmith_trace_batch_result",
        **body,
    }


def test_langsmith_rejects_incomplete_trace_envelope(tmp_path):
    response = make_client(tmp_path).post(
        "/api/actiondesign-agent/telemetry/langsmith/runs/batch",
        json={
            "schemaVersion": "1.0",
            "contentLevel": "sanitized",
            "sentAt": "2026-07-25T00:00:00Z",
            "events": [{"eventId": "event_1"}],
        },
    )

    assert response.status_code == 422
