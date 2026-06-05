from __future__ import annotations

from pathlib import Path
from typing import Any

from .backend_tools import BackendToolCall, BackendToolResult, NoopBackendToolExecutor
from .embedding_client import embedding_client_from_settings
from .knowledge_vector_index import KnowledgePathError, MarkdownKnowledgeIndex
from .qdrant_knowledge_store import qdrant_knowledge_store_from_settings


class ActionDesignBackendToolExecutor:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._fallback = NoopBackendToolExecutor()

    async def execute(self, call: BackendToolCall) -> BackendToolResult:
        if call.name == "knowledge.search":
            return self._knowledge_search(call.arguments)
        if call.name == "knowledge.read":
            return self._knowledge_read(call.arguments)
        return await self._fallback.execute(call)

    def _knowledge_search(self, arguments: dict[str, Any]) -> BackendToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return BackendToolResult(
                name="knowledge.search",
                status="failed",
                error="knowledge.search requires query",
                code="KNOWLEDGE_QUERY_REQUIRED",
            )

        index = self._index()
        store = qdrant_knowledge_store_from_settings(self.settings)
        if store is None and index is None:
            return _not_configured("knowledge.search")

        max_results = _int_argument(
            arguments,
            "limit",
            default=_int_setting(self.settings, "knowledge_max_results", 4),
        )
        max_chars_per_item = _int_argument(
            arguments,
            "maxCharsPerItem",
            "max_chars_per_item",
            default=_int_setting(self.settings, "knowledge_max_chars_per_item", 4000),
        )
        max_context_chars = _int_argument(
            arguments,
            "maxContextChars",
            "max_context_chars",
            default=_int_setting(self.settings, "knowledge_max_context_chars", 12000),
        )

        if store is not None:
            results = store.search(
                query,
                limit=max_results,
                max_chars_per_item=max_chars_per_item,
                max_context_chars=max_context_chars,
            )
        else:
            results = index.search(
                query,
                top_k=max_results,
                max_chars_per_item=max_chars_per_item,
                max_context_chars=max_context_chars,
            )
        return BackendToolResult(
            name="knowledge.search",
            status="success",
            result={"query": query, "results": results},
        )

    def _knowledge_read(self, arguments: dict[str, Any]) -> BackendToolResult:
        path = str(arguments.get("path") or "").strip()
        if not path:
            return BackendToolResult(
                name="knowledge.read",
                status="failed",
                error="knowledge.read requires path",
                code="KNOWLEDGE_PATH_REQUIRED",
            )

        index = self._index()
        store = qdrant_knowledge_store_from_settings(self.settings)
        if store is None and index is None:
            return _not_configured("knowledge.read")

        try:
            max_chars = _int_argument(
                arguments,
                "maxChars",
                "max_chars",
                default=_int_setting(
                    self.settings,
                    "knowledge_max_chars_per_item",
                    4000,
                ),
            )
            if store is not None:
                result = store.read(
                    path=path,
                    heading=str(arguments.get("heading") or "").strip(),
                    max_chars=max_chars,
                )
            else:
                result = index.read(
                    path=path,
                    heading=str(arguments.get("heading") or "").strip(),
                    max_chars=max_chars,
                )
        except KnowledgePathError as exc:
            return BackendToolResult(
                name="knowledge.read",
                status="failed",
                error=str(exc),
                code=exc.code,
            )
        except FileNotFoundError:
            return BackendToolResult(
                name="knowledge.read",
                status="failed",
                error=f"Knowledge file not found: {path}",
                code="KNOWLEDGE_NOT_FOUND",
            )

        return BackendToolResult(
            name="knowledge.read",
            status="success",
            result=result,
        )

    def _index(self) -> MarkdownKnowledgeIndex | None:
        return knowledge_index_from_settings(self.settings)


def warm_knowledge_index(settings: Any) -> None:
    index = knowledge_index_from_settings(settings)
    if index is not None:
        index.build_or_load()


def knowledge_index_from_settings(settings: Any) -> MarkdownKnowledgeIndex | None:
    root = _path_setting(settings, "knowledge_root")
    if root is None or not root.exists():
        return None
    index_dir = _path_setting(
        settings,
        "knowledge_index_dir",
        default=Path("debug_logs/actiondesign-agent/knowledge-index"),
    )
    embedding_client = getattr(settings, "embedding_client", None)
    if embedding_client is None:
        embedding_client = embedding_client_from_settings(settings)
    return MarkdownKnowledgeIndex(
        root=root,
        index_dir=index_dir,
        embedding_client=embedding_client,
    )


def _not_configured(name: str) -> BackendToolResult:
    return BackendToolResult(
        name=name,
        status="failed",
        error="Knowledge root is not configured or does not exist",
        code="KNOWLEDGE_ROOT_NOT_CONFIGURED",
    )


def _path_setting(
    settings: Any,
    name: str,
    *,
    default: Path | None = None,
) -> Path | None:
    value = _setting(settings, name, default=default)
    if value is None or str(value).strip() == "":
        return None
    return Path(value)


def _int_setting(settings: Any, name: str, default: int) -> int:
    return int(_setting(settings, name, default=default))


def _int_argument(arguments: dict[str, Any], *names: str, default: int) -> int:
    for name in names:
        value = arguments.get(name)
        if value is not None and str(value).strip() != "":
            return int(value)
    return default


def _setting(settings: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(settings, dict) and name in settings:
        return settings[name]
    if hasattr(settings, name):
        return getattr(settings, name)
    return default
