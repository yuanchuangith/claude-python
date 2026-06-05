from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from claude_agent_sdk.actiondesign_gateway import mimo_provider
from claude_agent_sdk.actiondesign_gateway.backend_tools import (
    BackendToolCall,
    BackendToolResult,
    execute_backend_tool_calls,
)


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


def test_mimo_stream_hides_backend_tool_protocol(monkeypatch):
    responses = [
        '[BACKEND_TOOL_CALL] skill.search({"query":"validation"})',
        '完成\n[TOOL_CALL] preview_code({"targetAction":"main"})',
    ]

    async def fake_post_mimo(body, _settings):
        return {"content": [{"type": "text", "text": responses.pop(0)}]}

    monkeypatch.setattr(mimo_provider, "_post_mimo", fake_post_mimo)

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
