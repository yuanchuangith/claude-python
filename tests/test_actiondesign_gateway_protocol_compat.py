import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.models import AgentChatResponse
from claude_agent_sdk.actiondesign_gateway.profile_store import ProviderProfile
from claude_agent_sdk.actiondesign_gateway.protocol_compat import (
    OpenAIChatCompletionRequest,
    anthropic_error_response,
    anthropic_message_response,
    anthropic_stream_response,
    anthropic_to_agent_request,
    openai_chat_completion_response,
    openai_error_response,
    openai_models_response,
    openai_stream_response,
    openai_to_agent_request,
)
from claude_agent_sdk.actiondesign_gateway.provider_http import (
    openai_request_body,
    request_body,
)

CONVERSATION_ID = "88888888-8888-4888-8888-888888888888"
RUN_ID = "run_88888888-8888-4888-8888-888888888888"
REQUEST_ID = "99999999-9999-4999-8999-999999999999"
CORRELATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _identity():
    return {
        "conversation_id": CONVERSATION_ID,
        "run_id": RUN_ID,
        "request_id": REQUEST_ID,
        "correlation_id": CORRELATION_ID,
    }


def _profile(protocol):
    return ProviderProfile(
        id=f"team-{protocol}",
        provider=f"team-{protocol}",
        display_name=f"Team {protocol}",
        protocol=protocol,
        base_url="https://example.test/v1",
        api_key="provider-secret",
        default_model="test-model",
        auth_mode="bearer",
        timeout_seconds=30,
        enabled=True,
        source="managed",
    )


def _response():
    return AgentChatResponse.model_validate(
        {
            "provider": "team-openai",
            "model": "test-model",
            "content": "I will inspect it.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "inspect_action",
                    "arguments": {"actionKey": "main"},
                }
            ],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    )


def test_models_response_uses_routing_order_and_deduplicates_model_ids():
    response = openai_models_response(
        {
            "routingOrder": [
                {"provider": "first", "model": "shared"},
                {"provider": "second", "model": "shared"},
                {"provider": "second", "model": "unique"},
            ],
            "providers": {
                "first": {"models": [{"id": "shared"}]},
                "third": {"models": [{"id": "fallback"}]},
            },
        },
        created=0,
    )

    assert response == {
        "object": "list",
        "data": [
            {"id": "shared", "object": "model", "created": 0, "owned_by": "first"},
            {"id": "unique", "object": "model", "created": 0, "owned_by": "second"},
            {"id": "fallback", "object": "model", "created": 0, "owned_by": "third"},
        ],
    }


def test_openai_request_preserves_structured_tool_history_for_existing_provider_chain():
    canonical = openai_to_agent_request(
        {
            "model": "test-model",
            "max_completion_tokens": 1024,
            "messages": [
                {"role": "system", "content": "Keep tool calls structured."},
                {"role": "user", "content": "Inspect main"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "inspect_action",
                                "arguments": '{"actionKey":"main"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "inspect_action",
                    "content": '{"exists":true}',
                },
                {"role": "user", "content": "Summarize it"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_action",
                        "description": "Inspect an action",
                        "parameters": {
                            "type": "object",
                            "properties": {"actionKey": {"type": "string"}},
                            "required": ["actionKey"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        **_identity(),
    )

    assert canonical.system_prompt == "Keep tool calls structured."
    assert canonical.prompt == "Summarize it"
    assert [message.role for message in canonical.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert canonical.messages[1].tool_calls[0].arguments == {"actionKey": "main"}
    assert canonical.messages[2].tool_call_id == "call_1"

    body = openai_request_body(canonical, _profile("openai"), False)
    assert body["messages"][0] == {
        "role": "system",
        "content": "Keep tool calls structured.",
    }
    assert body["messages"][2]["tool_calls"][0]["function"] == {
        "name": "inspect_action",
        "arguments": '{"actionKey":"main"}',
    }
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"exists":true}',
        "name": "inspect_action",
    }


def test_anthropic_request_preserves_tool_use_and_tool_result_for_existing_provider_chain():
    canonical = anthropic_to_agent_request(
        {
            "model": "test-model",
            "max_tokens": 1024,
            "system": [{"type": "text", "text": "Use native tools."}],
            "messages": [
                {"role": "user", "content": "Inspect main"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "inspect_action",
                            "input": {"actionKey": "main"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": '{"exists":true}'}],
                            "is_error": True,
                        },
                        {"type": "text", "text": "Summarize it"},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "inspect_action",
                    "description": "Inspect an action",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "tool", "name": "inspect_action"},
        },
        **_identity(),
    )

    assert canonical.system_prompt == "Use native tools."
    assert [message.role for message in canonical.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert canonical.messages[1].tool_calls[0].id == "toolu_1"
    assert canonical.messages[2].content == '{"exists":true}'
    assert canonical.messages[2].is_error is True
    assert canonical.tool_choice is not None
    assert canonical.tool_choice.name == "inspect_action"

    body = request_body(canonical, _profile("claude"), False)
    assert body["system"] == "Use native tools."
    assert body["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "inspect_action",
        "input": {"actionKey": "main"},
    }
    assert body["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"exists":true}',
            "is_error": True,
        },
        {"type": "text", "text": "Summarize it"},
    ]


def test_nonstream_responses_convert_text_tools_finish_reason_and_usage():
    openai = openai_chat_completion_response(
        _response(), completion_id="chatcmpl-fixed", created=123
    )
    anthropic = anthropic_message_response(_response(), message_id="msg_fixed")

    assert openai["id"] == "chatcmpl-fixed"
    assert openai["choices"][0]["finish_reason"] == "tool_calls"
    assert openai["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "inspect_action",
        "arguments": '{"actionKey":"main"}',
    }
    assert openai["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }

    assert anthropic["id"] == "msg_fixed"
    assert anthropic["stop_reason"] == "tool_use"
    assert anthropic["content"] == [
        {"type": "text", "text": "I will inspect it."},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "inspect_action",
            "input": {"actionKey": "main"},
        },
    ]
    assert anthropic["usage"] == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.asyncio
async def test_openai_stream_emits_chat_completion_chunks_tool_calls_usage_and_done():
    async def events():
        yield {"kind": "delta", "content": "I will "}
        yield {"kind": "delta", "content": "inspect."}
        yield {"kind": "completed", "payload": _response().model_dump(mode="json")}

    chunks = [
        item
        async for item in openai_stream_response(
            events(),
            model="test-model",
            completion_id="chatcmpl-fixed",
            created=123,
            include_usage=True,
        )
    ]

    assert chunks[-1] == "data: [DONE]\n\n"
    payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks[:-1]
    ]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[1]["choices"][0]["delta"] == {"content": "I will "}
    assert payloads[3]["choices"][0]["delta"]["tool_calls"][0]["function"] == {
        "name": "inspect_action",
        "arguments": '{"actionKey":"main"}',
    }
    assert payloads[4]["choices"][0]["finish_reason"] == "tool_calls"
    assert payloads[5]["choices"] == []
    assert payloads[5]["usage"]["total_tokens"] == 18


@pytest.mark.asyncio
async def test_anthropic_stream_accepts_existing_gateway_sse_envelopes():
    async def events():
        yield 'data: {"type":"model.delta","payload":{"content":"hello"}}\n\n'
        yield "data: " + json.dumps(
            {
                "type": "model.completed",
                "payload": _response().model_dump(mode="json"),
            },
            separators=(",", ":"),
        ) + "\n\n"

    chunks = [
        item
        async for item in anthropic_stream_response(
            events(), model="test-model", message_id="msg_fixed"
        )
    ]

    event_names = [chunk.splitlines()[0] for chunk in chunks]
    assert event_names == [
        "event: message_start",
        "event: content_block_start",
        "event: content_block_delta",
        "event: content_block_stop",
        "event: content_block_start",
        "event: content_block_delta",
        "event: content_block_stop",
        "event: message_delta",
        "event: message_stop",
    ]
    assert '"type":"text_delta","text":"hello"' in chunks[2]
    assert '"type":"input_json_delta"' in chunks[5]
    assert '"stop_reason":"tool_use"' in chunks[7]


def test_protocol_error_shapes_do_not_expose_internal_detail_envelopes():
    openai, openai_status = openai_error_response(
        {"code": "INVALID_API_KEY", "message": "Invalid API key"}, status_code=401
    )
    anthropic, anthropic_status = anthropic_error_response(
        {"code": "INVALID_API_KEY", "message": "Invalid API key"}, status_code=401
    )

    assert openai_status == anthropic_status == 401
    assert openai == {
        "error": {
            "message": "Invalid API key",
            "type": "authentication_error",
            "param": None,
            "code": "INVALID_API_KEY",
        }
    }
    assert anthropic == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "Invalid API key"},
    }

    with pytest.raises(ValidationError) as raised:
        OpenAIChatCompletionRequest.model_validate(
            {"model": "test-model", "messages": []}
        )
    invalid, invalid_status = openai_error_response(raised.value)
    assert invalid_status == 400
    assert invalid["error"]["type"] == "invalid_request_error"

    rate_limit, rate_limit_status = anthropic_error_response(
        HTTPException(
            status_code=429,
            detail={"code": "PROVIDER_RATE_LIMITED", "message": "Try later"},
        )
    )
    assert rate_limit_status == 429
    assert rate_limit["error"]["type"] == "rate_limit_error"
