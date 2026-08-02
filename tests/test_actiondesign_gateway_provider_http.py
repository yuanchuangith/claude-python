from types import SimpleNamespace

import anyio
import pytest

from claude_agent_sdk.actiondesign_gateway import provider_http
from claude_agent_sdk.actiondesign_gateway.models import AgentChatRequest
from claude_agent_sdk.actiondesign_gateway.profile_store import ProviderProfile

CONVERSATION_ID = "88888888-8888-4888-8888-888888888888"
RUN_ID = "run_88888888-8888-4888-8888-888888888888"
REQUEST_ID = "99999999-9999-4999-8999-999999999999"
CORRELATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def profile():
    return ProviderProfile(
        id="default-mimo",
        provider="mimo",
        base_url="https://example.test/anthropic",
        api_key="secret",
        default_model="mimo-v2.5",
        auth_mode="api-key",
        timeout_seconds=30,
        enabled=True,
        source="environment",
    )


def openai_profile():
    return ProviderProfile(
        id="team-openai",
        provider="team-openai",
        display_name="Team OpenAI",
        protocol="openai",
        base_url="https://example.test/openai/v1",
        api_key="secret",
        default_model="gpt-5.1",
        auth_mode="bearer",
        timeout_seconds=30,
        enabled=True,
        source="managed",
    )


def test_request_body_uses_provider_native_tools():
    req = AgentChatRequest(
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system rules",
        prompt="hello",
        tools=[
            {
                "name": "inspect_action",
                "description": "Inspect an action",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    body = provider_http.request_body(req, profile(), False)

    assert body["tools"] == [
        {
            "name": "inspect_action",
            "description": "Inspect an action",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["system"] == "system rules"
    assert provider_http.normalize_messages_url(profile().base_url) == "https://example.test/anthropic/v1/messages"


@pytest.mark.parametrize(
    ("normalizer", "base_url", "expected"),
    [
        (
            provider_http.normalize_messages_url,
            "https://example.test",
            "https://example.test/v1/messages",
        ),
        (
            provider_http.normalize_messages_url,
            "https://example.test/tenant",
            "https://example.test/tenant/v1/messages",
        ),
        (
            provider_http.normalize_messages_url,
            "https://example.test/tenant/v1",
            "https://example.test/tenant/v1/messages",
        ),
        (
            provider_http.normalize_messages_url,
            "https://example.test/tenant/v1/messages",
            "https://example.test/tenant/v1/messages",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test",
            "https://example.test/v1/chat/completions",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/tenant",
            "https://example.test/tenant/v1/chat/completions",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/tenant/v1",
            "https://example.test/tenant/v1/chat/completions",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/tenant/v1/chat/completions",
            "https://example.test/tenant/v1/chat/completions",
        ),
    ],
)
def test_provider_endpoint_normalization_preserves_controlled_base_path_prefix(
    normalizer,
    base_url,
    expected,
):
    assert normalizer(base_url) == expected


@pytest.mark.parametrize(
    ("normalizer", "base_url"),
    [
        (provider_http.normalize_messages_url, "https://example.test/messages"),
        (provider_http.normalize_messages_url, "https://example.test/tenant/messages"),
        (provider_http.normalize_messages_url, "https://example.test/tenant/v2/messages"),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/chat/completions",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/tenant/chat/completions",
        ),
        (
            provider_http.normalize_chat_completions_url,
            "https://example.test/tenant/v2/chat/completions",
        ),
    ],
)
def test_provider_endpoint_normalization_rejects_disguised_nonfixed_suffix(
    normalizer,
    base_url,
):
    with pytest.raises(provider_http.HTTPException) as raised:
        normalizer(base_url)

    assert raised.value.detail["code"] == "PROVIDER_ENDPOINT_INVALID"
    assert raised.value.detail["retryable"] is False


def test_provider_http_client_explicitly_disables_redirects(monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(provider_http.httpx, "AsyncClient", FakeClient)

    provider_http._provider_http_client(profile())
    provider_http._provider_http_client(openai_profile())

    assert len(captured) == 2
    assert all(item["follow_redirects"] is False for item in captured)


@pytest.mark.parametrize(
    ("protocol", "base_url", "expected"),
    [
        ("openai", "https://example.test", "https://example.test/v1/models"),
        (
            "openai",
            "https://example.test/tenant/v1/chat/completions",
            "https://example.test/tenant/v1/models",
        ),
        ("claude", "https://example.test", "https://example.test/v1/models"),
        (
            "claude",
            "https://example.test/tenant/v1/messages",
            "https://example.test/tenant/v1/models",
        ),
    ],
)
def test_model_discovery_url_uses_fixed_v1_models(protocol, base_url, expected):
    assert provider_http.normalize_models_url(base_url, protocol) == expected


@pytest.mark.parametrize(
    ("profile_factory", "expected_auth_header", "expected_name"),
    [
        (openai_profile, "Bearer secret", "GPT 5.1"),
        (profile, None, "Claude Test"),
    ],
)
def test_discover_provider_models_uses_protocol_auth_and_strict_data(
    monkeypatch,
    profile_factory,
    expected_auth_header,
    expected_name,
):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["x_api_key"] = request.headers.get("x-api-key")
        name_key = "name" if profile_factory is openai_profile else "display_name"
        return provider_http.httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-1", name_key: expected_name},
                    {"id": "model-1", name_key: "Duplicate"},
                ]
            },
        )

    transport = provider_http.httpx.MockTransport(handler)

    def client(_profile):
        return provider_http.httpx.AsyncClient(transport=transport, follow_redirects=False)

    monkeypatch.setattr(provider_http, "_provider_http_client", client)
    result = anyio.run(provider_http.discover_provider_models, profile_factory())

    assert captured["url"].endswith("/v1/models")
    assert captured["authorization"] == expected_auth_header
    assert captured["x_api_key"] == (None if expected_auth_header else "secret")
    assert result == {
        "models": [{"id": "model-1", "name": expected_name}],
        "truncated": False,
    }


def test_discover_provider_models_truncates_at_256(monkeypatch):
    payload = {"data": [{"id": f"model-{index}"} for index in range(257)]}
    transport = provider_http.httpx.MockTransport(
        lambda _request: provider_http.httpx.Response(200, json=payload)
    )
    monkeypatch.setattr(
        provider_http,
        "_provider_http_client",
        lambda _profile: provider_http.httpx.AsyncClient(transport=transport),
    )

    result = anyio.run(provider_http.discover_provider_models, openai_profile())

    assert len(result["models"]) == 256
    assert result["truncated"] is True


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (provider_http.httpx.Response(404), "PROVIDER_MODEL_DISCOVERY_UNSUPPORTED"),
        (
            provider_http.httpx.Response(200, json={"data": [{"name": "missing id"}]}),
            "PROVIDER_MODELS_RESPONSE_INVALID",
        ),
        (
            provider_http.httpx.Response(
                200,
                content=b"x" * (provider_http._MODEL_DISCOVERY_RESPONSE_MAX_BYTES + 1),
            ),
            "PROVIDER_MODELS_RESPONSE_TOO_LARGE",
        ),
    ],
)
def test_discover_provider_models_maps_unsafe_upstream_responses(
    monkeypatch,
    response,
    expected_code,
):
    transport = provider_http.httpx.MockTransport(lambda _request: response)
    monkeypatch.setattr(
        provider_http,
        "_provider_http_client",
        lambda _profile: provider_http.httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(provider_http.HTTPException) as raised:
        anyio.run(provider_http.discover_provider_models, openai_profile())

    assert raised.value.detail["code"] == expected_code


def test_request_body_forwards_native_tool_choice():
    req = AgentChatRequest(
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system rules",
        prompt="review",
        tools=[
            {
                "name": "review_result",
                "description": "Return review",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        toolChoice={"type": "tool", "name": "review_result"},
    )

    body = provider_http.request_body(req, profile(), False)

    assert body["tool_choice"] == {"type": "tool", "name": "review_result"}


def test_claude_request_forwards_enabled_thinking_budget():
    req = AgentChatRequest(
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system rules",
        prompt="reason carefully",
        maxTokens=8192,
        thinking={"type": "enabled", "budget_tokens": 4096},
    )

    body = provider_http.request_body(req, profile(), False)

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_openai_request_converts_messages_images_tools_and_tool_choice():
    req = AgentChatRequest(
        provider="team-openai",
        profileId="team-openai",
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system rules",
        prompt="inspect image",
        images=[{"media_type": "image/png", "data": "aGVsbG8="}],
        tools=[{
            "name": "inspect_action",
            "description": "Inspect an action",
            "input_schema": {"type": "object", "properties": {}},
        }],
        toolChoice={"type": "any"},
        thinking={"type": "enabled", "budget_tokens": 8192},
    )

    body = provider_http.openai_request_body(req, openai_profile(), False)

    assert provider_http.normalize_chat_completions_url(openai_profile().base_url) == (
        "https://example.test/openai/v1/chat/completions"
    )
    assert body["messages"][0] == {"role": "system", "content": "system rules"}
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert body["tools"][0]["function"]["parameters"]["type"] == "object"
    assert body["tool_choice"] == "required"
    assert body["reasoning_effort"] == "medium"


def test_parse_openai_response_preserves_function_calls_and_usage():
    parsed = provider_http.parse_openai_response(
        {
            "model": "gpt-5.1",
            "choices": [{
                "message": {
                    "content": "checking",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "inspect_action",
                            "arguments": '{"actionKey":"main"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        },
        "team-openai",
        "gpt-5.1",
        0,
    )

    assert parsed["provider"] == "team-openai"
    assert parsed["tool_calls"] == [{
        "id": "call_1",
        "name": "inspect_action",
        "arguments": {"actionKey": "main"},
    }]
    assert parsed["usage"]["finish_reason"] == "tool_calls"


def test_openai_stream_accumulates_text_and_function_arguments(monkeypatch):
    class FakeResponse:
        is_error = False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"model":"gpt-5.1","choices":[{"delta":{"content":"hello "}}]}'
            yield 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"inspect_action","arguments":"{\\"actionKey\\":"}}]}}]}'
            yield 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"main\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4}}'
            yield "data: [DONE]"

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(provider_http.httpx, "AsyncClient", FakeClient)
    req = AgentChatRequest(
        provider="team-openai",
        profileId="team-openai",
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system",
        prompt="inspect",
        tools=[{
            "name": "inspect_action",
            "description": "Inspect",
            "input_schema": {
                "type": "object",
                "properties": {"actionKey": {"type": "string"}},
                "required": ["actionKey"],
            },
        }],
    )

    async def collect():
        return [event async for event in provider_http.stream_openai(req, openai_profile())]

    events = anyio.run(collect)

    assert events[0] == {"kind": "delta", "content": "hello "}
    assert events[-1]["payload"]["tool_calls"] == [{
        "id": "call_1",
        "name": "inspect_action",
        "arguments": {"actionKey": "main"},
    }]
    assert events[-1]["payload"]["usage"]["finish_reason"] == "tool_calls"


def test_parse_response_preserves_native_tool_use():
    parsed = provider_http.parse_response(
        {
            "model": "mimo-v2.5",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "call_1", "name": "inspect_action", "input": {"actionKey": "main"}},
            ],
            "usage": {"input_tokens": 4},
        },
        "mimo",
        "mimo-v2.5",
        0,
    )

    assert parsed["content"] == "checking"
    assert parsed["tool_calls"] == [
        {"id": "call_1", "name": "inspect_action", "arguments": {"actionKey": "main"}}
    ]


def test_parse_response_marks_max_tokens_as_structured_failure():
    parsed = provider_http.parse_response(
        {
            "model": "mimo-v2.5",
            "content": [{"type": "text", "text": "partial"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 4, "output_tokens": 10},
        },
        "mimo",
        "mimo-v2.5",
        0,
    )

    assert parsed["success"] is False
    assert parsed["code"] == "PROVIDER_OUTPUT_TRUNCATED"
    assert parsed["usage"]["output_truncated"] is True


def test_response_tool_arguments_are_validated_against_request_schema():
    req = AgentChatRequest(
        conversationId=CONVERSATION_ID,
        runId=RUN_ID,
        requestId=REQUEST_ID,
        correlationId=CORRELATION_ID,
        systemPrompt="system rules",
        prompt="inspect",
        tools=[{
            "name": "inspect_action",
            "description": "Inspect",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"targetAction": {"type": "string", "minLength": 1}},
                "required": ["targetAction"],
            },
        }],
    )
    response = provider_http.AgentChatResponse.model_validate({
        "provider": "mimo",
        "model": "mimo-v2.5",
        "content": "",
        "tool_calls": [{"id": "call_1", "name": "inspect_action", "arguments": {}}],
        "success": True,
    })

    try:
        provider_http.validate_response_against_request(req, response)
    except Exception as exc:
        assert getattr(exc, "detail", {}).get("code") == "PROVIDER_TOOL_CALL_INVALID"
    else:
        raise AssertionError("invalid tool arguments must be rejected")


def test_stream_processes_final_sse_frame_without_blank_terminator(monkeypatch):
    class FakeResponse:
        is_error = False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"tail"}}'

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(provider_http.httpx, "AsyncClient", FakeClient)
    req = SimpleNamespace(
        model="mimo-v2.5",
        prompt="hello",
        images=[],
        tools=[],
        max_tokens=100,
    )

    async def collect():
        return [event async for event in provider_http.stream_messages(req, profile())]

    events = anyio.run(collect)

    assert events[0] == {"kind": "delta", "content": "tail"}
    assert events[-1]["payload"]["content"] == "tail"


def test_stream_buffers_error_response_before_mapping_status(monkeypatch):
    class ErrorStream(provider_http.httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"type":"invalid_key"}}'

        async def aclose(self):
            return None

    request = provider_http.httpx.Request("POST", "https://example.test/anthropic/v1/messages")
    response = provider_http.httpx.Response(401, request=request, stream=ErrorStream())

    class FakeStreamContext:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(provider_http.httpx, "AsyncClient", FakeClient)
    req = SimpleNamespace(
        model="mimo-v2.5",
        prompt="hello",
        images=[],
        tools=[],
        max_tokens=256,
    )

    async def collect():
        return [event async for event in provider_http.stream_messages(req, profile())]

    try:
        anyio.run(collect)
    except Exception as exc:
        assert getattr(exc, "detail", {}).get("code") == "PROVIDER_AUTH_FAILED"
        assert response.is_closed
    else:
        raise AssertionError("streaming provider errors must preserve the mapped error code")
