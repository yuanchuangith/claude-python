from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from .embedding_client import EmbeddingClient, embedding_client_from_settings
from .knowledge_vector_index import KnowledgePathError

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_EMBEDDING_BATCH_SIZE = 64
_UPSERT_BATCH_SIZE = 64


@dataclass
class KnowledgeDocument:
    path: str
    content: str


@dataclass
class QdrantKnowledgeChunk:
    id: str
    path: str
    category: str
    title: str
    heading: str
    chunk_index: int
    chunk_hash: str
    embedding_model: str
    content: str
    mtime: float
    size: int
    vector: list[float]


class QdrantKnowledgeStore:
    def __init__(
        self,
        *,
        settings: Any,
        embedding_client: EmbeddingClient | None = None,
        http_client: Any = None,
    ) -> None:
        self.settings = settings
        self.collection = str(
            _setting(settings, "qdrant_collection", default="actiondesign_knowledge")
            or "actiondesign_knowledge"
        )
        self.vector_size = int(_setting(settings, "embedding_dimensions", default=1024))
        self.embedding_model = str(_setting(settings, "embedding_model", default=""))
        self.embedding_client = embedding_client or embedding_client_from_settings(
            settings
        )
        self.http = http_client or _qdrant_http_client(settings)

    def replace_documents(self, documents: list[dict[str, str]]) -> dict[str, Any]:
        parsed = [
            KnowledgeDocument(
                path=_safe_upload_path(document["path"]),
                content=str(document["content"]),
            )
            for document in documents
        ]
        chunks = self._build_chunks(parsed)
        self._recreate_collection()
        if chunks:
            self._upsert_chunks(chunks)
        return {
            "files": len(parsed),
            "chunks": len(chunks),
            "collection": self.collection,
        }

    def search(
        self,
        query: str,
        *,
        limit: int,
        max_chars_per_item: int = 4000,
        max_context_chars: int = 12000,
    ) -> list[dict[str, Any]]:
        if self.embedding_client is None:
            raise RuntimeError("Embedding client is not configured")
        vector = self.embedding_client.embed_query(query)
        recall_limit = max(
            int(_setting(self.settings, "knowledge_recall_limit", default=20)),
            limit,
        )
        response = self.http.post(
            f"/collections/{self.collection}/points/search",
            json={
                "vector": vector,
                "limit": recall_limit,
                "with_payload": True,
            },
        )
        response.raise_for_status()
        points = response.json().get("result", [])

        results: list[dict[str, Any]] = []
        used_chars = 0
        for point in points:
            if len(results) >= limit or used_chars >= max_context_chars:
                break
            payload = point.get("payload", {}) if isinstance(point, dict) else {}
            content = str(payload.get("content") or "")
            snippet = _trim(content, max_chars_per_item)
            used_chars += len(snippet)
            results.append(
                {
                    "path": str(payload.get("path") or ""),
                    "category": str(payload.get("category") or ""),
                    "title": str(payload.get("title") or ""),
                    "heading": str(payload.get("heading") or ""),
                    "score": float(point.get("score") or 0.0),
                    "snippet": snippet,
                }
            )
        return results

    def read(
        self,
        *,
        path: str,
        heading: str = "",
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        safe_path = _safe_upload_path(path)
        must = [{"key": "path", "match": {"value": safe_path}}]
        if heading:
            must.append({"key": "heading", "match": {"value": heading}})
        response = self.http.post(
            f"/collections/{self.collection}/points/scroll",
            json={
                "filter": {"must": must},
                "limit": 100,
                "with_payload": True,
                "with_vector": False,
            },
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        points = result.get("points", []) if isinstance(result, dict) else []
        payloads = [
            point.get("payload", {})
            for point in points
            if isinstance(point, dict) and isinstance(point.get("payload"), dict)
        ]
        if not payloads:
            raise FileNotFoundError(path)

        payloads.sort(key=lambda payload: int(payload.get("chunk_index") or 0))
        content = "\n\n".join(str(payload.get("content") or "") for payload in payloads)
        first = payloads[0]
        return {
            "path": safe_path,
            "category": str(first.get("category") or ""),
            "title": str(first.get("title") or ""),
            "heading": heading or "",
            "content": _trim(content, max_chars),
        }

    def _build_chunks(
        self,
        documents: list[KnowledgeDocument],
    ) -> list[QdrantKnowledgeChunk]:
        raw_chunks: list[dict[str, Any]] = []
        for document in documents:
            raw_chunks.extend(_split_markdown(document))
        if not raw_chunks:
            return []
        if self.embedding_client is None:
            raise RuntimeError("Embedding client is not configured")
        embeddings: list[list[float]] = []
        for batch in _batched(raw_chunks, _EMBEDDING_BATCH_SIZE):
            embeddings.extend(
                self.embedding_client.embed_documents(
                    [chunk["content"] for chunk in batch]
                )
            )
        chunks: list[QdrantKnowledgeChunk] = []
        for chunk, vector in zip(raw_chunks, embeddings, strict=False):
            chunk_hash = hashlib.sha1(chunk["content"].encode()).hexdigest()
            chunk_id = str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        f"actiondesign-knowledge:{chunk['path']}:"
                        f"{chunk['chunk_index']}:{chunk['heading']}:{chunk_hash}"
                    ),
                )
            )
            chunks.append(
                QdrantKnowledgeChunk(
                    id=chunk_id,
                    path=chunk["path"],
                    category=_category(chunk["path"]),
                    title=chunk["title"],
                    heading=chunk["heading"],
                    chunk_index=chunk["chunk_index"],
                    chunk_hash=chunk_hash,
                    embedding_model=self.embedding_model,
                    content=chunk["content"],
                    mtime=time.time(),
                    size=len(chunk["content"].encode("utf-8")),
                    vector=[float(value) for value in vector],
                )
            )
        return chunks

    def _recreate_collection(self) -> None:
        delete_response = self.http.delete(f"/collections/{self.collection}")
        if getattr(delete_response, "status_code", 200) not in {200, 202, 404}:
            delete_response.raise_for_status()
        create_response = self.http.put(
            f"/collections/{self.collection}",
            json={"vectors": {"size": self.vector_size, "distance": "Cosine"}},
        )
        create_response.raise_for_status()

    def _upsert_chunks(self, chunks: list[QdrantKnowledgeChunk]) -> None:
        for batch in _batched(chunks, _UPSERT_BATCH_SIZE):
            points = [
                {
                    "id": chunk.id,
                    "vector": chunk.vector,
                    "payload": {
                        "id": chunk.id,
                        "path": chunk.path,
                        "category": chunk.category,
                        "title": chunk.title,
                        "heading": chunk.heading,
                        "chunk_index": chunk.chunk_index,
                        "chunk_hash": chunk.chunk_hash,
                        "embedding_model": chunk.embedding_model,
                        "content": chunk.content,
                        "mtime": chunk.mtime,
                        "size": chunk.size,
                    },
                }
                for chunk in batch
            ]
            response = self.http.put(
                f"/collections/{self.collection}/points",
                json={"points": points},
            )
            response.raise_for_status()


def qdrant_knowledge_store_from_settings(settings: Any) -> QdrantKnowledgeStore | None:
    injected = _setting(settings, "knowledge_store", default=None)
    if injected is not None:
        return injected
    qdrant_url = str(_setting(settings, "qdrant_url", default="") or "")
    if not qdrant_url:
        return None
    return QdrantKnowledgeStore(settings=settings)


def _qdrant_http_client(settings: Any) -> httpx.Client:
    qdrant_url = str(_setting(settings, "qdrant_url", default="") or "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    api_key = str(_setting(settings, "qdrant_api_key", default="") or "")
    if api_key:
        headers["api-key"] = api_key
    return httpx.Client(base_url=qdrant_url, headers=headers, timeout=60.0)


def _split_markdown(document: KnowledgeDocument) -> list[dict[str, Any]]:
    title = _title_from_content(document.content, PurePosixPath(document.path).stem)
    headings = list(_HEADING_RE.finditer(document.content))
    if not headings:
        return [
            {
                "path": document.path,
                "title": title,
                "heading": title,
                "chunk_index": 0,
                "content": document.content.strip(),
            }
        ]

    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(document.content)
        )
        chunks.append(
            {
                "path": document.path,
                "title": title,
                "heading": match.group(2).strip(),
                "chunk_index": index,
                "content": document.content[match.start() : end].strip(),
            }
        )
    return [chunk for chunk in chunks if chunk["content"]]


def _safe_upload_path(path: str) -> str:
    if "\0" in path:
        raise KnowledgePathError("Invalid knowledge path")
    normalized = str(path).replace("\\", "/").lstrip("/")
    pure_path = PurePosixPath(normalized)
    if (
        not normalized
        or pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or pure_path.suffix.lower() != ".md"
    ):
        raise KnowledgePathError("Knowledge path must be a safe Markdown path")
    return pure_path.as_posix()


def _title_from_content(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _category(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else ""


def _trim(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _setting(settings: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(settings, dict) and name in settings:
        return settings[name]
    if hasattr(settings, name):
        return getattr(settings, name)
    return default
