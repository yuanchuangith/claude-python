from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import ValidationError

from .models import RequestIdentity
from .redaction import redact_value, safe_conversation_id

if TYPE_CHECKING:
    from .settings import Settings

_DEFAULT_TTL_SECONDS = 300.0
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_CONVERSATION_LOCKS: dict[str, threading.RLock] = {}
_CONVERSATION_LOCKS_GUARD = threading.RLock()


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


def conversation_log_enabled(settings: "Settings | Any") -> bool:
    return bool(getattr(settings, "full_conversation_log_enabled", False))


def resolve_request_identity(request: Any, body: Any) -> tuple[str, str]:
    header_payload = {
        "conversationId": _normalize_id(_header_value(request, "X-Conversation-Id")),
        "runId": _normalize_id(_header_value(request, "X-Agent-Run-Id")),
    }
    body_payload = {
        "conversationId": _normalize_id(_value(body, "conversationId", "conversation_id")),
        "runId": _normalize_id(_value(body, "runId", "run_id")),
    }
    missing = [
        field
        for field, value in {
            "X-Conversation-Id": header_payload["conversationId"],
            "X-Agent-Run-Id": header_payload["runId"],
            "conversationId": body_payload["conversationId"],
            "runId": body_payload["runId"],
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REQUEST_IDENTITY_REQUIRED",
                "fields": missing,
            },
        )
    try:
        header_identity = RequestIdentity.model_validate(header_payload)
        body_identity = RequestIdentity.model_validate(body_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "REQUEST_IDENTITY_INVALID"},
        ) from exc
    mismatches = []
    if header_identity.conversation_id != body_identity.conversation_id:
        mismatches.append("conversationId")
    if header_identity.run_id != body_identity.run_id:
        mismatches.append("runId")
    if mismatches:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REQUEST_IDENTITY_MISMATCH",
                "fields": mismatches,
            },
        )
    return safe_conversation_id(body_identity.conversation_id), body_identity.run_id


def append_conversation_event(
    settings: "Settings | Any",
    conversation_id: str,
    event: Any,
) -> None:
    if not conversation_log_enabled(settings):
        return

    safe_id = safe_conversation_id(conversation_id)
    root = Path(
        getattr(
            settings,
            "full_conversation_log_root",
            Path("logs/actiondesign-agent"),
        )
    )
    log_path = _conversation_log_path(root, safe_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Conversation events are a persistent boundary.  Callers may already
    # sanitize selected fields, but the writer must remain the final,
    # fail-safe redaction layer so new event types cannot accidentally persist
    # provider keys, credentials, connection details, or encoded image data.
    payload = redact_value(model_to_dict(event))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload.setdefault("conversationId", safe_id)
    payload.setdefault("time", _iso_time())
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    max_bytes = int(
        getattr(settings, "full_conversation_log_max_bytes", 100 * 1024 * 1024)
    )
    with _conversation_lock(safe_id):
        current_bytes = log_path.stat().st_size if log_path.exists() else 0
        incoming_bytes = len(serialized.encode("utf-8")) + 1
        if current_bytes + incoming_bytes > max_bytes:
            marker = json.dumps(
                {
                    "type": "conversation_log_limit_reached",
                    "conversationId": safe_id,
                    "time": _iso_time(),
                    "maxBytes": max_bytes,
                    "incompleteReason": "conversation_log_size_limit",
                },
                ensure_ascii=False,
            )
            if current_bytes + len(marker.encode("utf-8")) + 1 <= max_bytes:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(marker + "\n")
            return
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.write("\n")
        try:
            log_path.parent.chmod(0o700)
            log_path.chmod(0o600)
        except OSError:
            pass


def list_conversations(
    settings: "Settings | Any",
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    if not root.is_dir():
        return [], 0
    items: list[dict[str, Any]] = []
    for path in root.glob("????????_??????_*.jsonl"):
        name = path.stem
        conversation_id = name[16:]
        try:
            safe_id = safe_conversation_id(conversation_id)
            stat = path.stat()
            status = "open"
            incomplete_reason = ""
            event_count = 0
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    event_count += 1
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        incomplete_reason = "invalid_jsonl_event"
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "conversation_sealed":
                        status = str(event.get("status") or "open")
                    if event.get("incompleteReason"):
                        incomplete_reason = str(event["incompleteReason"])
        except (OSError, HTTPException):
            continue
        started_at = ""
        updated_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = json.loads(handle.readline())
                if isinstance(first, dict):
                    started_at = str(first.get("time") or "")
        except (OSError, ValueError, TypeError):
            pass
        items.append(
            {
                "conversationId": safe_id,
                "startedAt": started_at,
                "updatedAt": updated_at,
                "sizeBytes": stat.st_size,
                "eventCount": event_count,
                "status": status,
                "incompleteReason": incomplete_reason,
            }
        )
    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    return items[offset : offset + limit], len(items)


def seal_conversation(
    settings: "Settings | Any",
    conversation_id: str,
    status: str,
) -> dict[str, Any]:
    if status not in {"completed", "aborted"}:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CONVERSATION_STATUS"})
    safe_id = safe_conversation_id(conversation_id)
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    if not list(root.glob(f"????????_??????_{safe_id}.jsonl")):
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    append_conversation_event(
        settings,
        safe_id,
        {"type": "conversation_sealed", "status": status},
    )
    return {"success": True, "status": status}


def read_conversation(
    settings: "Settings | Any",
    conversation_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    safe_id = safe_conversation_id(conversation_id)
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    matches = list(root.glob(f"????????_??????_{safe_id}.jsonl"))
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    items: list[dict[str, Any]] = []
    try:
        with matches[0].open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    items.append(value)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CONVERSATION_LOG_UNAVAILABLE"},
        ) from exc
    return items[offset : offset + limit], len(items)


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


def _header_value(request: Any, name: str) -> str:
    headers = getattr(request, "headers", None)
    if not headers:
        return ""
    value = headers.get(name) if hasattr(headers, "get") else None
    return str(value or "")


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _normalize_id(value: Any) -> str:
    return str(value or "").strip()


def _iso_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conversation_log_path(root: Path, safe_id: str) -> Path:
    matches = sorted(root.glob(f"????????_??????_{safe_id}.jsonl"))
    if matches:
        return matches[0]
    return root / f"{_china_log_timestamp()}_{safe_id}.jsonl"


def _conversation_lock(safe_id: str) -> threading.RLock:
    with _CONVERSATION_LOCKS_GUARD:
        return _CONVERSATION_LOCKS.setdefault(safe_id, threading.RLock())


def _china_log_timestamp() -> str:
    return datetime.now(_CHINA_TIMEZONE).strftime("%Y%m%d_%H%M%S")


class ToolResultStore:
    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._results: dict[tuple[str, str, str], float] = {}

    def store(
        self,
        settings: "Settings | Any",
        conversation_id: str,
        payload: Any,
        *,
        request_id: str,
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

        stored = dict(result)
        stored["conversationId"] = safe_id
        stored["requestId"] = request_id
        self._results[key] = now
        append_log(settings, safe_id, "tool-results.jsonl", stored)
        return False

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, created_at in self._results.items()
            if now - created_at >= self._ttl_seconds
        ]
        for key in expired:
            del self._results[key]
