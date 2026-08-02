from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from .models import (
    MessageRecord,
    RequestCorrelationIdentity,
    RequestIdentity,
    ToolResultLogRecord,
)
from .redaction import redact_value, safe_conversation_id

if TYPE_CHECKING:
    from .settings import Settings

_DEFAULT_TTL_SECONDS = 300.0
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_CONVERSATION_LOCKS: dict[str, threading.RLock] = {}
_CONVERSATION_LOCKS_GUARD = threading.RLock()
_INLINE_TOOL_RESULT_MAX_CHARS = 32_768
_ARTIFACT_REF_PREFIX = "artifact:sha256:"
_MAX_CONVERSATION_ARTIFACT_BYTES = 5 * 1024 * 1024


def append_log(
    settings: Settings | Any,
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
    try:
        log_path.parent.chmod(0o700)
        log_path.chmod(0o600)
    except OSError:
        pass


def conversation_log_enabled(settings: Settings | Any) -> bool:
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


def resolve_correlation_identity(
    request: Any,
    body: Any | None = None,
    *,
    header_only: bool = False,
) -> RequestCorrelationIdentity:
    header_payload = {
        "requestId": _normalize_id(_header_value(request, "X-Request-Id")),
        "correlationId": _normalize_id(_header_value(request, "X-Correlation-Id")),
        "retryOf": _normalize_id(_header_value(request, "X-Retry-Of")),
        "parentRequestId": _normalize_id(_header_value(request, "X-Parent-Request-Id")),
    }
    missing = [
        name
        for name, value in {
            "X-Request-Id": header_payload["requestId"],
            "X-Correlation-Id": header_payload["correlationId"],
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"code": "REQUEST_CORRELATION_REQUIRED", "fields": missing},
        )
    try:
        header_identity = RequestCorrelationIdentity.model_validate(header_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "REQUEST_CORRELATION_INVALID"},
        ) from exc
    if header_only:
        return header_identity

    body_payload = {
        "requestId": _normalize_id(_value(body, "requestId", "request_id")),
        "correlationId": _normalize_id(
            _value(body, "correlationId", "correlation_id")
        ),
        "retryOf": _normalize_id(_value(body, "retryOf", "retry_of")),
        "parentRequestId": _normalize_id(
            _value(body, "parentRequestId", "parent_request_id")
        ),
    }
    missing_body = [
        name
        for name, value in {
            "requestId": body_payload["requestId"],
            "correlationId": body_payload["correlationId"],
        }.items()
        if not value
    ]
    if missing_body:
        raise HTTPException(
            status_code=422,
            detail={"code": "REQUEST_CORRELATION_REQUIRED", "fields": missing_body},
        )
    try:
        body_identity = RequestCorrelationIdentity.model_validate(body_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "REQUEST_CORRELATION_INVALID"},
        ) from exc
    mismatches = [
        alias
        for alias, attribute in (
            ("requestId", "request_id"),
            ("correlationId", "correlation_id"),
            ("retryOf", "retry_of"),
            ("parentRequestId", "parent_request_id"),
        )
        if getattr(header_identity, attribute) != getattr(body_identity, attribute)
    ]
    if mismatches:
        raise HTTPException(
            status_code=409,
            detail={"code": "REQUEST_CORRELATION_MISMATCH", "fields": mismatches},
        )
    return body_identity


def append_conversation_event(
    settings: Settings | Any,
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
            if not _conversation_log_has_limit_marker(log_path):
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(marker + "\n")
                try:
                    log_path.parent.chmod(0o700)
                    log_path.chmod(0o600)
                except OSError:
                    pass
            return
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.write("\n")
        try:
            log_path.parent.chmod(0o700)
            log_path.chmod(0o600)
        except OSError:
            pass


def store_conversation_artifact(
    settings: Settings | Any,
    conversation_id: str,
    value: Any,
) -> str:
    """Persist one redacted, content-addressed conversation artifact."""

    if not conversation_log_enabled(settings):
        return ""
    safe_id = safe_conversation_id(conversation_id)
    sanitized = redact_value(model_to_dict(value))
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
    )
    encoded = serialized.encode("utf-8")
    conversation_max_bytes = int(
        getattr(settings, "full_conversation_log_max_bytes", 100 * 1024 * 1024)
    )
    if len(encoded) > _MAX_CONVERSATION_ARTIFACT_BYTES:
        return ""
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    artifact_dir = root / "artifacts" / safe_id
    artifact_path = artifact_dir / f"{digest}.json"
    with _conversation_lock(safe_id):
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            if not artifact_path.exists():
                artifact_bytes = sum(
                    path.stat().st_size for path in artifact_dir.glob("*.json")
                )
                conversation_path = _conversation_log_path(root, safe_id)
                conversation_bytes = (
                    conversation_path.stat().st_size
                    if conversation_path.exists()
                    else 0
                )
                if conversation_bytes + artifact_bytes + len(encoded) > conversation_max_bytes:
                    return ""
                temporary_path = artifact_dir / (
                    f".{digest}.{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                with temporary_path.open("x", encoding="utf-8") as handle:
                    handle.write(serialized)
                temporary_path.replace(artifact_path)
            root.chmod(0o700)
            (root / "artifacts").chmod(0o700)
            artifact_dir.chmod(0o700)
            artifact_path.chmod(0o600)
        except OSError:
            try:
                if "temporary_path" in locals() and temporary_path.exists():
                    temporary_path.unlink()
            except OSError:
                pass
            return ""
    return _ARTIFACT_REF_PREFIX + digest


def read_conversation_artifact(
    settings: Settings | Any,
    conversation_id: str,
    artifact_ref: str,
) -> Any | None:
    """Load and verify a locally stored redacted conversation artifact."""

    if not artifact_ref.startswith(_ARTIFACT_REF_PREFIX):
        return None
    digest = artifact_ref.removeprefix(_ARTIFACT_REF_PREFIX)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    safe_id = safe_conversation_id(conversation_id)
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    artifact_path = root / "artifacts" / safe_id / f"{digest}.json"
    max_bytes = int(
        getattr(settings, "full_conversation_log_max_bytes", 100 * 1024 * 1024)
    )
    try:
        if not artifact_path.is_file() or artifact_path.stat().st_size > max_bytes:
            return None
        encoded = artifact_path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(encoded).hexdigest() != digest:
        return None
    try:
        return redact_value(json.loads(encoded.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def list_conversations(
    settings: Settings | Any,
    *,
    limit: int,
    offset: int,
    conversation_id: str = "",
    request_id: str = "",
    run_id: str = "",
    provider: str = "",
    model: str = "",
    status: str = "",
    incomplete_only: bool = False,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    if not root.is_dir():
        return [], 0
    items: list[dict[str, Any]] = []
    for path in root.glob("????????_??????_*.jsonl"):
        name = path.stem
        file_conversation_id = name[16:]
        if conversation_id and conversation_id != file_conversation_id:
            continue
        try:
            safe_id = safe_conversation_id(file_conversation_id)
            stat = path.stat()
            conversation_status = "open"
            incomplete_reason = ""
            event_count = 0
            message_count = 0
            tool_result_count = 0
            title = ""
            user_id = ""
            topic_id = ""
            first_run_id = ""
            latest_run_id = ""
            matched_request = not request_id
            matched_run = not run_id
            matched_provider = not provider
            matched_model = not model
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
                    event_type = str(event.get("type") or "")
                    event_run_id = str(event.get("runId") or "")
                    if event_run_id:
                        first_run_id = first_run_id or event_run_id
                        latest_run_id = event_run_id
                    if event_type == "gateway_request":
                        request_payload = event.get("request")
                        if isinstance(request_payload, dict):
                            user_id = user_id or str(request_payload.get("userId") or "")
                            topic_id = topic_id or str(request_payload.get("topicId") or "")
                            prompt = str(request_payload.get("prompt") or "").strip()
                            if prompt and not title:
                                title = prompt.splitlines()[0][:512]
                        if int(event.get("attempt") or 0) == 1 and not event.get("retryOf"):
                            message_count += 1
                            if isinstance(request_payload, dict) and request_payload.get("systemPrompt"):
                                message_count += 1
                    elif event_type == "gateway_response":
                        message_count += 1
                    elif event_type == "tool_result":
                        tool_result_count += 1
                        if not event.get("duplicate"):
                            message_count += 1
                    if event.get("type") == "conversation_sealed":
                        sealed_status = str(event.get("status") or "")
                        if sealed_status in {"completed", "aborted"}:
                            conversation_status = sealed_status
                        elif sealed_status:
                            incomplete_reason = "invalid_conversation_status"
                    if event.get("incompleteReason") and not incomplete_reason:
                        incomplete_reason = str(redact_value(event["incompleteReason"]))[:256]
                    matched_request = matched_request or _event_has_value(
                        event, {"requestId"}, request_id
                    )
                    matched_run = matched_run or _event_has_value(event, {"runId"}, run_id)
                    matched_provider = matched_provider or _event_has_value(
                        event, {"provider"}, provider
                    )
                    matched_model = matched_model or _event_has_value(
                        event, {"model"}, model
                    )
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
        updated_timestamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if status and conversation_status != status:
            continue
        if incomplete_only and not incomplete_reason:
            continue
        if not (matched_request and matched_run and matched_provider and matched_model):
            continue
        if updated_from is not None and updated_timestamp < updated_from.astimezone(timezone.utc):
            continue
        if updated_to is not None and updated_timestamp > updated_to.astimezone(timezone.utc):
            continue
        items.append(
            {
                "conversationId": safe_id,
                "createdAt": started_at,
                "startedAt": started_at,
                "updatedAt": updated_at,
                "title": title,
                "userId": user_id,
                "topicId": topic_id,
                "firstRunId": first_run_id,
                "latestRunId": latest_run_id,
                "sizeBytes": stat.st_size,
                "eventCount": event_count,
                "messageCount": message_count,
                "toolResultCount": tool_result_count,
                "lastSequence": event_count,
                "status": conversation_status,
                "incompleteReason": incomplete_reason,
            }
        )
    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    page = redact_value(items[offset : offset + limit])
    return page, len(items)


def seal_conversation(
    settings: Settings | Any,
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
    settings: Settings | Any,
    conversation_id: str,
    *,
    limit: int,
    offset: int,
    event_type: str = "",
    request_id: str = "",
    run_id: str = "",
    turn_status: str = "",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    safe_id = safe_conversation_id(conversation_id)
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    matches = list(root.glob(f"????????_??????_{safe_id}.jsonl"))
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    items: list[dict[str, Any]] = []
    total = 0
    try:
        with matches[0].open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    redacted = redact_value(value)
                    if not isinstance(redacted, dict):
                        continue
                    if event_type and str(redacted.get("type") or "") != event_type:
                        continue
                    if request_id and not _event_has_value(redacted, {"requestId"}, request_id):
                        continue
                    if run_id and not _event_has_value(redacted, {"runId"}, run_id):
                        continue
                    if turn_status and str(redacted.get("turnStatus") or "") != turn_status:
                        continue
                    if not _event_time_in_range(
                        redacted,
                        started_at=started_at,
                        ended_at=ended_at,
                    ):
                        continue
                    if total >= offset and len(items) < limit:
                        items.append(redacted)
                    total += 1
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CONVERSATION_LOG_UNAVAILABLE"},
        ) from exc
    return items, total


def reconstruct_conversation_transcript(
    settings: Settings | Any,
    conversation_id: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, str]:
    safe_id = safe_conversation_id(conversation_id)
    root = Path(getattr(settings, "full_conversation_log_root", ""))
    matches = list(root.glob(f"????????_??????_{safe_id}.jsonl"))
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    items: list[dict[str, Any]] = []
    total = 0
    event_sequence = 0
    message_sequence = 0
    incomplete_reason = ""
    try:
        with matches[0].open("r", encoding="utf-8") as handle:
            for line in handle:
                event_sequence += 1
                try:
                    raw_event = json.loads(line)
                except (ValueError, TypeError):
                    incomplete_reason = incomplete_reason or "invalid_jsonl_event"
                    continue
                event = redact_value(raw_event)
                if not isinstance(event, dict):
                    incomplete_reason = incomplete_reason or "invalid_jsonl_event"
                    continue
                candidates = _messages_from_event(
                    settings,
                    safe_id,
                    event,
                    event_sequence=event_sequence,
                    next_sequence=message_sequence + 1,
                )
                for candidate in candidates:
                    message_sequence += 1
                    candidate["sequence"] = message_sequence
                    try:
                        message = MessageRecord.model_validate(candidate).model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    except ValidationError:
                        incomplete_reason = incomplete_reason or "invalid_transcript_message"
                        continue
                    if total >= offset and len(items) < limit:
                        items.append(message)
                    total += 1
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CONVERSATION_LOG_UNAVAILABLE"},
        ) from exc
    return items, total, incomplete_reason


def _messages_from_event(
    settings: Settings | Any,
    conversation_id: str,
    event: dict[str, Any],
    *,
    event_sequence: int,
    next_sequence: int,
) -> list[dict[str, Any]]:
    event_type = str(event.get("type") or "")
    request_id = str(event.get("requestId") or "")
    correlation_id = str(event.get("correlationId") or "")
    run_id = str(event.get("runId") or "")
    created_at = str(event.get("time") or event.get("timestamp") or "")
    common = {
        "conversationId": conversation_id,
        "runId": run_id,
        "requestId": request_id,
        "correlationId": correlation_id,
        "createdAt": created_at,
        "toolCalls": [],
        "toolResultRefs": [],
    }
    if event_type == "gateway_request":
        if int(event.get("attempt") or 0) != 1 or event.get("retryOf"):
            return []
        request_payload = event.get("request")
        if not isinstance(request_payload, dict):
            return []
        messages: list[dict[str, Any]] = []
        system_prompt = str(request_payload.get("systemPrompt") or "")
        if system_prompt:
            messages.append(
                {
                    **common,
                    "messageId": _message_uuid(
                        conversation_id, request_id, event_sequence, "system"
                    ),
                    "role": "system",
                    "content": system_prompt,
                    "sequence": next_sequence,
                }
            )
        prompt = str(request_payload.get("prompt") or "")
        messages.append(
            {
                **common,
                "messageId": _message_uuid(
                    conversation_id, request_id, event_sequence, "user"
                ),
                "role": "user",
                "content": prompt,
                "sequence": next_sequence + len(messages),
            }
        )
        return messages
    if event_type == "gateway_response":
        response_payload = event.get("response")
        if not isinstance(response_payload, dict):
            return []
        return [
            {
                **common,
                "messageId": _message_uuid(
                    conversation_id, request_id, event_sequence, "assistant"
                ),
                "role": "assistant",
                "content": str(response_payload.get("content") or ""),
                "toolCalls": response_payload.get("tool_calls")
                or response_payload.get("toolCalls")
                or [],
                "sequence": next_sequence,
            }
        ]
    if event_type in {"tool_result", "mcp_call"} and not event.get("duplicate"):
        call_id = str(event.get("callId") or event.get("toolCallId") or "")
        artifact_ref = str(event.get("artifactRef") or "")
        inline_result = event.get("inlineResult")
        artifact_result = (
            read_conversation_artifact(settings, conversation_id, artifact_ref)
            if artifact_ref
            else None
        )
        incomplete_reason = str(event.get("incompleteReason") or "")
        if artifact_ref and artifact_result is None:
            incomplete_reason = incomplete_reason or "artifact_missing_or_invalid"
        content_value = (
            inline_result
            if inline_result is not None
            else artifact_result
            if artifact_result is not None
            else event.get("error") or event.get("resultMetadata") or {}
        )
        content = (
            content_value
            if isinstance(content_value, str)
            else json.dumps(content_value, ensure_ascii=False, default=str)
        )
        return [
            {
                **common,
                "messageId": _message_uuid(
                    conversation_id, request_id, event_sequence, "tool"
                ),
                "role": "tool",
                "content": content,
                "toolResultRefs": [artifact_ref or f"tool:{call_id}"],
                "incompleteReason": incomplete_reason,
                "sequence": next_sequence,
            }
        ]
    return []


def _message_uuid(
    conversation_id: str,
    request_id: str,
    event_sequence: int,
    role: str,
) -> str:
    digest = bytearray(
        hashlib.sha256(
            f"{conversation_id}:{request_id}:{event_sequence}:{role}".encode()
        ).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(digest)))


def _event_has_value(value: Any, keys: set[str], expected: str) -> bool:
    if not expected:
        return True
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in keys and str(item) == expected:
                return True
            if _event_has_value(item, keys, expected):
                return True
    elif isinstance(value, list):
        return any(_event_has_value(item, keys, expected) for item in value)
    return False


def _event_time_in_range(
    event: Mapping[str, Any],
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> bool:
    if started_at is None and ended_at is None:
        return True
    value = event.get("time") or event.get("timestamp")
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        return False
    timestamp = timestamp.astimezone(timezone.utc)
    if started_at is not None and timestamp < started_at.astimezone(timezone.utc):
        return False
    if ended_at is not None and timestamp > ended_at.astimezone(timezone.utc):
        return False
    return True


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


def _conversation_log_has_limit_marker(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    try:
        with log_path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - 4096))
            tail = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return False
    return '"type": "conversation_log_limit_reached"' in tail


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
        self._lock = threading.RLock()

    def store(
        self,
        settings: Settings | Any,
        conversation_id: str,
        payload: Any,
        *,
        request_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            result = model_to_dict(payload)
            run_id = result.get("runId")
            tool_call_id = result.get("toolCallId")
            if not isinstance(run_id, str) or not isinstance(tool_call_id, str):
                raise ValueError("Tool result payload requires runId and toolCallId")

            safe_id = safe_conversation_id(conversation_id)
            key = (safe_id, run_id, tool_call_id)
            duplicate = key in self._results or _persisted_tool_result_exists(
                settings,
                safe_id,
                run_id,
                tool_call_id,
            )
            stored = _tool_result_log_record(
                settings,
                safe_id,
                result,
                request_id=request_id,
                duplicate=duplicate,
            )
            if duplicate:
                self._results[key] = now
                return True, stored
            self._results[key] = now
            append_log(settings, safe_id, "tool-results.jsonl", stored)
            return False, stored

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, created_at in self._results.items()
            if now - created_at >= self._ttl_seconds
        ]
        for key in expired:
            del self._results[key]


def _persisted_tool_result_exists(
    settings: Settings | Any,
    conversation_id: str,
    run_id: str,
    tool_call_id: str,
) -> bool:
    log_root = getattr(settings, "log_root", None)
    if not log_root:
        return False
    log_path = Path(log_root) / conversation_id / "tool-results.jsonl"
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("runId") == run_id
                    and item.get("toolCallId") == tool_call_id
                ):
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return False


def _tool_result_log_record(
    settings: Settings | Any,
    conversation_id: str,
    payload: dict[str, Any],
    *,
    request_id: str,
    duplicate: bool,
) -> dict[str, Any]:
    raw_result = payload.get("result")
    raw_serialized = json.dumps(
        raw_result,
        ensure_ascii=False,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
    )
    redacted_result = redact_value(raw_result)
    sanitized_serialized = json.dumps(
        redacted_result,
        ensure_ascii=False,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
    )
    encoded = raw_serialized.encode("utf-8")
    inline = (
        len(raw_serialized) <= _INLINE_TOOL_RESULT_MAX_CHARS
        and len(sanitized_serialized) <= _INLINE_TOOL_RESULT_MAX_CHARS
    )
    metadata: dict[str, Any] = {
        "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "characters": len(raw_serialized),
        "bytes": len(encoded),
        "kind": type(redacted_result).__name__,
        "inline": inline,
    }
    if isinstance(redacted_result, dict):
        metadata["keys"] = sorted(str(key) for key in redacted_result)[:256]
    elif isinstance(redacted_result, list):
        metadata["items"] = len(redacted_result)
    inline_result = redacted_result if inline else None
    artifact_ref = ""
    if inline_result is None:
        artifact_ref = store_conversation_artifact(
            settings,
            conversation_id,
            redacted_result,
        )
        metadata["artifactStored"] = bool(artifact_ref)
        metadata["sanitizedSha256"] = (
            artifact_ref.removeprefix("artifact:") if artifact_ref else ""
        )
        if not artifact_ref:
            metadata["incompleteReason"] = "artifact_store_unavailable"
    call_id = str(payload.get("callId") or payload.get("toolCallId") or "")
    record = ToolResultLogRecord.model_validate(
        {
            "conversationId": conversation_id,
            "runId": payload.get("runId"),
            "requestId": request_id,
            "correlationId": payload.get("correlationId"),
            "parentRequestId": payload.get("parentRequestId") or "",
            "retryOf": payload.get("retryOf") or "",
            "topicId": payload.get("topicId") or "",
            "checkpointId": payload.get("checkpointId") or "",
            "protocolVersion": payload.get("protocolVersion") or "1.0",
            "graphVersion": payload.get("graphVersion") or "",
            "skillCatalogVersion": payload.get("skillCatalogVersion") or "",
            "skillCatalogHash": payload.get("skillCatalogHash") or "",
            "loadedSkillVersions": payload.get("loadedSkillVersions") or {},
            "mcpServerIds": payload.get("mcpServerIds") or [],
            "toolCallId": payload.get("toolCallId"),
            "callId": call_id,
            "idempotencyKey": payload.get("idempotencyKey") or call_id,
            "toolName": payload.get("toolName"),
            "status": payload.get("status"),
            "traceId": payload.get("traceId") or "",
            "turn": payload.get("turn") or 0,
            "duplicate": duplicate,
            "success": payload.get("status") == "success",
            "durationMs": payload.get("durationMs") or 0,
            "sanitizedInput": redact_value(payload.get("arguments") or {}),
            "sanitizedOutput": inline_result if inline_result is not None else metadata,
            "resultMetadata": metadata,
            "artifactRef": artifact_ref,
            "sourceArtifactRef": payload.get("artifactRef") or "",
            "inlineResult": inline_result,
            "error": redact_value(str(payload.get("error") or "")),
            "createdAt": payload.get("timestamp") or _iso_time(),
        }
    ).model_dump(mode="json", by_alias=True)
    return record
