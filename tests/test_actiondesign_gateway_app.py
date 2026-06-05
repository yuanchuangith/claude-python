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


def test_models_without_mimo_key_marks_mimo_unavailable(tmp_path):
    client = make_client(log_root=tmp_path)

    response = client.get("/api/actiondesign-agent/models")

    assert response.status_code == 200
    body = response.json()
    assert body["defaultProvider"] == "mimo"
    assert body["providers"]["mimo"]["status"] == "unavailable"
    assert "GXP_MIMO_API_KEY" in body["providers"]["mimo"]["error"]


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
            "prompt": "hello",
            "toolNames": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["code"] == "MIMO_RESPONSE_INCOMPLETE"


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
