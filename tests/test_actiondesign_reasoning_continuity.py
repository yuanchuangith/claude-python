import json
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway import provider_http, provider_responses
from claude_agent_sdk.actiondesign_gateway.models import (
    AgentChatMessage,
    AgentChatRequest,
    ApiProfileWriteRequest,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import ProviderProfile
from claude_agent_sdk.actiondesign_gateway.reasoning import (
    REASONING_ADAPTER,
    reasoning_payload,
)
from claude_agent_sdk.actiondesign_gateway.redaction import (
    redact_secret_values,
    redact_value,
)
from claude_agent_sdk.actiondesign_gateway.session_log import append_conversation_event
from claude_agent_sdk.actiondesign_gateway.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


def profile(protocol="claude"):
    return ProviderProfile(
        id="team",
        provider="team",
        protocol=protocol,
        base_url="https://example.test/v1",
        api_key="DO_NOT_PERSIST_PROVIDER_KEY",
        default_model="model-alias",
        auth_mode="bearer",
        timeout_seconds=30,
        enabled=True,
        source="managed",
    )


def request(**overrides):
    return AgentChatRequest(
        **{
            "conversationId": "11111111-1111-4111-8111-111111111111",
            "runId": "run_22222222-2222-4222-8222-222222222222",
            "requestId": "33333333-3333-4333-8333-333333333333",
            "correlationId": "44444444-4444-4444-8444-444444444444",
            "provider": "team",
            "profileId": "team",
            "model": "model-alias",
            "systemPrompt": "Use native tools",
            "prompt": "Continue",
            "tools": [
                {
                    "name": "inspect_action",
                    "description": "Read",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            **overrides,
        }
    )


def history(state):
    return [
        {"role": "user", "content": "Inspect"},
        {
            "role": "assistant",
            "content": "",
            "reasoning": state,
            "toolCalls": [{"id": "call-1", "name": "inspect_action", "arguments": {}}],
        },
        {
            "role": "tool",
            "content": "result",
            "toolCallId": "call-1",
            "name": "inspect_action",
            "isError": False,
        },
    ]


def state(protocol="claude"):
    values = (
        {
            "blocks": [
                {
                    "type": "thinking",
                    "thinking": "  Inspect the real design.\n",
                    "signature": "ABCD" * 128,
                },
                {"type": "redacted_thinking", "data": "EFGH" * 128},
            ]
        }
        if protocol == "claude"
        else (
            {"reasoningContent": "  Check results before explaining.\n"}
            if protocol == "openai"
            else {
                "items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Check results"}],
                        "encrypted_content": "IJKL" * 128,
                    }
                ]
            }
        )
    )
    return reasoning_payload("team", "team", "model-alias", protocol, **values)


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
def test_native_state_exact_roundtrip_and_route_isolation(protocol):
    native = state(protocol)
    req, prof = request(messages=history(native)), profile(protocol)
    adapter = {
        "claude": provider_http.request_body,
        "openai": provider_http.openai_request_body,
        "openai-responses": provider_responses.responses_request_body,
    }[protocol]
    wire = adapter(req, prof, False)
    assert native == req.messages[1].reasoning.model_dump(
        by_alias=True, exclude_none=True
    )
    if protocol == "claude":
        assert wire["messages"][1]["content"][:2] == native["blocks"]
    elif protocol == "openai":
        assert (
            next(m for m in wire["messages"] if m["role"] == "assistant")[
                "reasoning_content"
            ]
            == native["reasoningContent"]
        )
    else:
        assert wire["input"][1] == native["items"][0]
        assert wire["input"][2]["call_id"] == "call-1"
        assert wire["input"][3]["call_id"] == "call-1"
        assert wire["store"] is False and wire["include"] == [
            "reasoning.encrypted_content"
        ]
    for changed in [replace(prof, id="other"), replace(prof, provider="other"), prof]:
        changed_req = (
            request(messages=history(native), model="other-model")
            if changed == prof
            else req
        )
        incompatible = adapter(changed_req, changed, False)
        assert "ABCD" not in json.dumps(incompatible)
        assert "reasoning_content" not in json.dumps(incompatible)
        assert "encrypted_content" not in json.dumps(incompatible.get("input", []))
        assert "call-1" in json.dumps(incompatible), (
            "ordinary call/result history survives model changes"
        )


def test_signed_and_encrypted_state_survives_log_redaction_but_secrets_do_not(tmp_path):
    for protocol in ["claude", "openai", "openai-responses"]:
        value = {"reasoning": state(protocol)}
        assert redact_value(value) == value
        assert redact_secret_values(value, [profile().api_key]) == value
        settings = Settings(full_conversation_log_root=tmp_path)
        append_conversation_event(
            settings,
            request().conversation_id,
            {"type": "gateway_response", "response": value},
        )
    text = "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    assert "ABCD" * 128 in text and "IJKL" * 128 in text
    contaminated = state()
    contaminated["blocks"][0]["thinking"] = "Authorization: Bearer stolen-token"
    assert redact_value({"reasoning": contaminated})["reasoning"] is None
    contaminated["blocks"][0]["thinking"] = profile().api_key
    assert (
        redact_secret_values({"reasoning": contaminated}, [profile().api_key])[
            "reasoning"
        ]
        is None
    )


def test_reasoning_strict_validation_and_role_boundary():
    value = state()
    with pytest.raises(ValidationError):
        REASONING_ADAPTER.validate_python({**value, "secret": "bad"})
    value["blocks"][0]["thinking"] = "测" * 180_000
    with pytest.raises(ValidationError, match="512 KiB"):
        REASONING_ADAPTER.validate_python(value)
    with pytest.raises(ValidationError, match="assistant"):
        AgentChatMessage(role="user", content="hello", reasoning=state())
    assert AgentChatMessage(role="assistant", content="legacy").reasoning is None


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
@pytest.mark.anyio
async def test_http_and_sse_capture_native_state_then_replay(monkeypatch, protocol):
    native = state(protocol)
    prof, req = profile(protocol), request()
    if protocol == "claude":
        payload = {
            "model": "provider-revision",
            "content": [
                *native["blocks"],
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "inspect_action",
                    "input": {},
                },
            ],
            "stop_reason": "tool_use",
        }
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": native["blocks"][0]["thinking"],
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "signature_delta",
                    "signature": native["blocks"][0]["signature"],
                },
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": native["blocks"][1],
            },
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": payload["content"][-1],
            },
            {"type": "message_stop"},
        ]
    elif protocol == "openai":
        payload = {
            "model": "provider-revision",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": native["reasoningContent"],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "inspect_action",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        events = [
            {
                "choices": [
                    {
                        "delta": payload["choices"][0]["message"],
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
    else:
        payload = {
            "model": "provider-revision",
            "status": "completed",
            "output": [
                native["items"][0],
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "inspect_action",
                    "arguments": "{}",
                },
            ],
        }
        events = [
            {"type": "response.output_item.done", "item": native["items"][0]},
            {"type": "response.completed", "response": payload},
        ]
    wire_requests = []

    async def handle(wire):
        wire_requests.append(json.loads(wire.content))
        if wire_requests[-1]["stream"]:
            return httpx.Response(
                200,
                text="".join("data: " + json.dumps(e) + "\n\n" for e in events),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handle)

    def client(_):
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(provider_http, "_provider_http_client", client)
    monkeypatch.setattr(provider_responses, "_provider_http_client", client)
    result = await provider_http.call_provider(req, prof)
    assert result["reasoning"] == native, (
        "route aliases bind state to requested model, not provider revision"
    )
    streamed = [event async for event in provider_http.stream_provider(req, prof)]
    assert streamed[-1]["payload"]["reasoning"] == native
    await provider_http.call_provider(
        request(messages=history(streamed[-1]["payload"]["reasoning"])), prof
    )
    assert (
        "signature" in json.dumps(wire_requests[-1])
        if protocol == "claude"
        else (
            "reasoning_content" in json.dumps(wire_requests[-1])
            if protocol == "openai"
            else "encrypted_content" in json.dumps(wire_requests[-1])
        )
    )


def test_responses_endpoint_normalization_and_profile_public_shape():
    prof = profile("openai-responses")
    assert prof.public()["providerPath"] == "/v1/responses"
    for base in [
        "https://example.test",
        "https://example.test/v1",
        "https://example.test/v1/responses",
    ]:
        assert (
            provider_responses.normalize_responses_url(base)
            == "https://example.test/v1/responses"
        )
        assert (
            provider_http.normalize_models_url(base, "openai-responses")
            == "https://example.test/v1/models"
        )
    # Profile management exposes the explicit protocol; existing profiles remain unchanged.
    ApiProfileWriteRequest(
        id="team",
        displayName="Responses",
        provider="team",
        protocol="openai-responses",
        baseUrl=prof.base_url,
        defaultModel=prof.default_model,
        models=[{"id": prof.default_model, "name": "Model"}],
        authMode="bearer",
    )


@pytest.mark.anyio
async def test_responses_probe_uses_responses_protocol(monkeypatch):
    captured = []

    async def handle(wire):
        captured.append((str(wire.url), json.loads(wire.content)))
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        provider_http,
        "_provider_http_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    from claude_agent_sdk.actiondesign_gateway.models import ApiProfileModel

    prof = replace(
        profile("openai-responses"),
        models=[ApiProfileModel(id="model-alias", name="Model")],
    )
    await provider_http.probe_provider(prof, "model-alias")
    assert captured[0][0] == "https://example.test/v1/responses"
    assert captured[0][1]["input"] == [{"role": "user", "content": "ping"}]
    assert captured[0][1]["store"] is False


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_responses_noncompleted_output_never_preserves_native_state(status):
    parsed = provider_responses.parse_responses_response(
        {"status": status, "output": [state("openai-responses")["items"][0]]},
        profile("openai-responses"),
        request(),
        0,
    )
    assert parsed["success"] is False
    assert parsed["reasoning"] is None


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "response.output_text.delta", "delta": "partial"}],
        [{"type": "response.failed"}],
    ],
)
@pytest.mark.anyio
async def test_responses_disconnected_or_failed_stream_never_completes(
    monkeypatch, events
):
    async def handle(wire):
        return httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )

    monkeypatch.setattr(
        provider_responses,
        "_provider_http_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    received = []
    with pytest.raises(HTTPException):
        async for event in provider_http.stream_provider(
            request(), profile("openai-responses")
        ):
            received.append(event)
    assert not any(e["kind"] == "completed" for e in received)


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
def test_gateway_http_sse_and_conversation_log_preserve_state(
    tmp_path, monkeypatch, protocol
):
    from fastapi.testclient import TestClient

    from claude_agent_sdk.actiondesign_gateway import app as gateway_app
    from claude_agent_sdk.actiondesign_gateway.profile_store import (
        MemoryProfileSecretStore,
    )

    settings = Settings(
        default_provider="team",
        service_auth_mode="disabled",
        profile_admin_token="admin-token",
        admin_allow_remote=True,
        admin_require_https_remote=False,
        profile_allowed_hosts=["example.test", "token-plan-cn.xiaomimimo.com", "api.anthropic.com"],
        profile_store_path=tmp_path / "profiles.json",
        managed_secret_store_path=tmp_path / "secrets.json",
        request_log_path=tmp_path / "requests.jsonl",
        log_root=tmp_path / "audit",
        full_conversation_log_root=tmp_path / "conversations",
        full_conversation_log_enabled=True,
        langsmith_enabled=False,
    )
    client = TestClient(
        gateway_app.create_app(
            settings, profile_secret_store=MemoryProfileSecretStore()
        )
    )
    created = client.put(
        "/api/actiondesign-agent/admin/profiles/team",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "id": "team",
            "provider": "team",
            "protocol": protocol,
            "baseUrl": "https://example.test/v1",
            "apiKey": profile().api_key,
            "defaultModel": "model-alias",
            "authMode": "bearer",
            "models": [
                {"id": "model-alias", "name": "Model", "supportsNativeTools": True}
            ],
        },
    )
    assert created.status_code == 200, created.text
    native = state(protocol)
    captured = []

    async def call(req, prof):
        captured.append(req)
        return {
            "provider": "team",
            "model": "model-alias",
            "success": True,
            "content": "checked",
            "tool_calls": [],
            "reasoning": native,
        }

    async def stream(req, prof):
        yield {"kind": "completed", "payload": await call(req, prof)}

    monkeypatch.setattr(gateway_app, "call_provider", call)
    monkeypatch.setattr(gateway_app, "stream_provider", stream)
    payload = request().model_dump(mode="json", by_alias=True)
    headers = {
        "X-Conversation-Id": payload["conversationId"],
        "X-Agent-Run-Id": payload["runId"],
        "X-Request-Id": payload["requestId"],
        "X-Correlation-Id": payload["correlationId"],
    }
    response = client.post(
        "/api/actiondesign-agent/chat", headers=headers, json=payload
    )
    assert response.status_code == 200, response.text
    assert response.json()["reasoning"] == native
    payload.update(stream=True, messages=history(response.json()["reasoning"]))
    streamed = client.post(
        "/api/actiondesign-agent/chat/stream", headers=headers, json=payload
    )
    events = [
        json.loads(line[6:])
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "model.completed", streamed.text
    assert events[-1]["payload"]["reasoning"] == native
    assert (
        captured[-1].messages[1].reasoning.model_dump(by_alias=True, exclude_none=True)
        == native
    )
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (tmp_path / "conversations").glob("*.jsonl")
    )
    expected = (
        "ABCD" * 128
        if protocol == "claude"
        else "IJKL" * 128
        if protocol == "openai-responses"
        else native["reasoningContent"].strip()
    )
    assert expected in text
    assert profile().api_key not in text


@pytest.mark.parametrize("protocol", ["claude", "openai"])
@pytest.mark.anyio
async def test_reasoning_stream_without_terminal_marker_is_rejected(
    monkeypatch, protocol
):
    events = (
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": state()["blocks"][0],
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": "partial"},
            },
        ]
        if protocol == "claude"
        else [
            {
                "choices": [
                    {"delta": {"content": "partial", "reasoning_content": "unfinished"}}
                ]
            }
        ]
    )

    async def handle(wire):
        return httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )

    monkeypatch.setattr(
        provider_http,
        "_provider_http_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(HTTPException):
        _ = [
            event
            async for event in provider_http.stream_provider(
                request(), profile(protocol)
            )
        ]
