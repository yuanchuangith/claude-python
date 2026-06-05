from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .embedding_client import EmbeddingClient


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_MANIFEST_NAME = "manifest.json"
_CHUNKS_NAME = "chunks.jsonl"


class KnowledgePathError(ValueError):
    code = "KNOWLEDGE_PATH_FORBIDDEN"


@dataclass
class KnowledgeChunk:
    id: str
    path: str
    category: str
    title: str
    heading: str
    content: str
    mtime: float
    size: int
    embedding: list[float]


class MarkdownKnowledgeIndex:
    def __init__(
        self,
        *,
        root: str | Path,
        index_dir: str | Path,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.root = Path(root)
        self.index_dir = Path(index_dir)
        self.embedding_client = embedding_client
        self.chunks: list[KnowledgeChunk] = []

    def build_or_load(self) -> list[KnowledgeChunk]:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._read_manifest()
        previous_chunks = self._read_chunks()
        previous_by_path: dict[str, list[KnowledgeChunk]] = {}
        for chunk in previous_chunks:
            previous_by_path.setdefault(chunk.path, []).append(chunk)

        next_files: dict[str, dict[str, float | int]] = {}
        next_chunks: list[KnowledgeChunk] = []
        for file_path in sorted(self.root.glob("**/*.md")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.root).as_posix()
            stat = file_path.stat()
            file_meta = {"mtime": stat.st_mtime, "size": stat.st_size}
            next_files[relative] = file_meta
            if (
                manifest.get("files", {}).get(relative) == file_meta
                and relative in previous_by_path
            ):
                next_chunks.extend(previous_by_path.get(relative, []))
                continue

            parsed = self._parse_markdown_file(file_path, relative, stat)
            self._embed_chunks(parsed)
            next_chunks.extend(parsed)

        self.chunks = next_chunks
        self._write_manifest({"files": next_files})
        self._write_chunks(next_chunks)
        return list(self.chunks)

    def search(
        self,
        query: str,
        *,
        top_k: int,
        max_chars_per_item: int = 4000,
        max_context_chars: int = 12000,
    ) -> list[dict[str, Any]]:
        chunks = self.build_or_load()
        if not chunks:
            return []

        vector_results = self._vector_search(query, chunks)
        ranked = (
            vector_results
            if vector_results
            else self._keyword_search(query, chunks)
        )

        results: list[dict[str, Any]] = []
        used_chars = 0
        for chunk, score in ranked:
            if len(results) >= top_k or used_chars >= max_context_chars:
                break
            snippet = _trim(chunk.content, max_chars_per_item)
            used_chars += len(snippet)
            results.append(
                {
                    "path": chunk.path,
                    "category": chunk.category,
                    "title": chunk.title,
                    "heading": chunk.heading,
                    "score": score,
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
        safe_path = self._safe_relative_path(path)
        chunks = self.build_or_load()
        matching = [chunk for chunk in chunks if chunk.path == safe_path]
        if heading:
            matching = [
                chunk
                for chunk in matching
                if chunk.heading.casefold() == heading.casefold()
            ]
        if matching:
            content = "\n\n".join(chunk.content for chunk in matching)
            first = matching[0]
            return {
                "path": first.path,
                "category": first.category,
                "title": first.title,
                "heading": first.heading if heading else "",
                "content": _trim(content, max_chars),
            }

        file_path = self.root / safe_path
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return {
            "path": safe_path,
            "category": _category(safe_path),
            "title": _title_from_content(content, file_path.stem),
            "heading": "",
            "content": _trim(content, max_chars),
        }

    def _vector_search(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
    ) -> list[tuple[KnowledgeChunk, float]]:
        if self.embedding_client is None:
            return []
        try:
            query_embedding = self.embedding_client.embed_query(query)
        except Exception:
            return []
        if not query_embedding:
            return []

        scored = [
            (chunk, _cosine_similarity(query_embedding, chunk.embedding))
            for chunk in chunks
            if chunk.embedding
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def _keyword_search(
        self,
        query: str,
        chunks: list[KnowledgeChunk],
    ) -> list[tuple[KnowledgeChunk, float]]:
        terms = [term for term in re.split(r"\W+", query.casefold()) if term]
        if not terms:
            return [(chunk, 0.0) for chunk in chunks]
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk in chunks:
            text = f"{chunk.heading}\n{chunk.content}".casefold()
            score = float(sum(text.count(term) for term in terms))
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def _parse_markdown_file(
        self,
        file_path: Path,
        relative: str,
        stat: Any,
    ) -> list[KnowledgeChunk]:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        title = _title_from_content(content, file_path.stem)
        category = _category(relative)
        headings = list(_HEADING_RE.finditer(content))
        if not headings:
            return [
                self._chunk(
                    relative=relative,
                    category=category,
                    title=title,
                    heading=title,
                    content=content,
                    stat=stat,
                    index=0,
                )
            ]

        chunks: list[KnowledgeChunk] = []
        for index, match in enumerate(headings):
            end = (
                headings[index + 1].start()
                if index + 1 < len(headings)
                else len(content)
            )
            heading = match.group(2).strip()
            chunk_content = content[match.start() : end].strip()
            chunks.append(
                self._chunk(
                    relative=relative,
                    category=category,
                    title=title,
                    heading=heading,
                    content=chunk_content,
                    stat=stat,
                    index=index,
                )
            )
        return chunks

    def _chunk(
        self,
        *,
        relative: str,
        category: str,
        title: str,
        heading: str,
        content: str,
        stat: Any,
        index: int,
    ) -> KnowledgeChunk:
        chunk_id = hashlib.sha1(
            f"{relative}\n{index}\n{heading}".encode("utf-8")
        ).hexdigest()
        return KnowledgeChunk(
            id=chunk_id,
            path=relative,
            category=category,
            title=title,
            heading=heading,
            content=content,
            mtime=stat.st_mtime,
            size=stat.st_size,
            embedding=[],
        )

    def _embed_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if self.embedding_client is None or not chunks:
            return
        try:
            embeddings = self.embedding_client.embed_documents(
                [chunk.content for chunk in chunks]
            )
        except Exception:
            return
        for chunk, embedding in zip(chunks, embeddings):
            chunk.embedding = [float(value) for value in embedding]

    def _safe_relative_path(self, path: str) -> str:
        if "\0" in path:
            raise KnowledgePathError("Invalid knowledge path")
        candidate = (self.root / path).resolve()
        root = self.root.resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise KnowledgePathError("Knowledge path escapes root") from exc
        if candidate.suffix.lower() != ".md":
            raise KnowledgePathError("Knowledge path must be a Markdown file")
        return relative.as_posix()

    def _read_manifest(self) -> dict[str, Any]:
        path = self.index_dir / _MANIFEST_NAME
        if not path.exists():
            return {"files": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"files": {}}
        return payload if isinstance(payload, dict) else {"files": {}}

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        (self.index_dir / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_chunks(self) -> list[KnowledgeChunk]:
        path = self.index_dir / _CHUNKS_NAME
        if not path.exists():
            return []
        chunks: list[KnowledgeChunk] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                chunks.append(KnowledgeChunk(**payload))
            except (TypeError, json.JSONDecodeError):
                continue
        return chunks

    def _write_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        path = self.index_dir / _CHUNKS_NAME
        lines = [json.dumps(asdict(chunk), ensure_ascii=False) for chunk in chunks]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _title_from_content(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _category(relative: str) -> str:
    parts = relative.split("/")
    return parts[0] if len(parts) > 1 else ""


def _trim(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(value * value for value in left[:length]))
    right_norm = math.sqrt(sum(value * value for value in right[:length]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
