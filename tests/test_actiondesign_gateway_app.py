import json
from datetime import datetime, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


def make_client(**settings_overrides):
    mimo_api_key = settings_overrides.pop("mimo_api_key", "")
    settings = Settings(
        default_provider="mimo",
        mimo_api_key=mimo_api_key,
        log_root=settings_overrides.pop("log_root", None),
        **settings_overrides,
    )
    return TestClient(create_app(settings=settings))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def full_log_path(root, conversation_id):
    matches = sorted(root.glob(f"????????_??????_{conversation_id}.jsonl"))
    assert len(matches) == 1
    return matches[0]


def freeze_conversation_log_time(monkeypatch):
    from claude_agent_sdk.actiondesign_gateway import session_log

    frozen_utc = datetime(2026, 6, 10, 10, 30, 45, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_utc.replace(tzinfo=None)
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(session_log, "datetime", FrozenDateTime)


def test_models_without_mimo_key_marks_mimo_unavailable(tmp_path):
    client = make_client(log_root=tmp_path)

    response = client.get("/api/actiondesign-agent/models")

    assert response.status_code == 200
    body = response.json()
    assert body["defaultProvider"] == "mimo"
    assert body["providers"]["mimo"]["status"] == "unavailable"
    assert "GXP_MIMO_API_KEY" in body["providers"]["mimo"]["error"]
    claude_code = body["providers"]["claude-code"]
    assert claude_code["defaultModel"] == "mimo-v2.5"
    assert [model["id"] for model in claude_code["models"]] == [
        "mimo-v2.5",
        "mimo-v2.5-pro",
    ]
    assert all(
        model["provider"] == "claude-code" for model in claude_code["models"]
    )


def test_models_can_override_claude_code_models(tmp_path):
    client = make_client(
        log_root=tmp_path,
        claude_code_models=[
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "mimo-custom",
        ],
    )

    response = client.get("/api/actiondesign-agent/models")

    assert response.status_code == 200
    claude_code = response.json()["providers"]["claude-code"]
    assert claude_code["defaultModel"] == "mimo-v2.5"
    assert [model["id"] for model in claude_code["models"]] == [
        "mimo-v2.5",
        "mimo-v2.5-pro",
        "mimo-custom",
    ]


def test_models_include_configured_claude_code_default_model(tmp_path):
    client = make_client(
        log_root=tmp_path,
        claude_code_default_model="mimo-custom",
    )

    response = client.get("/api/actiondesign-agent/models")

    assert response.status_code == 200
    claude_code = response.json()["providers"]["claude-code"]
    assert claude_code["defaultModel"] == "mimo-custom"
    assert [model["id"] for model in claude_code["models"]][:2] == [
        "mimo-custom",
        "mimo-v2.5",
    ]


def test_tool_result_is_idempotent_by_conversation_run_and_call(tmp_path):
    client = make_client(log_root=tmp_path)
    payload = {
        "conversationId": "conv_test",
        "runId": "run_1",
        "toolCallId": "tc_1",
        "toolName": "preview_code",
        "arguments": {"targetAction": "main"},
        "status": "success",
        "result": {"ok": True},
    }

    first = client.post("/api/actiondesign-agent/tool-result", json=payload)
    second = client.post("/api/actiondesign-agent/tool-result", json=payload)
    results = client.get("/api/actiondesign-agent/tool-results/conv_test/run_1")

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert results.status_code == 200
    assert len(results.json()["results"]) == 1


def test_full_conversation_log_disabled_does_not_create_jsonl(tmp_path, monkeypatch):
    async def fake_call_mimo(req, settings):
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "ok",
            "tool_calls": [],
            "success": True,
            "error": None,
            "duration_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    full_log_root = tmp_path / "full"
    client = make_client(
        log_root=tmp_path / "legacy",
        mimo_api_key="secret",
        full_conversation_log_enabled=False,
        full_conversation_log_root=full_log_root,
    )

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "conversationId": "conv_disabled",
            "runId": "run_1",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert list(full_log_root.glob("*.jsonl")) == []


def test_full_conversation_log_chat_writes_request_and_response(
    tmp_path, monkeypatch
):
    freeze_conversation_log_time(monkeypatch)

    async def fake_call_mimo(req, settings):
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "mimo ok",
            "tool_calls": [{"name": "preview_code", "arguments": {}}],
            "success": True,
            "error": None,
            "duration_ms": 12,
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    full_log_root = tmp_path / "full"
    client = make_client(
        log_root=tmp_path / "legacy",
        mimo_api_key="secret",
        full_conversation_log_enabled=True,
        full_conversation_log_root=full_log_root,
    )

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        headers={"X-Conversation-Id": "conv_header"},
        json={
            "provider": "mimo",
            "conversationId": "conv_body",
            "runId": "run_1",
            "prompt": "hello raw prompt",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Conversation-Id"] == "conv_header"
    assert response.headers["X-Run-Id"] == "run_1"
    log_path = full_log_root / "20260610_183045_conv_header.jsonl"
    events = read_jsonl(log_path)
    assert [event["type"] for event in events] == ["request", "response"]
    assert events[0]["conversationId"] == "conv_header"
    assert events[0]["runId"] == "run_1"
    assert events[0]["provider"] == "mimo"
    assert events[0]["model"] == "mimo-v2.5"
    assert events[0]["userPrompt"] == "hello raw prompt"
    assert events[0]["requestBody"]["conversationId"] == "conv_header"
    assert events[0]["requestBody"]["model"] == "mimo-v2.5"
    assert events[1]["content"] == "mimo ok"
    assert events[1]["toolCalls"] == [{"name": "preview_code", "arguments": {}}]


def test_full_conversation_log_claude_code_chat_writes_request_and_response(
    tmp_path, monkeypatch
):
    async def fake_call_claude_code(req, settings):
        return {
            "provider": "claude-code",
            "model": req.model,
            "content": "claude ok",
            "tool_calls": [],
            "success": True,
            "error": None,
            "duration_ms": 21,
            "usage": {"total_cost_usd": 0.01},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_claude_code",
        fake_call_claude_code,
    )
    full_log_root = tmp_path / "full"
    client = make_client(
        log_root=tmp_path / "legacy",
        full_conversation_log_enabled=True,
        full_conversation_log_root=full_log_root,
    )

    response = client.post(
        "/api/actiondesign-agent/claude-code/chat",
        json={
            "provider": "mimo",
            "conversationId": "conv_claude_log",
            "runId": "run_claude",
            "prompt": "claude raw prompt",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Conversation-Id"] == "conv_claude_log"
    assert response.headers["X-Run-Id"] == "run_claude"
    events = read_jsonl(full_log_path(full_log_root, "conv_claude_log"))
    assert [event["type"] for event in events] == ["request", "response"]
    assert events[0]["provider"] == "claude-code"
    assert events[0]["model"] == "mimo-v2.5"
    assert events[0]["userPrompt"] == "claude raw prompt"
    assert events[1]["provider"] == "claude-code"
    assert events[1]["model"] == "mimo-v2.5"
    assert events[1]["content"] == "claude ok"


def test_full_conversation_log_requires_run_id_when_enabled(tmp_path, monkeypatch):
    async def fake_call_mimo(req, settings):
        raise AssertionError("provider should not be called without runId")

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    client = make_client(
        log_root=tmp_path / "legacy",
        mimo_api_key="secret",
        full_conversation_log_enabled=True,
        full_conversation_log_root=tmp_path / "full",
    )

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "conversationId": "conv_missing_run",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "RUN_ID_REQUIRED"


def test_full_conversation_log_generates_conversation_id(tmp_path, monkeypatch):
    freeze_conversation_log_time(monkeypatch)

    async def fake_call_mimo(req, settings):
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "ok",
            "tool_calls": [],
            "success": True,
            "error": None,
            "duration_ms": 1,
            "usage": {},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    full_log_root = tmp_path / "full"
    client = make_client(
        log_root=tmp_path / "legacy",
        mimo_api_key="secret",
        full_conversation_log_enabled=True,
        full_conversation_log_root=full_log_root,
    )

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "runId": "run_generated",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    conversation_id = response.headers["X-Conversation-Id"]
    assert conversation_id.startswith("conv_server_")
    assert (full_log_root / f"20260610_183045_{conversation_id}.jsonl").exists()


def test_full_conversation_log_stream_writes_request_and_response(
    tmp_path, monkeypatch
):
    freeze_conversation_log_time(monkeypatch)

    async def fake_stream_mimo(req, settings):
        yield 'data: {"type":"text_delta","content":"ok"}\n\n'
        yield 'data: {"type":"message_complete","content":"ok","tool_calls":[],"success":true,"provider":"mimo","model":"mimo-v2.5","usage":{}}\n\n'

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.stream_mimo",
        fake_stream_mimo,
    )
    full_log_root = tmp_path / "full"
    client = make_client(
        log_root=tmp_path / "legacy",
        mimo_api_key="secret",
        full_conversation_log_enabled=True,
        full_conversation_log_root=full_log_root,
    )

    response = client.post(
        "/api/actiondesign-agent/mimo/chat/stream",
        json={
            "provider": "mimo",
            "conversationId": "conv_stream_log",
            "runId": "run_stream",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    events = read_jsonl(full_log_root / "20260610_183045_conv_stream_log.jsonl")
    assert [event["type"] for event in events] == ["request", "response"]
    assert events[0]["model"] == "mimo-v2.5"
    assert events[1]["content"] == "ok"


def test_full_conversation_log_tool_result_appends_same_file(tmp_path, monkeypatch):
    freeze_conversation_log_time(monkeypatch)
    full_log_root = tmp_path / "full"
    existing_log_path = full_log_root / "20260609_093000_conv_tool.jsonl"
    existing_log_path.parent.mkdir(parents=True)
    existing_log_path.write_text(
        json.dumps(
            {
                "type": "request",
                "conversationId": "conv_tool",
                "runId": "run_1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = make_client(
        log_root=tmp_path / "legacy",
        full_conversation_log_enabled=True,
        full_conversation_log_root=full_log_root,
    )
    payload = {
        "conversationId": "conv_tool",
        "runId": "run_1",
        "toolCallId": "tc_1",
        "toolName": "create_node",
        "arguments": {"elementKey": "ExitAction"},
        "status": "success",
        "result": {"ok": True},
    }

    response = client.post("/api/actiondesign-agent/tool-result", json=payload)

    assert response.status_code == 200
    assert sorted(path.name for path in full_log_root.glob("*.jsonl")) == [
        "20260609_093000_conv_tool.jsonl"
    ]
    events = read_jsonl(existing_log_path)
    assert [event["type"] for event in events] == ["request", "tool_result"]
    assert events[1]["conversationId"] == "conv_tool"
    assert events[1]["runId"] == "run_1"
    assert events[1]["toolName"] == "create_node"
    assert events[1]["result"] == {"ok": True}


def test_chat_rejects_unsafe_conversation_id_before_provider_call(tmp_path):
    client = make_client(log_root=tmp_path)

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "conversationId": "../bad",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CONVERSATION_ID"


def test_mimo_v25_pro_rejects_images_before_upstream_call(tmp_path):
    client = make_client(log_root=tmp_path, mimo_api_key="secret")

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "model": "mimo-v2.5-pro",
            "conversationId": "conv_img",
            "runId": "run_img",
            "prompt": "look",
            "toolNames": [],
            "images": [{"media_type": "image/png", "data": "abc"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MODEL_DOES_NOT_SUPPORT_IMAGES"
    assert response.json()["detail"]["fallbackModel"] == "mimo-v2.5"


def test_mimo_chat_route_forces_mimo_provider_with_consistent_response(
    tmp_path, monkeypatch
):
    async def fake_call_mimo(req, settings):
        return {
            "provider": "mimo",
            "model": req.model or "mimo-v2.5",
            "content": "mimo ok",
            "tool_calls": [{"name": "preview_code", "arguments": {}}],
            "success": True,
            "error": None,
            "duration_ms": 12,
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    client = make_client(log_root=tmp_path, mimo_api_key="secret")

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "claude-code",
            "conversationId": "conv_mimo",
            "runId": "run_mimo",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "mimo",
        "model": "mimo-v2.5",
        "content": "mimo ok",
        "tool_calls": [{"name": "preview_code", "arguments": {}}],
        "success": True,
        "error": None,
        "duration_ms": 12,
        "usage": {"input_tokens": 1},
        "code": None,
    }


def test_mimo_chat_route_preserves_structured_error_code(tmp_path, monkeypatch):
    async def fake_call_mimo(req, settings):
        return {
            "provider": "mimo",
            "model": "mimo-v2.5",
            "content": "",
            "tool_calls": [],
            "success": False,
            "error": "MiMo stopped before completion because max_tokens was reached",
            "code": "MIMO_RESPONSE_INCOMPLETE",
            "duration_ms": 12,
            "usage": {"input_tokens": 1},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo",
        fake_call_mimo,
    )
    client = make_client(log_root=tmp_path, mimo_api_key="secret")

    response = client.post(
        "/api/actiondesign-agent/mimo/chat",
        json={
            "provider": "mimo",
            "conversationId": "conv_mimo_code",
            "runId": "run_mimo_code",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["code"] == "MIMO_RESPONSE_INCOMPLETE"


def test_mimo_review_route_returns_structured_review_without_tool_calls(
    tmp_path, monkeypatch
):
    async def fake_call_mimo_review(req, settings):
        return {
            "provider": "mimo",
            "model": req.model,
            "pass": False,
            "summary": "裸 return 会绕过提交阻止语义",
            "issues": [
                {
                    "severity": "error",
                    "code": "BARE_RETURN",
                    "message": "BeforeSubmit 阻止类校验必须明确 return false",
                    "actionKey": "beforeSubmit",
                    "nodeKey": "exit_1",
                }
            ],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 9,
            "usage": {"input_tokens": 1},
            "raw": '{"pass":false}',
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_mimo_review",
        fake_call_mimo_review,
    )
    client = make_client(log_root=tmp_path, mimo_api_key="secret")

    response = client.post(
        "/api/actiondesign-agent/mimo/review",
        json={
            "provider": "claude-code",
            "conversationId": "conv_review",
            "runId": "run_review",
            "prompt": "review generated code",
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Conversation-Id"] == "conv_review"
    assert response.headers["X-Run-Id"] == "run_review"
    assert response.json()["provider"] == "mimo"
    assert response.json()["model"] == "mimo-v2.5"
    assert response.json()["pass"] is False
    assert response.json()["issues"][0]["code"] == "BARE_RETURN"
    assert "tool_calls" not in response.json()


def test_claude_code_review_route_forces_claude_provider(tmp_path, monkeypatch):
    async def fake_call_claude_code_review(req, settings):
        return {
            "provider": "claude-code",
            "model": req.model,
            "pass": True,
            "summary": "review passed",
            "issues": [],
            "success": True,
            "error": None,
            "code": None,
            "duration_ms": 7,
            "usage": {"total_cost_usd": 0.01},
            "raw": '{"pass":true}',
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_claude_code_review",
        fake_call_claude_code_review,
    )
    client = make_client(log_root=tmp_path)

    response = client.post(
        "/api/actiondesign-agent/claude-code/review",
        json={
            "provider": "mimo",
            "conversationId": "conv_claude_review",
            "runId": "run_claude_review",
            "prompt": "review generated code",
        },
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "claude-code"
    assert response.json()["model"] == "mimo-v2.5"
    assert response.json()["pass"] is True


def test_claude_code_chat_route_forces_claude_provider_with_same_shape(
    tmp_path, monkeypatch
):
    async def fake_call_claude_code(req, settings):
        return {
            "provider": "claude-code",
            "model": req.model,
            "content": "claude ok",
            "tool_calls": [],
            "success": True,
            "error": None,
            "duration_ms": 34,
            "usage": {},
        }

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.call_claude_code",
        fake_call_claude_code,
    )
    client = make_client(log_root=tmp_path)

    response = client.post(
        "/api/actiondesign-agent/claude-code/chat",
        json={
            "provider": "mimo",
            "conversationId": "conv_claude",
            "runId": "run_claude",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "provider",
        "model",
        "content",
        "tool_calls",
        "success",
        "error",
        "duration_ms",
        "usage",
        "code",
    }
    assert body["provider"] == "claude-code"
    assert body["code"] is None
    assert body["tool_calls"] == []


def test_provider_specific_stream_routes_keep_message_complete_shape(
    tmp_path, monkeypatch
):
    async def fake_stream_mimo(req, settings):
        yield 'data: {"type":"message_complete","content":"ok","tool_calls":[],"success":true}\n\n'

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.stream_mimo",
        fake_stream_mimo,
    )
    client = make_client(log_root=tmp_path, mimo_api_key="secret")

    response = client.post(
        "/api/actiondesign-agent/mimo/chat/stream",
        json={
            "provider": "claude-code",
            "conversationId": "conv_stream",
            "runId": "run_stream",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert '"message_complete"' in response.text
    assert '"tool_calls":[]' in response.text


def test_claude_code_stream_route_keeps_message_complete_shape(
    tmp_path, monkeypatch
):
    async def fake_stream_claude_code(req, settings):
        yield 'data: {"type":"message_complete","content":"ok","tool_calls":[],"success":true}\n\n'

    monkeypatch.setattr(
        "claude_agent_sdk.actiondesign_gateway.app.stream_claude_code",
        fake_stream_claude_code,
    )
    client = make_client(log_root=tmp_path)

    response = client.post(
        "/api/actiondesign-agent/claude-code/chat/stream",
        json={
            "provider": "mimo",
            "conversationId": "conv_claude_stream",
            "runId": "run_claude_stream",
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert '"message_complete"' in response.text
    assert '"tool_calls":[]' in response.text


def test_generic_chat_routes_are_not_registered(tmp_path):
    client = make_client(log_root=tmp_path)
    payload = {
        "provider": "mimo",
        "conversationId": "conv_generic",
        "prompt": "hello",
        "toolNames": [],
    }

    chat = client.post("/api/actiondesign-agent/chat", json=payload)
    stream = client.post("/api/actiondesign-agent/chat/stream", json=payload)

    assert chat.status_code == 404
    assert stream.status_code == 404


class FakeKnowledgeStore:
    def __init__(self):
        self.replaced = None

    def replace_documents(self, documents):
        self.replaced = documents
        return {"files": len(documents), "chunks": 2, "collection": "test_knowledge"}

    def search(self, query, *, limit, max_chars_per_item, max_context_chars):
        return [
            {
                "path": "components/actions.md",
                "category": "components",
                "title": "Actions",
                "heading": "NullCondition",
                "score": 0.91,
                "snippet": f"matched {query}",
            }
        ][:limit]

    def read(self, *, path, heading="", max_chars=4000):
        return {
            "path": path,
            "category": "components",
            "title": "Actions",
            "heading": heading,
            "content": "NullCondition validates required fields.",
        }


def test_knowledge_upload_requires_admin_token(tmp_path):
    client = make_client(
        log_root=tmp_path,
        knowledge_admin_token="admin-secret",
        knowledge_store=FakeKnowledgeStore(),
    )

    response = client.post(
        "/api/actiondesign-agent/knowledge/upload",
        files=[("files", ("actions.md", b"# Actions\n", "text/markdown"))],
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "KNOWLEDGE_ADMIN_AUTH_REQUIRED"


def test_knowledge_upload_replaces_documents_with_admin_token(tmp_path):
    store = FakeKnowledgeStore()
    client = make_client(
        log_root=tmp_path,
        knowledge_admin_token="admin-secret",
        knowledge_store=store,
    )

    response = client.post(
        "/api/actiondesign-agent/knowledge/upload",
        headers={"Authorization": "Bearer admin-secret"},
        files=[
            (
                "files",
                (
                    "components/actions.md",
                    b"# Actions\n\n## NullCondition\nRequired fields.",
                    "text/markdown",
                ),
            )
        ],
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "files": 1,
        "chunks": 2,
        "collection": "test_knowledge",
    }
    assert store.replaced == [
        {
            "path": "components/actions.md",
            "content": "# Actions\n\n## NullCondition\nRequired fields.",
        }
    ]


def test_knowledge_upload_accepts_explicit_paths_and_gb18030_content(tmp_path):
    store = FakeKnowledgeStore()
    client = make_client(
        log_root=tmp_path,
        knowledge_admin_token="admin-secret",
        knowledge_store=store,
    )

    response = client.post(
        "/api/actiondesign-agent/knowledge/upload",
        headers={"Authorization": "Bearer admin-secret"},
        files=[
            (
                "files",
                (
                    "file0.md",
                    "# 组件概要\n\n## 分类\n表单组件".encode("gb18030"),
                    "text/markdown",
                ),
            ),
            ("paths", (None, "概览/组件概要.md")),
        ],
    )

    assert response.status_code == 200
    assert store.replaced == [
        {
            "path": "概览/组件概要.md",
            "content": "# 组件概要\n\n## 分类\n表单组件",
        }
    ]


def test_public_knowledge_search_and_read_are_unauthenticated(tmp_path):
    client = make_client(log_root=tmp_path, knowledge_store=FakeKnowledgeStore())

    search = client.post(
        "/api/actiondesign-agent/knowledge/search",
        json={"query": "required validation", "limit": 1},
    )
    read = client.post(
        "/api/actiondesign-agent/knowledge/read",
        json={
            "path": "components/actions.md",
            "heading": "NullCondition",
            "maxChars": 200,
        },
    )

    assert search.status_code == 200
    assert search.json()["results"][0]["heading"] == "NullCondition"
    assert read.status_code == 200
    assert read.json()["content"] == "NullCondition validates required fields."
