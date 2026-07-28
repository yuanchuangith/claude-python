from types import SimpleNamespace

import anyio

from claude_agent_sdk.actiondesign_gateway.models import AgentChatRequest
from claude_agent_sdk.actiondesign_gateway.profile_store import ProviderProfile
from claude_agent_sdk.actiondesign_gateway import provider_http


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
        conversationId="conv",
        runId="run",
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


def test_request_body_forwards_native_tool_choice():
    req = AgentChatRequest(
        conversationId="conv",
        runId="run",
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
        conversationId="conv",
        runId="run",
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
        conversationId="conv",
        runId="run",
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
        conversationId="conv",
        runId="run",
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
        conversationId="conv",
        runId="run",
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
