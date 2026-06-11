import json
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from fastapi import HTTPException

from claude_agent_sdk.actiondesign_gateway import (
    backend_tools,
    claude_code_provider,
    mimo_provider,
)
from claude_agent_sdk.actiondesign_gateway.backend_tools import (
    BackendToolCall,
    BackendToolResult,
    execute_backend_tool_calls,
    extract_backend_tool_calls,
)
from claude_agent_sdk.actiondesign_gateway.mimo_http import normalize_messages_url
from claude_agent_sdk.actiondesign_gateway.mimo_stream import iter_sse_events


def sse_payloads(events: list[str]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in events:
        for line in event.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def full_log_path(root, conversation_id):
    matches = sorted(root.glob(f"????????_??????_{conversation_id}.jsonl"))
    assert len(matches) == 1
    return matches[0]


async def _aiter(items: list[str]):
    for item in items:
        yield item


class FakeBackendExecutor:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[Any] = []
        self.result = result if result is not None else {"items": ["match"]}

    async def execute(self, call: Any) -> BackendToolResult:
        self.calls.append(call)
        return BackendToolResult(
            name=call.name,
            status="success",
            result=self.result,
        )


class FakeEmbeddingClient:
    def embed_documents(self, texts):
        return [self._embedding(text) for text in texts]

    def embed_query(self, text):
        return self._embedding(text)

    def _embedding(self, text):
        text = text.lower()
        if "required" in text or "validation" in text or "nullcondition" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def make_settings(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "mimo_api_key": "secret",
        "mimo_auth_mode": "api-key",
        "mimo_messages_url": "https://mimo.test/messages",
        "mimo_default_model": "mimo-v2.5",
        "mimo_timeout_seconds": 120,
        "mimo_max_backend_tool_turns": 6,
        "mimo_max_backend_tool_calls_per_turn": 4,
        "backend_tool_executor": FakeBackendExecutor(),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def write_knowledge_fixture(root):
    root.mkdir(parents=True)
    (root / "actions.md").write_text(
        """# ActionDesign Actions

## NullCondition

NullCondition validates required form fields before submit.
""",
        encoding="utf-8",
    )


def assert_read_before_write_prompt(prompt: str):
    assert "Read-before-write rule" in prompt
    assert "inputParams" in prompt
    assert "outputParams" in prompt
    assert "events" in prompt
    assert "methods" in prompt
    assert "examples" in prompt
    assert "constraints" in prompt
    assert "[BACKEND_TOOL_CALL] knowledge.search" in prompt
    assert "[BACKEND_TOOL_CALL] knowledge.read" in prompt
    assert "must not also" in prompt
    assert "create_node" in prompt
    assert "insert_node" in prompt
    assert "inspect_action" in prompt
    assert "get_element_detail" in prompt
    assert "get_component_methods" in prompt
    assert "NullCondition BeforeSubmit inputParams validation examples" in prompt


def test_mimo_system_prompt_requires_knowledge_read_before_write():
    prompt = mimo_provider._system_prompt({"prompt": "x"})

    assert_read_before_write_prompt(prompt)


def test_claude_code_prompt_requires_knowledge_read_before_write():
    prompt = claude_code_provider._prompt({"prompt": "x"})

    assert_read_before_write_prompt(prompt)
    assert "Claude Code internal tools" in prompt


def test_mimo_messages_url_accepts_anthropic_base_url():
    assert normalize_messages_url(
        "https://token-plan-cn.xiaomimimo.com/anthropic"
    ) == "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"
    assert normalize_messages_url(
        "https://token-plan-cn.xiaomimimo.com/anthropic/"
    ) == "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"
    assert normalize_messages_url(
        "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"
    ) == "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"
    assert normalize_messages_url("https://mimo.test/messages") == (
        "https://mimo.test/messages"
    )


def test_frontend_tool_used_as_backend_tool_gets_actionable_feedback():
    content = '[BACKEND_TOOL_CALL] get_element_detail({"elementKey":"ExitAction"})'

    calls = extract_backend_tool_calls(content)
    result = backend_tools.frontend_tool_misuse_result(content, calls)

    assert calls == []
    assert result is not None
    assert result.code == "BACKEND_TOOL_FRONTEND_TOOL_MISUSED"
    assert result.name == "get_element_detail"
    assert "[TOOL_CALL] get_element_detail" in result.error


def test_mimo_executes_backend_tool_then_returns_only_frontend_tool(monkeypatch):
    responses = [
        '[BACKEND_TOOL_CALL] mcp.search({"query":"ActionDesign validation"})',
        '可以创建节点\n[TOOL_CALL] create_node({"elementKey":"ExitAction"})',
    ]
    bodies: list[dict[str, Any]] = []
    settings = make_settings()

    async def fake_post_mimo(body, _settings):
        bodies.append(body)
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {
            "prompt": "build validation flow",
            "conversationId": "conv_loop",
            "toolNames": ["create_node"],
        },
        settings,
    )

    assert [call.name for call in settings.backend_tool_executor.calls] == [
        "mcp.search"
    ]
    assert len(bodies) == 2
    assert "[BACKEND_TOOL_RESULT] mcp.search" in str(bodies[1])
    assert response["provider"] == "mimo"
    assert response["tool_calls"] == [
        {"name": "create_node", "arguments": {"elementKey": "ExitAction"}}
    ]
    assert "BACKEND_TOOL_CALL" not in response["content"]
    assert "mcp.search" not in str(response["tool_calls"])


def test_mimo_executes_knowledge_search_then_returns_only_frontend_tool(
    tmp_path,
    monkeypatch,
):
    knowledge_root = tmp_path / "knowledge"
    write_knowledge_fixture(knowledge_root)
    responses = [
        '[BACKEND_TOOL_CALL] knowledge.search({"query":"required validation"})',
        '可以创建校验节点\n[TOOL_CALL] create_node({"elementKey":"NullCondition"})',
    ]
    bodies: list[dict[str, Any]] = []
    settings = make_settings(
        backend_tool_executor=None,
        embedding_client=FakeEmbeddingClient(),
        knowledge_root=knowledge_root,
        knowledge_index_dir=tmp_path / "index",
        knowledge_max_results=4,
        knowledge_max_chars_per_item=4000,
        knowledge_max_context_chars=12000,
    )

    async def fake_post_mimo(body, _settings):
        bodies.append(body)
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {
            "prompt": "build validation flow",
            "conversationId": "conv_knowledge",
            "toolNames": ["create_node"],
        },
        settings,
    )

    assert len(bodies) == 2
    assert "[BACKEND_TOOL_RESULT] knowledge.search" in str(bodies[1])
    assert "NullCondition" in str(bodies[1])
    assert response["tool_calls"] == [
        {"name": "create_node", "arguments": {"elementKey": "NullCondition"}}
    ]
    assert "knowledge.search" not in str(response["tool_calls"])


def test_mimo_full_conversation_log_records_model_and_backend_tool_events(
    tmp_path,
    monkeypatch,
):
    responses = [
        '[BACKEND_TOOL_CALL] knowledge.search({"query":"required validation"})',
        "final answer",
    ]
    settings = make_settings(
        full_conversation_log_enabled=True,
        full_conversation_log_root=tmp_path,
    )

    async def fake_post_mimo(_body, _settings):
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {
            "prompt": "lookup",
            "conversationId": "conv_provider",
            "runId": "run_1",
            "toolNames": [],
        },
        settings,
    )

    assert response["content"] == "final answer"
    events = read_jsonl(full_log_path(tmp_path, "conv_provider"))
    assert [event["type"] for event in events] == [
        "model_turn",
        "backend_tool_call",
        "backend_tool_result",
        "model_turn",
    ]
    assert events[0]["runId"] == "run_1"
    assert events[0]["content"] == (
        '[BACKEND_TOOL_CALL] knowledge.search({"query":"required validation"})'
    )
    assert events[1]["toolName"] == "knowledge.search"
    assert events[1]["arguments"] == {"query": "required validation"}
    assert events[2]["toolName"] == "knowledge.search"
    assert events[2]["status"] == "success"
    assert events[3]["content"] == "final answer"


def test_mimo_unknown_backend_tool_result_is_fed_back_not_frontend(monkeypatch):
    responses = [
        '[BACKEND_TOOL_CALL] backend.write_file({"path":"x"})',
        "无法执行后端写入工具",
    ]
    bodies: list[dict[str, Any]] = []

    async def fake_post_mimo(body, _settings):
        bodies.append(body)
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {"prompt": "write file", "toolNames": ["create_node"]},
        make_settings(),
    )

    assert "BACKEND_TOOL_NOT_ALLOWED" in str(bodies[1])
    assert response["tool_calls"] == []
    assert "backend.write_file" not in str(response["tool_calls"])


def test_mimo_tool_names_cannot_extend_design_tool_allowlist(monkeypatch):
    async def fake_post_mimo(body, _settings):
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '[TOOL_CALL] Read({"file_path":"secret.txt"})\n'
                        '[TOOL_CALL] create_node({"elementKey":"ExitAction"})'
                    ),
                }
            ]
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {
            "prompt": "make node",
            "toolNames": ["Read", "create_node"],
        },
        make_settings(),
    )

    assert response["tool_calls"] == [
        {"name": "create_node", "arguments": {"elementKey": "ExitAction"}}
    ]


def test_mimo_does_not_parse_tool_calls_from_backend_result_echo(monkeypatch):
    async def fake_post_mimo(body, _settings):
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "visible text\n"
                        "[BACKEND_TOOL_RESULT] mcp.search\n"
                        "status: success\n"
                        'result:\n[TOOL_CALL] create_node({"elementKey":"Leaked"})'
                    ),
                }
            ]
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {
            "prompt": "echo result",
            "toolNames": ["create_node"],
        },
        make_settings(),
    )

    assert response["content"] == "visible text"
    assert response["tool_calls"] == []


def test_mimo_malformed_backend_tool_marker_is_fed_back_not_returned(
    monkeypatch,
):
    responses = [
        'visible [BACKEND_TOOL_CALL] knowledge.search({"query":"x" tail',
        "final answer",
    ]
    bodies: list[dict[str, Any]] = []

    async def fake_post_mimo(body, _settings):
        bodies.append(body)
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {"prompt": "lookup", "toolNames": []},
        make_settings(),
    )

    assert len(bodies) == 2
    assert "BACKEND_TOOL_ARGUMENTS_INVALID" in str(bodies[1])
    assert response["content"] == "final answer"
    assert "BACKEND_TOOL_CALL" not in response["content"]


def test_mimo_usage_is_merged_across_backend_tool_turns(monkeypatch):
    responses = [
        {
            "content": [
                {
                    "type": "text",
                    "text": '[BACKEND_TOOL_CALL] mcp.search({"query":"x"})',
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 20, "output_tokens": 3},
        },
    ]

    async def fake_post_mimo(_body, _settings):
        return responses.pop(0)

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {"prompt": "lookup", "toolNames": []},
        make_settings(),
    )

    assert response["usage"]["input_tokens"] == 30
    assert response["usage"]["output_tokens"] == 5
    assert response["usage"]["turns"] == [
        {"input_tokens": 10, "output_tokens": 2},
        {"input_tokens": 20, "output_tokens": 3},
    ]


def test_mimo_single_turn_usage_keeps_legacy_shape(monkeypatch):
    async def fake_post_mimo(_body, _settings):
        return {
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {"prompt": "single", "toolNames": []},
        make_settings(),
    )

    assert response["usage"] == {"input_tokens": 10, "output_tokens": 2}


def test_mimo_malformed_frontend_tool_marker_is_cleaned(monkeypatch):
    async def fake_post_mimo(_body, _settings):
        return {
            "content": [
                {
                    "type": "text",
                    "text": 'visible [TOOL_CALL] create_node({"elementKey":"x"',
                }
            ],
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo,
        {"prompt": "bad frontend tool", "toolNames": ["create_node"]},
        make_settings(),
    )

    assert response["success"] is True
    assert response["content"] == "visible"
    assert response["tool_calls"] == []
    assert "TOOL_CALL" not in response["content"]


def test_mimo_stream_uses_upstream_sse_and_emits_incremental_text(
    monkeypatch,
):
    bodies: list[dict[str, Any]] = []

    async def fake_stream_post_mimo(body, _settings):
        bodies.append(body)
        yield {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hel"},
        }
        yield {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "lo"},
        }
        yield {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }
        yield {"type": "message_stop"}

    async def fail_post_mimo(_body, _settings):
        raise AssertionError("_post_mimo must not be used by stream_mimo")

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )
    monkeypatch.setattr(mimo_provider, "_post_mimo", fail_post_mimo)

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "stream", "toolNames": []},
                make_settings(),
            )
        ]

    events = anyio.run(collect_events)
    payloads = sse_payloads(events)

    assert bodies[0]["stream"] is True
    assert [
        payload["content"] for payload in payloads if payload["type"] == "text_delta"
    ] == ["hel", "lo"]
    assert payloads[-1]["type"] == "message_complete"
    assert payloads[-1]["success"] is True


def test_mimo_stream_full_conversation_log_records_assembled_turn(
    tmp_path,
    monkeypatch,
):
    async def fake_stream_post_mimo(_body, _settings):
        yield {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hel"},
        }
        yield {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "lo"},
        }
        yield {"type": "message_stop"}

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {
                    "prompt": "stream",
                    "conversationId": "conv_stream_provider",
                    "runId": "run_stream",
                    "toolNames": [],
                },
                make_settings(
                    full_conversation_log_enabled=True,
                    full_conversation_log_root=tmp_path,
                ),
            )
        ]

    payloads = sse_payloads(anyio.run(collect_events))
    events = read_jsonl(full_log_path(tmp_path, "conv_stream_provider"))

    assert [
        payload["content"] for payload in payloads if payload["type"] == "text_delta"
    ] == ["hel", "lo"]
    assert [event["type"] for event in events] == ["model_turn"]
    assert events[0]["content"] == "hello"
    assert events[0]["runId"] == "run_stream"


def test_mimo_review_parses_json_response(monkeypatch):
    captured_bodies = []

    async def fake_post_mimo(body, _settings):
        captured_bodies.append(body)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "pass": False,
                            "summary": "需要显式 return false",
                            "issues": [
                                {
                                    "severity": "error",
                                    "code": "EXIT_RETURN_VALUE",
                                    "message": "ExitAction 缺少 returnValue=false",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo_review,
        {
            "prompt": "review code",
            "conversationId": "conv_review_provider",
            "runId": "run_review",
            "model": "mimo-v2.5",
        },
        make_settings(),
    )

    assert response["provider"] == "mimo"
    assert response["model"] == "mimo-v2.5"
    assert response["pass"] is False
    assert response["issues"][0]["code"] == "EXIT_RETURN_VALUE"
    assert response["success"] is True
    assert response["usage"] == {"input_tokens": 3, "output_tokens": 5}
    assert "Return only valid JSON" in captured_bodies[0]["system"]


def test_mimo_review_rejects_frontend_tool_calls(monkeypatch):
    async def fake_post_mimo(_body, _settings):
        return {
            "content": [
                {
                    "type": "text",
                    "text": '[TOOL_CALL] create_node({"elementKey":"ExitAction"})',
                }
            ],
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    response = anyio.run(
        mimo_provider.call_mimo_review,
        {
            "prompt": "review code",
            "conversationId": "conv_review_tool",
            "runId": "run_review_tool",
        },
        make_settings(),
    )

    assert response["success"] is False
    assert response["pass"] is False
    assert response["code"] == "REVIEW_TOOL_CALL_NOT_ALLOWED"
    assert response["issues"][0]["code"] == "REVIEW_TOOL_CALL_NOT_ALLOWED"
    assert response["issues"][0]["severity"] == "error"


def test_model_turn_log_summarizes_frontend_tools_and_content_diagnostics(tmp_path):
    content = (
        '[TOOL_CALL] create_node({"elementKey":"ExitAction"})\n'
        '[TOOL_CALL] preview_code({"targetAction":"main"})\n'
        'bad fragment \x00 [TOOL_CALL] create_node({"elementKey":"x"'
    )

    mimo_provider._log_model_turn(
        {
            "conversationId": "conv_tool_diagnostics",
            "runId": "run_tool_diagnostics",
            "toolNames": ["create_node", "preview_code"],
        },
        make_settings(
            full_conversation_log_enabled=True,
            full_conversation_log_root=tmp_path,
        ),
        "mimo-v2.5",
        content,
        "end_turn",
        {},
        False,
    )

    raw = full_log_path(tmp_path, "conv_tool_diagnostics").read_text(
        encoding="utf-8"
    )
    assert len(raw.splitlines()) == 1
    events = [json.loads(raw)]
    assert events[0]["content"] == content
    assert events[0]["frontendToolCallCount"] == 2
    assert events[0]["frontendToolCallNames"] == ["create_node", "preview_code"]
    assert events[0]["contentDiagnostics"]["containsControlCharacters"] is True
    assert events[0]["contentDiagnostics"]["containsMalformedFrontendToolCall"] is True


def test_mimo_stream_buffers_backend_tool_turn_until_final_turn(monkeypatch):
    turns = [
        [
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "searching first "},
            },
            {
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": '[BACKEND_TOOL_CALL] knowledge.search({"query":"x"})',
                },
            },
            {"type": "message_stop"},
        ],
        [
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "final answer"},
            },
            {"type": "message_stop"},
        ],
    ]

    async def fake_stream_post_mimo(_body, _settings):
        for event in turns.pop(0):
            yield event

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "preview", "toolNames": []},
                make_settings(),
            )
        ]

    payloads = sse_payloads(anyio.run(collect_events))
    text_deltas = [
        payload["content"] for payload in payloads if payload["type"] == "text_delta"
    ]

    assert text_deltas == ["final answer"]
    assert "searching first" not in "".join(json.dumps(payload) for payload in payloads)
    assert payloads[-1]["type"] == "message_complete"
    assert payloads[-1]["content"] == "final answer"


def test_mimo_stream_cleans_malformed_frontend_tool_marker(monkeypatch):
    async def fake_stream_post_mimo(_body, _settings):
        yield {
            "type": "content_block_delta",
            "delta": {
                "type": "text_delta",
                "text": 'visible [TOL_CALL] create_node({"elementKey":"x"',
            },
        }
        yield {"type": "message_stop"}

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "bad frontend tool", "toolNames": ["create_node"]},
                make_settings(),
            )
        ]

    payload = "".join(anyio.run(collect_events))

    assert "TOL_CALL" not in payload
    assert "TOOL_CALL" not in payload
    assert '"content": "visible"' in payload


def test_mimo_stream_hides_backend_tool_protocol(monkeypatch):
    responses = [
        '[BACKEND_TOOL_CALL] skill.search({"query":"validation"})',
        '完成\n[TOOL_CALL] preview_code({"targetAction":"main"})',
    ]

    async def fake_stream_post_mimo(_body, _settings):
        yield {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": responses.pop(0)},
        }
        yield {"type": "message_stop"}

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "preview", "toolNames": ["preview_code"]},
                make_settings(),
            )
        ]

    events = anyio.run(collect_events)
    payload = "".join(events)

    assert "BACKEND_TOOL_CALL" not in payload
    assert "BACKEND_TOOL_RESULT" not in payload
    assert '"message_complete"' in payload
    assert '"preview_code"' in payload


def test_mimo_stream_loop_limit_reports_error_code(monkeypatch):
    async def fake_stream_post_mimo(_body, _settings):
        yield {
            "type": "content_block_delta",
            "delta": {
                "type": "text_delta",
                "text": '[BACKEND_TOOL_CALL] mcp.search({"query":"again"})',
            },
        }
        yield {"type": "message_stop"}

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "loop", "toolNames": []},
                make_settings(mimo_max_backend_tool_turns=1),
            )
        ]

    payload = "".join(anyio.run(collect_events))

    assert '"success": false' in payload
    assert "BACKEND_TOOL_LOOP_LIMIT" in payload


def test_mimo_stream_error_event_returns_structured_code(monkeypatch):
    async def fake_stream_post_mimo(_body, _settings):
        yield {
            "type": "error",
            "error": {"type": "upstream_error", "message": "stream broke"},
        }

    monkeypatch.setattr(
        mimo_provider,
        "_stream_post_mimo",
        fake_stream_post_mimo,
        raising=False,
    )

    async def collect_events():
        return [
            event
            async for event in mimo_provider.stream_mimo(
                {"prompt": "stream", "toolNames": []},
                make_settings(),
            )
        ]

    complete = sse_payloads(anyio.run(collect_events))[-1]

    assert complete["success"] is False
    assert complete["code"] == "MIMO_STREAM_ERROR"
    assert "stream broke" in complete["error"]


def test_mimo_sse_parser_supports_multiline_data_and_event_fallback():
    async def collect():
        lines = [
            "event: content_block_delta",
            'data: {"delta":{"type":"text_delta",',
            'data: "text":"hello"}}',
            "",
            "data: [DONE]",
            "",
        ]
        return [event async for event in iter_sse_events(_aiter(lines))]

    events = anyio.run(collect)

    assert events == [
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        }
    ]


def test_mimo_sse_parser_invalid_json_is_structured():
    async def collect():
        return [event async for event in iter_sse_events(_aiter(["data: {bad", ""]))]

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(collect)

    assert exc_info.value.detail["code"] == "MIMO_RESPONSE_INVALID"


def test_mimo_backend_tool_loop_limit(monkeypatch):
    async def fake_post_mimo(body, _settings):
        return {
            "content": [
                {
                    "type": "text",
                    "text": '[BACKEND_TOOL_CALL] mcp.search({"query":"again"})',
                }
            ]
        }

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

    with pytest.raises(Exception) as exc_info:
        anyio.run(
            mimo_provider.call_mimo,
            {"prompt": "loop", "toolNames": []},
            make_settings(mimo_max_backend_tool_turns=1),
        )

    assert getattr(exc_info.value, "status_code", None) == 502
    assert exc_info.value.detail["code"] == "BACKEND_TOOL_LOOP_LIMIT"


def test_mimo_timeout_error_is_structured(monkeypatch):
    async def fake_post(self, *args, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(
            mimo_provider.call_mimo,
            {"prompt": "hello", "toolNames": []},
            make_settings(),
        )

    assert exc_info.value.detail["code"] == "MIMO_UPSTREAM_TIMEOUT"


def test_mimo_network_error_is_structured(monkeypatch):
    async def fake_post(self, *args, **kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(
            mimo_provider.call_mimo,
            {"prompt": "hello", "toolNames": []},
            make_settings(),
        )

    assert exc_info.value.detail["code"] == "MIMO_UPSTREAM_NETWORK_ERROR"


def test_mimo_invalid_json_response_is_structured(monkeypatch):
    class BadJsonResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("bad json")

    async def fake_post(self, *args, **kwargs):
        return BadJsonResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(
            mimo_provider.call_mimo,
            {"prompt": "hello", "toolNames": []},
            make_settings(),
        )

    assert exc_info.value.detail["code"] == "MIMO_RESPONSE_INVALID"


def test_mimo_image_support_uses_models_image_whitelist(monkeypatch):
    monkeypatch.setattr(
        mimo_provider,
        "MIMO_IMAGE_MODELS",
        frozenset({"new-image-model"}),
        raising=False,
    )

    mimo_provider._reject_unsupported_images(
        {"images": [{"data": "abc"}]},
        "new-image-model",
    )

    with pytest.raises(HTTPException) as exc_info:
        mimo_provider._reject_unsupported_images(
            {"images": [{"data": "abc"}]},
            "text-only-model",
        )

    assert exc_info.value.detail["fallbackModel"] == "new-image-model"


def test_mcp_call_tool_requires_configured_read_only_nested_tool():
    settings = make_settings()

    results = anyio.run(
        execute_backend_tool_calls,
        [
            BackendToolCall(
                name="mcp.call_tool",
                arguments={"name": "write_file", "arguments": {}},
            )
        ],
        settings,
    )

    assert settings.backend_tool_executor.calls == []
    assert results[0].status == "failed"
    assert results[0].code == "BACKEND_TOOL_NOT_ALLOWED"


def test_mcp_call_tool_allows_configured_read_only_nested_tool():
    settings = make_settings(mcp_read_only_tool_names=["lookup_resource"])

    results = anyio.run(
        execute_backend_tool_calls,
        [
            BackendToolCall(
                name="mcp.call_tool",
                arguments={"name": "lookup_resource", "arguments": {}},
            )
        ],
        settings,
    )

    assert [call.name for call in settings.backend_tool_executor.calls] == [
        "mcp.call_tool"
    ]
    assert results[0].status == "success"
