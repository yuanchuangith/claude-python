import pytest
from fastapi import HTTPException

from claude_agent_sdk.actiondesign_gateway.models import (
    AgentChatRequest,
    ApiProfileModel,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import ProviderProfile
from claude_agent_sdk.actiondesign_gateway.request_budget import (
    _native_block_tokens,
    _provider_input_tokens,
    _tokens,
    normalize_usage,
    prepare_provider_body,
    response_accounting,
)


def profile(protocol="claude", window=16000):
    return ProviderProfile(id="team", provider="team", base_url="https://example.test/v1", api_key="secret",
        default_model="m", auth_mode="api-key", timeout_seconds=30, enabled=True, source="managed", protocol=protocol, models=[ApiProfileModel(id="m", contextWindowTokens=window, maxOutputTokens=1024)])


def request(**extra):
    return AgentChatRequest(provider="team", model="m", conversationId="11111111-1111-4111-8111-111111111111",
        runId="run_22222222-2222-4222-8222-222222222222", requestId="33333333-3333-4333-8333-333333333333",
        correlationId="44444444-4444-4444-8444-444444444444", systemPrompt="system", prompt=extra.pop("prompt", "hello"), **extra)


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
def test_complete_body_budget_and_output_limit(protocol):
    body, budget = prepare_provider_body(request(maxTokens=8192), profile(protocol), False)
    assert budget["reservedOutputTokens"] == 1024
    assert body["max_output_tokens" if protocol == "openai-responses" else "max_tokens"] == 1024
    with pytest.raises(HTTPException) as caught:
        prepare_provider_body(request(tools=[{"name": "result", "description": "large schema", "input_schema": {"type": "object", "properties": {"data": {"type": "string", "description": "x" * 100000}}}}]), profile(protocol), False)
    assert caught.value.status_code == 413
    assert caught.value.detail["budget"]["profileId"] == "team"


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
def test_image_estimate_independent_of_base64_size(protocol):
    _, small = prepare_provider_body(request(images=[{"media_type": "image/png", "data": "YQ=="}]), profile(protocol, 100000), False)
    _, large = prepare_provider_body(request(images=[{"media_type": "image/png", "data": __import__("base64").b64encode(b"a" * 10000).decode()}]), profile(protocol, 100000), False)
    assert small["estimatedInputTokens"] == large["estimatedInputTokens"]


def test_usage_unknown_and_no_double_counting():
    assert normalize_usage({}, "claude")["totalTokens"] is None
    claude = normalize_usage({"input_tokens": 20, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10, "output_tokens": 5}, "claude")
    assert claude["inputTokens"] == 110 and claude["totalTokens"] == 115
    chat = normalize_usage({"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 12}}, "openai")
    assert chat["totalTokens"] == 120 and chat["reasoningTokens"] == 12


def test_accounting_survives_final_response_redaction_and_validation():
    from claude_agent_sdk.actiondesign_gateway.models import AgentChatResponse
    from claude_agent_sdk.actiondesign_gateway.redaction import redact_secret_values
    payload = {"provider": "team", "model": "m", "content": "ok", "tool_calls": [], "success": True,
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10, "output_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 8}, "output_tokens_details": {"reasoning_tokens": 3}},
        "authorization": "Bearer secret-value", "accessToken": "secret-value"}
    accounted = response_accounting(request(protocolVersion="1.1"), profile(), payload)
    safe = redact_secret_values(accounted, ["secret-value"])
    assert safe["normalizedUsage"] == accounted["normalizedUsage"]
    assert safe["requestBudget"] == accounted["requestBudget"]
    assert safe["usage"] == payload["usage"]
    assert safe["authorization"] == "[REDACTED]" and safe["accessToken"] == "[REDACTED]"
    AgentChatResponse.model_validate({k: v for k, v in safe.items() if k not in {"authorization", "accessToken"}})


def test_sourced_auxiliary_result_keeps_graph_message_identity():
    from claude_agent_sdk.actiondesign_gateway.redaction import redact_secret_values
    source = "run_22222222-2222-4222-8222-222222222222:user:0"
    payload = {"sourceId": source, "sourceIds": [source], "prompt": 'sources: [{"id":"' + source + '"}]',
        "diagnostic": "user:private-account password=private-password", "accessToken": "private-token"}
    safe = redact_secret_values(payload, ["private-token"])
    assert safe["sourceId"] == source and safe["sourceIds"] == [source]
    assert safe["prompt"] == payload["prompt"]
    assert "private-account" not in safe["diagnostic"] and "private-password" not in safe["diagnostic"]
    assert safe["accessToken"] == "[REDACTED]"
    assert redact_secret_values({"sourceId": source}, [source])["sourceId"] == "[REDACTED]"
    assert "private-account" not in redact_secret_values(source.replace(":0", ":private-account"), [])


def test_old_protocol_keeps_shape_new_protocol_is_explicit():
    payload = {"provider": "team", "model": "m", "content": "ok", "tool_calls": [], "success": True, "usage": {}}
    assert response_accounting(request(), profile(), payload) == payload
    normalized = response_accounting(request(protocolVersion="1.1", taskPurpose="compaction"), profile(), payload)
    assert "normalizedUsage" in normalized and normalized["requestBudget"]["isEstimate"]


def test_small_fallback_model_rechecks_request():
    req = request(prompt="x" * 10000)
    prepare_provider_body(req, profile(window=100000), False)
    with pytest.raises(HTTPException):
        prepare_provider_body(req, profile(window=2048), False)


def test_opaque_reasoning_and_business_data_have_different_estimates():
    assert _native_block_tokens({"type": "redacted_thinking", "data": "a" * 100000}, "claude") < 100
    assert _native_block_tokens({"type": "reasoning", "encrypted_content": "a" * 100000, "summary": []}, "openai-responses") < 100
    assert _tokens({"data": {"description": "a" * 100000}}) > 20000
    assert _tokens({"image_url": {"description": "a" * 100000}}) > 20000
    assert _tokens({"signature": {"description": "a" * 100000}}) > 20000
    assert _tokens({"encrypted_content": {"description": "a" * 100000}}) > 20000


def test_image_dimensions_use_protocol_estimates():
    import base64
    import struct
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 750, 750)).decode()
    assert _native_block_tokens({"type": "image", "source": {"data": png}}, "claude") == 750
    assert _native_block_tokens({"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}, "openai") >= 4096
    assert _native_block_tokens({"type": "input_image", "detail": "low", "image_url": "https://example.test/image"}, "openai-responses") == 85


def test_native_boundaries_exclude_opaque_state_but_count_nested_business_arguments():
    payload = {"type": "reasoning", "encrypted_content": "x" * 100000}
    assert _provider_input_tokens({"input": [payload]}, "openai-responses") < 100
    assert _provider_input_tokens({"messages": [{"role": "assistant", "content": [
        {"type": "redacted_thinking", "data": "x" * 100000}]}]}, "claude") < 100
    assert _provider_input_tokens({"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "input": payload}]}]}, "claude") > 25000


@pytest.mark.parametrize("protocol", ["claude", "openai", "openai-responses"])
@pytest.mark.parametrize("business_type", ["reasoning", "redacted_thinking", "image"])
def test_business_schema_examples_cannot_hide_request_tokens(protocol, business_type):
    schema = {"type": "object", "examples": [{"type": business_type, "data": "x" * 100000,
        "encrypted_content": "x" * 100000, "source": {"data": "YQ=="}}]}
    with pytest.raises(HTTPException) as caught:
        prepare_provider_body(request(tools=[{"name": "result", "description": "business schema",
            "input_schema": schema}]), profile(protocol), False)
    assert caught.value.status_code == 413
    assert caught.value.detail["budget"]["estimatedInputTokens"] > 50000


@pytest.mark.anyio
async def test_actual_gateway_adapters_account_http_and_stream(monkeypatch):
    from claude_agent_sdk.actiondesign_gateway import app as gateway
    payload = {"provider": "team", "model": "m", "content": "ok", "tool_calls": [], "success": True, "usage": {"input_tokens": 123, "output_tokens": 9}}
    async def call(req, routed_profile):
        return payload
    async def stream(req, routed_profile):
        yield {"kind": "completed", "payload": payload}
    monkeypatch.setattr(gateway, "call_provider", call)
    monkeypatch.setattr(gateway, "stream_provider", stream)
    result = await gateway._call_route(request(protocolVersion="1.1"), profile())
    assert result["normalizedUsage"]["totalTokens"] == 132
    events = [e async for e in gateway._stream_route(request(protocolVersion="1.1", stream=True), profile())]
    assert events[0]["payload"]["normalizedUsage"]["totalTokens"] == 132
    assert await gateway._call_route(request(), profile()) == payload


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("stream", [False, True])
def test_accounting_survives_actual_http_and_sse_response(tmp_path, monkeypatch, stream):
    from claude_agent_sdk.actiondesign_gateway import app as gateway
    from tests.test_actiondesign_gateway_app import (
        chat_payload,
        identity_headers,
        make_client,
    )
    usage = {"input_tokens": 20, "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 10, "output_tokens": 5}
    result = {"provider": "mimo", "model": "mimo-v2.5", "content": "ok", "tool_calls": [],
        "success": True, "usage": usage}
    source = "run_22222222-2222-4222-8222-222222222222:user:0"
    result["tool_calls"] = [{"id": "memory-result", "name": "compaction_result", "arguments": {"sourceIds": [source]}}]
    async def call(req, routed_profile):
        return result
    async def events(req, routed_profile):
        yield {"kind": "completed", "payload": result}
    monkeypatch.setattr(gateway, "call_provider", call)
    monkeypatch.setattr(gateway, "stream_provider", events)
    req = chat_payload(protocolVersion="1.1", stream=stream, taskPurpose="compaction")
    response = make_client(tmp_path).post("/api/actiondesign-agent/chat" + ("/stream" if stream else ""),
        headers=identity_headers(req), json=req)
    assert response.status_code == 200
    if stream:
        import json
        envelopes = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert len(envelopes) == 1 and envelopes[0]["type"] == "model.completed"
        body = envelopes[0]["payload"]
    else:
        body = response.json()
    assert body["normalizedUsage"]["inputTokens"] == 110
    assert body["normalizedUsage"]["totalTokens"] == 115
    assert body["usage"] == usage
    assert isinstance(body["requestBudget"]["estimatedInputTokens"], int)
    assert isinstance(body["requestBudget"]["availableInputTokens"], int)
    assert body["requestBudget"]["profileId"] == "default-mimo"
    assert body["tool_calls"][0]["arguments"]["sourceIds"] == [source]
