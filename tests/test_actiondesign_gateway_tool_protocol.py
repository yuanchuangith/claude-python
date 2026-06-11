import json

import anyio
import pytest

from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock
from claude_agent_sdk.actiondesign_gateway.claude_code_provider import (
    call_claude_code,
    call_claude_code_review,
)
from claude_agent_sdk.actiondesign_gateway.mimo_provider import call_mimo
from claude_agent_sdk.actiondesign_gateway.settings import Settings
from claude_agent_sdk.actiondesign_gateway.tool_protocol import (
    clean_tool_protocol_text,
    emit_safe_text,
    extract_tool_calls,
    flush_pending,
    normalize_image_data,
)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def full_log_path(root, conversation_id):
    matches = sorted(root.glob(f"????????_??????_{conversation_id}.jsonl"))
    assert len(matches) == 1
    return matches[0]


def test_extracts_typo_tool_call_marker():
    content = '[TOL_CALL] create_node({"elementKey":"NullCondition","targetAction":"validateForm"})'

    calls = extract_tool_calls(content)

    assert len(calls) == 1
    assert calls[0]["name"] == "create_node"
    assert calls[0]["arguments"]["elementKey"] == "NullCondition"


def test_repairs_unquoted_keys_without_corrupting_url_strings():
    content = '[TOOL_CALL] create_node({elementKey:"OpenMessageDialog",params:{"url":"http://host:8888/a:b"}})'

    calls = extract_tool_calls(content)

    assert len(calls) == 1
    assert calls[0]["arguments"]["elementKey"] == "OpenMessageDialog"
    assert calls[0]["arguments"]["params"]["url"] == "http://host:8888/a:b"


def test_extract_keeps_repeated_mutating_tool_calls():
    content = """
[TOOL_CALL] create_node({"elementKey":"ExitAction","targetAction":"main"})
[TOOL_CALL] create_node({"elementKey":"ExitAction","targetAction":"main"})
"""

    calls = extract_tool_calls(content)

    assert len(calls) == 2
    assert calls[0]["name"] == "create_node"
    assert calls[1]["name"] == "create_node"


def test_extracts_json_actions_from_code_block():
    content = """
说明文本
```json
{"actions":{"main":[{"elementKey":"NullCondition","paramsValue":{"required":true}}]}}
```
"""

    calls = extract_tool_calls(content)

    assert len(calls) == 1
    assert calls[0]["name"] == "create_node"
    assert calls[0]["arguments"]["element"] == "NullCondition"
    assert calls[0]["arguments"]["params"] == {"required": True}
    assert calls[0]["arguments"]["actionKey"] == "main"


def test_question_alias_args_transform_to_ask_user_shape():
    content = """[TOOL_CALL] Question({"questions":[{"question":"Which fields?","header":"Form","options":[{"label":"Required","description":"Only required fields"},"All fields"]}]})"""

    calls = extract_tool_calls(content)

    assert calls == [
        {
            "name": "ask_user",
            "arguments": {
                "question": "Which fields?",
                "context": "Form",
                "suggestedOptions": ["Required（Only required fields）", "All fields"],
            },
        }
    ]


def test_clean_tool_protocol_text_removes_calls_without_truncating_later_text():
    content = 'before [TOL_CALL] preview_code({"targetAction":"main"}) after'

    cleaned = clean_tool_protocol_text(content)

    assert cleaned == "before  after"


def test_safe_text_emitter_hides_tool_call_split_across_chunks():
    pending = ""
    chunks, calls, pending = emit_safe_text(pending, "hello [TOOL", {"create_node"})
    assert chunks == ["hello "]
    assert calls == []

    chunks, calls, pending = emit_safe_text(
        pending,
        '_CALL] create_node({"elementKey":"ExitAction"}) tail',
        {"create_node"},
    )

    assert chunks == [" tail"]
    assert calls == [
        {
            "name": "create_node",
            "arguments": {"elementKey": "ExitAction"},
        }
    ]
    assert pending == ""


def test_flush_pending_returns_plain_text_or_extracts_tool_call():
    text, calls = flush_pending("[TOOL_CALL] preview_code({})", {"preview_code"})
    assert text is None
    assert calls == [{"name": "preview_code", "arguments": {}}]

    text, calls = flush_pending("[TOOL maybe text", {"preview_code"})
    assert text == "[TOOL maybe text"
    assert calls == []


def test_normalize_image_data_strips_data_url_prefix():
    assert normalize_image_data("data:image/png;base64,abc123") == "abc123"
    assert normalize_image_data("abc123") == "abc123"


def test_claude_code_filters_text_protocol_to_design_tools(monkeypatch):
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        yield AssistantMessage(
            content=[
                TextBlock(
                    '[TOOL_CALL] Read({"file_path":"secret.txt"})\n'
                    '[TOOL_CALL] Bash({"cmd":"rm -rf ."})\n'
                    '[TOOL_CALL] create_node({"elementKey":"ExitAction"})'
                )
            ],
            model="test",
        )

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {
            "prompt": "make action",
            "toolNames": ["create_node", "Bash"],
        },
        Settings(log_root=None),
    )

    assert response["tool_calls"] == [
        {"name": "create_node", "arguments": {"elementKey": "ExitAction"}}
    ]
    assert captured["options"].tools == ["Read", "Grep", "Glob", "LS"]
    assert captured["options"].allowed_tools == ["Read", "Grep", "Glob", "LS"]
    assert "Claude Code internal tools" in captured["prompt"]
    assert "ActionDesign frontend tools" in captured["prompt"]


def test_claude_code_executes_backend_tool_loop_then_returns_frontend_tool(
    monkeypatch,
):
    responses = [
        '[BACKEND_TOOL_CALL] knowledge.search({"query":"required validation"})',
        '可以创建校验节点\n[TOOL_CALL] create_node({"elementKey":"NullCondition"})',
    ]
    prompts = []
    calls = []

    class FakeBackendExecutor:
        async def execute(self, call):
            calls.append(call)
            from claude_agent_sdk.actiondesign_gateway.backend_tools import (
                BackendToolResult,
            )

            return BackendToolResult(
                name=call.name,
                status="success",
                result={"results": [{"heading": "NullCondition"}]},
            )

    async def fake_query(prompt, options):
        prompts.append(prompt)
        yield AssistantMessage(content=[TextBlock(responses.pop(0))], model="test")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {"prompt": "build validation flow", "toolNames": ["create_node"]},
        Settings(
            log_root=None,
            backend_tool_executor=FakeBackendExecutor(),
            claude_code_max_backend_tool_turns=4,
            claude_code_max_backend_tool_calls_per_turn=2,
        ),
    )

    assert [call.name for call in calls] == ["knowledge.search"]
    assert len(prompts) == 2
    assert "[BACKEND_TOOL_RESULT] knowledge.search" in prompts[1]
    assert response["tool_calls"] == [
        {"name": "create_node", "arguments": {"elementKey": "NullCondition"}}
    ]
    assert "BACKEND_TOOL_CALL" not in response["content"]


def test_claude_code_full_conversation_log_records_model_and_backend_tool_events(
    tmp_path,
    monkeypatch,
):
    responses = [
        '[BACKEND_TOOL_CALL] knowledge.search({"query":"required validation"})',
        "final answer",
    ]

    class FakeBackendExecutor:
        async def execute(self, call):
            from claude_agent_sdk.actiondesign_gateway.backend_tools import (
                BackendToolResult,
            )

            return BackendToolResult(
                name=call.name,
                status="success",
                result={"results": [{"heading": "NullCondition"}]},
            )

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(responses.pop(0))], model="test")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {
            "prompt": "build validation flow",
            "conversationId": "conv_claude_provider",
            "runId": "run_claude",
            "toolNames": [],
        },
        Settings(
            log_root=None,
            backend_tool_executor=FakeBackendExecutor(),
            claude_code_max_backend_tool_turns=4,
            claude_code_max_backend_tool_calls_per_turn=2,
            full_conversation_log_enabled=True,
            full_conversation_log_root=tmp_path,
        ),
    )

    assert response["content"] == "final answer"
    events = read_jsonl(full_log_path(tmp_path, "conv_claude_provider"))
    assert [event["type"] for event in events] == [
        "model_turn",
        "backend_tool_call",
        "backend_tool_result",
        "model_turn",
    ]
    assert events[0]["provider"] == "claude-code"
    assert events[0]["runId"] == "run_claude"
    assert events[1]["toolName"] == "knowledge.search"
    assert events[1]["arguments"] == {"query": "required validation"}
    assert events[2]["status"] == "success"
    assert events[3]["content"] == "final answer"


def test_claude_code_internal_tools_can_be_configured_without_auto_allow(monkeypatch):
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock("ok")], model="test")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {"prompt": "read only", "toolNames": []},
        Settings(
            log_root=None,
            claude_code_internal_tools=["Read"],
            claude_code_auto_allow_internal_tools=False,
        ),
    )

    assert response["success"] is True
    assert captured["options"].tools == ["Read"]
    assert captured["options"].allowed_tools == []


def test_claude_code_review_parses_json_response(monkeypatch):
    async def fake_query(prompt, options):
        assert "Return only valid JSON" in prompt
        yield AssistantMessage(
            content=[
                TextBlock(
                    json.dumps(
                        {
                            "pass": True,
                            "summary": "review passed",
                            "issues": [],
                        },
                        ensure_ascii=False,
                    )
                )
            ],
            model="test",
        )

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code_review,
        {
            "prompt": "review code",
            "conversationId": "conv_claude_review_provider",
            "runId": "run_claude_review",
        },
        Settings(log_root=None),
    )

    assert response["provider"] == "claude-code"
    assert response["pass"] is True
    assert response["summary"] == "review passed"
    assert response["issues"] == []
    assert response["success"] is True


def test_claude_code_uses_mimo_default_model_when_unconfigured(monkeypatch):
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock("ok")], model="test")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {"prompt": "use default model", "toolNames": []},
        Settings(log_root=None, mimo_default_model="mimo-v2.5-pro"),
    )

    assert captured["options"].model == "mimo-v2.5-pro"
    assert response["model"] == "mimo-v2.5-pro"


def test_claude_code_uses_first_configured_mimo_model_as_default(monkeypatch):
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock("ok")], model="test")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    response = anyio.run(
        call_claude_code,
        {"prompt": "use default model", "toolNames": []},
        Settings(
            log_root=None,
            claude_code_models=["mimo-v2.5-pro", "mimo-v2.5"],
        ),
    )

    assert captured["options"].model == "mimo-v2.5-pro"
    assert response["model"] == "mimo-v2.5-pro"


def test_claude_code_stream_does_not_duplicate_single_text_tool_call(monkeypatch):
    from claude_agent_sdk.actiondesign_gateway.claude_code_provider import (
        stream_claude_code,
    )

    async def fake_query(prompt, options):
        yield StreamEvent(
            uuid="event_1",
            session_id="session_1",
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": '[TOOL_CALL] create_node({"elementKey":"ExitAction"})',
                },
            },
        )
        yield AssistantMessage(
            content=[TextBlock('[TOOL_CALL] create_node({"elementKey":"ExitAction"})')],
            model="test",
        )

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.claude_code_provider.query",
        fake_query,
    )

    async def collect_events():
        return [
            event
            async for event in stream_claude_code(
                {"prompt": "make action", "toolNames": ["create_node"]},
                Settings(log_root=None),
            )
        ]

    events = anyio.run(collect_events)

    complete = [event for event in events if '"message_complete"' in event][0]
    assert complete.count('"create_node"') == 1


def test_mimo_rejects_images_for_non_mimo_image_model(monkeypatch):
    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("upstream should not be called")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.mimo_provider.httpx.AsyncClient",
        FailingAsyncClient,
    )

    with pytest.raises(Exception) as exc_info:
        anyio.run(
            call_mimo,
            {
                "prompt": "look",
                "model": "gpt-5.5",
                "images": [{"media_type": "image/png", "data": "abc"}],
            },
            Settings(mimo_api_key="secret", log_root=None),
        )

    assert getattr(exc_info.value, "status_code", None) == 400
    assert exc_info.value.detail["code"] == "MODEL_DOES_NOT_SUPPORT_IMAGES"


def test_mimo_body_does_not_include_claude_code_internal_prompt():
    from claude_agent_sdk.actiondesign_gateway.mimo_provider import _messages_body

    body = _messages_body(
        {"prompt": "plain ActionDesign prompt", "toolNames": []},
        Settings(log_root=None),
        "mimo-v2.5",
        stream=False,
    )

    assert "Claude Code internal tools" not in str(body)
    assert body["messages"][0]["content"] == "plain ActionDesign prompt"
