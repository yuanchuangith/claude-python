from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .redaction import redact_value, safe_conversation_id

if TYPE_CHECKING:
    from .settings import Settings

_DEFAULT_TTL_SECONDS = 300.0


def append_log(
    settings: "Settings | Any",
    conversation_id: str,
    filename: str,
    payload: Any,
) -> None:
    log_root = getattr(settings, "log_root", None)
    if not log_root:
        return

    safe_id = safe_conversation_id(conversation_id)
    log_path = Path(log_root) / safe_id / Path(filename).name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_payload = redact_value(model_to_dict(payload))
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_payload, ensure_ascii=False, default=str))
        handle.write("\n")


def model_to_dict(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_to_dict(model_dump(mode="json", by_alias=True))
        except TypeError:
            return model_to_dict(model_dump())

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        try:
            return model_to_dict(dict_method(by_alias=True))
        except TypeError:
            return model_to_dict(dict_method())
    if isinstance(value, Mapping):
        return {key: model_to_dict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [model_to_dict(item) for item in value]
    if isinstance(value, tuple):
        return tuple(model_to_dict(item) for item in value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            key: model_to_dict(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


class ToolResultStore:
    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._results: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    def store(
        self,
        settings: "Settings | Any",
        conversation_id: str,
        payload: Any,
    ) -> bool:
        now = self._clock()
        self._prune(now)
        result = model_to_dict(payload)
        run_id = result.get("runId")
        tool_call_id = result.get("toolCallId")
        if not isinstance(run_id, str) or not isinstance(tool_call_id, str):
            raise ValueError("Tool result payload requires runId and toolCallId")

        safe_id = safe_conversation_id(conversation_id)
        key = (safe_id, run_id, tool_call_id)
        if key in self._results:
            return True

        stored = copy.deepcopy(result)
        stored["conversationId"] = safe_id
        self._results[key] = (now, stored)
        append_log(settings, safe_id, "tool-results.jsonl", stored)
        return False

    def get(self, conversation_id: str, run_id: str) -> list[dict[str, Any]]:
        now = self._clock()
        self._prune(now)
        safe_id = safe_conversation_id(conversation_id)
        return [
            copy.deepcopy(result)
            for (
                stored_conversation_id,
                stored_run_id,
                _,
            ), (_, result) in self._results.items()
            if stored_conversation_id == safe_id and stored_run_id == run_id
        ]

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, (created_at, _) in self._results.items()
            if now - created_at >= self._ttl_seconds
        ]
        for key in expired:
            del self._results[key]
