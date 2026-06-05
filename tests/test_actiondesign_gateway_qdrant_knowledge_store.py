from types import SimpleNamespace
from uuid import UUID

from claude_agent_sdk.actiondesign_gateway.qdrant_knowledge_store import (
    QdrantKnowledgeStore,
)


class FakeEmbeddingClient:
    def __init__(self):
        self.documents = []
        self.queries = []

    def embed_documents(self, texts):
        self.documents.append(list(texts))
        return [[1.0, 0.0], [0.0, 1.0]][: len(texts)]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


class FakeQdrantHttpClient:
    def __init__(self):
        self.requests = []

    def delete(self, path):
        self.requests.append(("DELETE", path, None))
        return FakeResponse({})

    def put(self, path, json=None):
        self.requests.append(("PUT", path, json))
        return FakeResponse({})

    def post(self, path, json=None):
        self.requests.append(("POST", path, json))
        if path.endswith("/points/search"):
            return FakeResponse(
                {
                    "result": [
                        {
                            "score": 0.9,
                            "payload": {
                                "path": "components/actions.md",
                                "category": "components",
                                "title": "Actions",
                                "heading": "NullCondition",
                                "content": "NullCondition validates required fields.",
                            },
                        }
                    ]
                }
            )
        if path.endswith("/points/scroll"):
            return FakeResponse(
                {
                    "result": {
                        "points": [
                            {
                                "payload": {
                                    "path": "components/actions.md",
                                    "category": "components",
                                    "title": "Actions",
                                    "heading": "NullCondition",
                                    "content": "NullCondition validates required fields.",
                                }
                            }
                        ]
                    }
                }
            )
        return FakeResponse({})


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def make_store():
    http = FakeQdrantHttpClient()
    embedding = FakeEmbeddingClient()
    settings = SimpleNamespace(
        qdrant_url="http://qdrant:6333",
        qdrant_api_key="secret",
        qdrant_collection="actiondesign_knowledge",
        embedding_model="BAAI/bge-m3",
        embedding_dimensions=2,
        knowledge_recall_limit=20,
    )
    return (
        QdrantKnowledgeStore(
            settings=settings,
            embedding_client=embedding,
            http_client=http,
        ),
        embedding,
        http,
    )


def test_qdrant_store_replaces_collection_with_embedded_markdown_chunks():
    store, embedding, http = make_store()

    result = store.replace_documents(
        [
            {
                "path": "components/actions.md",
                "content": "# Actions\n\n## NullCondition\nRequired fields.",
            }
        ]
    )

    assert result == {
        "files": 1,
        "chunks": 2,
        "collection": "actiondesign_knowledge",
    }
    assert embedding.documents == [["# Actions", "## NullCondition\nRequired fields."]]
    assert ("DELETE", "/collections/actiondesign_knowledge", None) in http.requests
    create = http.requests[1]
    assert create == (
        "PUT",
        "/collections/actiondesign_knowledge",
        {"vectors": {"size": 2, "distance": "Cosine"}},
    )
    upsert = http.requests[2]
    assert upsert[0:2] == ("PUT", "/collections/actiondesign_knowledge/points")
    UUID(upsert[2]["points"][0]["id"])
    assert upsert[2]["points"][0]["payload"]["embedding_model"] == "BAAI/bge-m3"


def test_qdrant_store_search_and_read_use_qdrant_payloads():
    store, embedding, http = make_store()

    results = store.search(
        "required validation",
        limit=1,
        max_chars_per_item=4000,
        max_context_chars=12000,
    )
    item = store.read(
        path="components/actions.md",
        heading="NullCondition",
        max_chars=200,
    )

    assert embedding.queries == ["required validation"]
    assert results[0]["heading"] == "NullCondition"
    assert item["content"] == "NullCondition validates required fields."
    assert http.requests[-1][0:2] == (
        "POST",
        "/collections/actiondesign_knowledge/points/scroll",
    )
