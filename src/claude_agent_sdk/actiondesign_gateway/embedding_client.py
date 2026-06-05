from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import httpx


class EmbeddingClient(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document chunks."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()

        data = payload.get("data", [])
        if not isinstance(data, list):
            return []
        ordered = sorted(
            (item for item in data if isinstance(item, dict)),
            key=lambda item: int(item.get("index", 0)),
        )
        return [
            [float(value) for value in item.get("embedding", [])]
            for item in ordered
        ]

    def embed_query(self, text: str) -> list[float]:
        embeddings = self.embed_documents([text])
        return embeddings[0] if embeddings else []


def embedding_client_from_settings(settings: Any) -> EmbeddingClient | None:
    provider = str(
        _setting(settings, "embedding_provider", default="openai-compatible")
        or "openai-compatible"
    ).lower()
    if provider != "openai-compatible":
        return None

    base_url = str(_setting(settings, "embedding_base_url", default="") or "")
    model = str(_setting(settings, "embedding_model", default="") or "")
    if not base_url or not model:
        return None

    return OpenAICompatibleEmbeddingClient(
        base_url=base_url,
        model=model,
        api_key=str(_setting(settings, "embedding_api_key", default="") or ""),
        timeout_seconds=float(
            _setting(settings, "embedding_timeout_seconds", default=60.0)
        ),
    )


def _setting(obj: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(obj, dict) and name in obj:
        return obj[name]
    if hasattr(obj, name):
        return getattr(obj, name)
    return default
