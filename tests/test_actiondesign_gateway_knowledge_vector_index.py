from types import SimpleNamespace

import anyio

from claude_agent_sdk.actiondesign_gateway.actiondesign_backend_executor import (
    ActionDesignBackendToolExecutor,
)
from claude_agent_sdk.actiondesign_gateway.backend_tools import BackendToolCall
from claude_agent_sdk.actiondesign_gateway.knowledge_vector_index import (
    MarkdownKnowledgeIndex,
)


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts):
        self.document_calls += 1
        return [self._embedding(text) for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._embedding(text)

    def _embedding(self, text):
        text = text.lower()
        if "required" in text or "validation" in text or "nullcondition" in text:
            return [1.0, 0.0, 0.0]
        if "dialog" in text or "message" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class FailingEmbeddingClient:
    def embed_documents(self, texts):
        raise RuntimeError("embedding service down")

    def embed_query(self, text):
        raise RuntimeError("embedding service down")


def write_fixture_markdown(root):
    component_dir = root / "components"
    component_dir.mkdir(parents=True)
    (component_dir / "actions.md").write_text(
        """# Action Components

## NullCondition

NullCondition validates required form fields before submit.

## OpenMessageDialog

OpenMessageDialog shows a message dialog with title and content.
""",
        encoding="utf-8",
    )


def test_knowledge_vector_index_indexes_fixture_markdown(tmp_path):
    root = tmp_path / "knowledge"
    index_dir = tmp_path / "index"
    write_fixture_markdown(root)

    index = MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=FakeEmbeddingClient(),
    )

    chunks = index.build_or_load()

    assert [chunk.heading for chunk in chunks] == [
        "Action Components",
        "NullCondition",
        "OpenMessageDialog",
    ]
    assert chunks[1].path == "components/actions.md"
    assert chunks[1].category == "components"
    assert chunks[1].embedding == [1.0, 0.0, 0.0]


def test_knowledge_search_uses_semantic_query_vector(tmp_path):
    root = tmp_path / "knowledge"
    index_dir = tmp_path / "index"
    embedding_client = FakeEmbeddingClient()
    write_fixture_markdown(root)

    index = MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=embedding_client,
    )
    index.build_or_load()

    results = index.search("required validation", top_k=1)

    assert embedding_client.query_calls == 1
    assert results[0]["heading"] == "NullCondition"
    assert "required form fields" in results[0]["snippet"]


def test_knowledge_index_reuses_embeddings_when_files_unchanged(tmp_path):
    root = tmp_path / "knowledge"
    index_dir = tmp_path / "index"
    write_fixture_markdown(root)

    first_client = FakeEmbeddingClient()
    first_index = MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=first_client,
    )
    first_index.build_or_load()

    second_client = FakeEmbeddingClient()
    second_index = MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=second_client,
    )
    chunks = second_index.build_or_load()

    assert first_client.document_calls == 1
    assert second_client.document_calls == 0
    assert len(chunks) == 3


def test_knowledge_search_falls_back_to_keyword_when_embedding_fails(tmp_path):
    root = tmp_path / "knowledge"
    index_dir = tmp_path / "index"
    write_fixture_markdown(root)

    index = MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=FailingEmbeddingClient(),
    )

    results = index.search("message dialog", top_k=1)

    assert results[0]["heading"] == "OpenMessageDialog"


def test_knowledge_read_rejects_path_traversal(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    settings = SimpleNamespace(
        knowledge_root=root,
        knowledge_index_dir=tmp_path / "index",
        embedding_client=FakeEmbeddingClient(),
        knowledge_max_results=4,
        knowledge_max_chars_per_item=4000,
        knowledge_max_context_chars=12000,
    )
    executor = ActionDesignBackendToolExecutor(settings)

    result = anyio.run(
        executor.execute,
        BackendToolCall(
            name="knowledge.read",
            arguments={"path": "../secret.md"},
        ),
    )

    assert result.status == "failed"
    assert result.code == "KNOWLEDGE_PATH_FORBIDDEN"
