from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AgentChatRequest, RequestLogEntry
from .profile_store import ProviderProfile
from .redaction import redact_value
from .session_log import model_to_dict

DEFAULT_REQUEST_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_REQUEST_LOG_BACKUP_COUNT = 5
_READ_CHUNK_BYTES = 64 * 1024
_MAX_LOG_LINE_BYTES = 64 * 1024
_MAX_USAGE_TOKENS = 1_000_000_000_000
_MAX_ERROR_MESSAGE_CHARS = 1_000
_KNOWN_ERROR_CODES = frozenset(
    {
        "GATEWAY_ERROR",
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_CONTEXT_TOO_LARGE",
        "PROVIDER_HTTP_ERROR",
        "PROVIDER_MODEL_UNAVAILABLE",
        "PROVIDER_NETWORK_ERROR",
        "PROVIDER_OUTPUT_TRUNCATED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_RESPONSE_EMPTY",
        "PROVIDER_RESPONSE_ERROR",
        "PROVIDER_RESPONSE_INVALID",
        "PROVIDER_STREAM_ERROR",
        "PROVIDER_STREAM_INVALID",
        "PROVIDER_TIMEOUT",
        "PROVIDER_TOOL_CALL_INVALID",
        "CLIENT_STREAM_CANCELLED",
    }
)
_TOKEN_USAGE_FIELDS = frozenset(
    {
        "accepted_prediction_tokens",
        "audio_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "completion_tokens",
        "input_cached_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
        "total_tokens",
    }
)
_FINISH_REASON_FIELDS = frozenset({"finish_reason", "stop_reason"})
_KNOWN_FINISH_REASONS = frozenset(
    {
        "cancelled",
        "content_filter",
        "end_turn",
        "error",
        "function_call",
        "length",
        "max_tokens",
        "output_truncated",
        "stop",
        "stop_sequence",
        "tool_calls",
        "tool_use",
    }
)


class RequestLogStore:
    """Append-only, metadata-only provider request log."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_REQUEST_LOG_MAX_BYTES,
        backup_count: int = DEFAULT_REQUEST_LOG_BACKUP_COUNT,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("request log max_bytes must be positive")
        if backup_count < 1:
            raise ValueError("request log backup_count must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()

    def record(
        self,
        *,
        request_id: str,
        request: AgentChatRequest,
        profile: ProviderProfile,
        started: float,
        status: str,
        response: dict[str, Any] | None = None,
        error: Any = None,
        http_status: int = 200,
        stream: bool,
        attempt: int = 1,
        endpoint: str = "",
    ) -> dict[str, Any]:
        response_data = model_to_dict(response or {})
        usage = _sanitize_usage(response_data.get("usage"))
        error_code, error_message = _sanitize_error(error)
        content = str(response_data.get("content") or "")
        tool_calls = response_data.get("tool_calls") or []
        response_tool_names = [
            str(call.get("name") or "")
            for call in tool_calls
            if isinstance(call, dict) and call.get("name")
        ]
        prompt = request.prompt
        system_prompt = request.system_prompt
        stop_reason = str(
            usage.get("stop_reason") or usage.get("finish_reason") or ""
        )
        request_metadata = redact_value(
            {
                "agentMode": request.agent_mode,
                "topicId": request.topic_id,
                "checkpointId": request.checkpoint_id,
                "graphVersion": request.graph_version,
                "protocolVersion": request.protocol_version,
                "promptManifestVersion": request.prompt_manifest_version,
                "promptManifestHash": request.prompt_manifest_hash,
                "toolCatalogVersion": request.tool_catalog_version,
                "toolCatalogHash": request.tool_catalog_hash,
                "skillCatalogVersion": request.skill_catalog_version,
                "skillCatalogHash": request.skill_catalog_hash,
                "loadedSkillIds": sorted(request.loaded_skill_versions),
                "mcpServerIds": request.mcp_server_ids,
                "maxTokens": request.max_tokens,
                "thinking": request.thinking.type,
                "toolCount": len(request.tools),
                "imageCount": len(request.images),
            }
        )
        response_metadata = redact_value(
            {
                "success": response_data.get("success", status == "success"),
                "contentSha256": _sha256(content),
                "toolCallCount": len(tool_calls),
                "toolCallIds": [
                    str(call.get("id") or "")
                    for call in tool_calls
                    if isinstance(call, dict) and call.get("id")
                ],
                "toolNames": response_tool_names,
            }
        )
        entry = RequestLogEntry.model_validate(
            {
                "requestId": request_id,
                "correlationId": request.correlation_id,
                "parentRequestId": request.parent_request_id,
                "retryOf": request.retry_of,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "conversationId": request.conversation_id,
                "runId": request.run_id,
                "traceId": request.trace_id,
                "endpoint": endpoint,
                "provider": profile.provider,
                "profileId": profile.id,
                "protocol": profile.protocol,
                "model": str(response_data.get("model") or request.model or profile.default_model),
                "attempt": attempt,
                "stream": stream,
                "status": status,
                "durationMs": max(0, int((time.monotonic() - started) * 1_000)),
                "httpStatus": http_status,
                "errorCode": error_code,
                "errorMessage": error_message,
                "promptCharacters": len(prompt),
                "promptSha256": _sha256(prompt),
                "systemPromptCharacters": len(system_prompt),
                "systemPromptSha256": _sha256(system_prompt),
                "imageCount": len(request.images),
                "toolNames": [tool.name for tool in request.tools],
                "responseToolNames": response_tool_names,
                "inputCharacters": len(prompt) + len(system_prompt),
                "outputCharacters": len(content),
                "finishReason": str(
                    usage.get("finish_reason") or usage.get("stop_reason") or ""
                ),
                "stopReason": stop_reason,
                "usage": usage,
                "requestMetadata": request_metadata,
                "responseMetadata": response_metadata,
            }
        ).model_dump(mode="json", by_alias=True)
        serialized = json.dumps(
            entry,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        encoded_size = len(serialized.encode("utf-8")) + 1
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            self._rotate_if_needed(encoded_size)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.write("\n")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return entry

    def list(
        self,
        *,
        limit: int,
        offset: int,
        provider: str = "",
        profile_id: str = "",
        protocol: str = "",
        status: str = "",
        conversation_id: str = "",
        request_id: str = "",
        run_id: str = "",
        model: str = "",
        stream: bool | None = None,
        error_code: str = "",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        total = 0
        for entry in self._iter_entries_newest():
            if not (
                (not provider or entry.get("provider") == provider)
                and (not profile_id or entry.get("profileId") == profile_id)
                and (not protocol or entry.get("protocol") == protocol)
                and (not status or entry.get("status") == status)
                and (not conversation_id or entry.get("conversationId") == conversation_id)
                and (not request_id or entry.get("requestId") == request_id)
                and (not run_id or entry.get("runId") == run_id)
                and (not model or entry.get("model") == model)
                and (stream is None or entry.get("stream") is stream)
                and (not error_code or entry.get("errorCode") == error_code)
                and _timestamp_in_range(
                    entry.get("timestamp"),
                    started_at=started_at,
                    ended_at=ended_at,
                )
            ):
                continue
            if total >= offset and len(items) < limit:
                items.append(entry)
            total += 1
        return items, total

    def get(self, request_id: str) -> dict[str, Any] | None:
        for entry in self._iter_entries_newest():
            if entry.get("requestId") == request_id:
                return entry
        return None

    def get_attempts(self, request_id: str) -> list[dict[str, Any]]:
        attempts = [
            entry
            for entry in self._iter_entries_newest()
            if entry.get("requestId") == request_id
        ]
        attempts.sort(
            key=lambda entry: (
                int(entry.get("attempt") or 0),
                str(entry.get("timestamp") or ""),
            )
        )
        return attempts

    def _iter_entries_newest(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            for path in self._log_paths_newest():
                try:
                    for line in _iter_lines_reverse(path):
                        entry = _parse_entry(line)
                        if entry is not None:
                            yield entry
                except OSError:
                    continue

    def _log_paths_newest(self) -> Iterator[Path]:
        if self.path.is_file():
            yield self.path
        for index in range(1, self.backup_count + 1):
            candidate = self._backup_path(index)
            if candidate.is_file():
                yield candidate

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_bytes = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_bytes == 0 or current_bytes + incoming_bytes <= self.max_bytes:
            return
        oldest = self._backup_path(self.backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self.path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")


def _sanitize_usage(value: Any) -> dict[str, int | bool | str]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, int | bool | str] = {}
    for key in _TOKEN_USAGE_FIELDS:
        item = value.get(key)
        if (
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= _MAX_USAGE_TOKENS
        ):
            sanitized[key] = item
    for key in _FINISH_REASON_FIELDS:
        reason = _sanitize_finish_reason(value.get(key))
        if reason:
            sanitized[key] = reason
    if isinstance(value.get("output_truncated"), bool):
        sanitized["output_truncated"] = value["output_truncated"]
    return sanitized


def _sanitize_finish_reason(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return normalized if normalized in _KNOWN_FINISH_REASONS else ""


def _sanitize_error(error: Any) -> tuple[str, str]:
    if isinstance(error, dict):
        raw_code = error.get("code")
        raw_message = error.get("message")
    else:
        raw_code = None
        raw_message = str(error) if error is not None else None
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    if code not in _KNOWN_ERROR_CODES:
        code = ""
    if not isinstance(raw_message, str):
        return code, ""
    message = redact_value(raw_message)
    if not isinstance(message, str):
        return code, ""
    return code, message[:_MAX_ERROR_MESSAGE_CHARS]


def _parse_entry(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
        if not isinstance(payload, dict):
            return None
        error_code, error_message = _sanitize_error(
            {
                "code": payload.get("errorCode"),
                "message": payload.get("errorMessage"),
            }
        )
        usage = _sanitize_usage(payload.get("usage"))
        payload = {
            **payload,
            "correlationId": payload.get("correlationId") or "",
            "parentRequestId": payload.get("parentRequestId") or "",
            "retryOf": payload.get("retryOf") or "",
            "traceId": payload.get("traceId") or "",
            "endpoint": payload.get("endpoint") or "",
            "errorCode": error_code,
            "errorMessage": error_message,
            "finishReason": (
                _sanitize_finish_reason(payload.get("finishReason"))
                or str(usage.get("finish_reason") or usage.get("stop_reason") or "")
            ),
            "stopReason": (
                _sanitize_finish_reason(payload.get("stopReason"))
                or _sanitize_finish_reason(payload.get("finishReason"))
                or str(usage.get("stop_reason") or usage.get("finish_reason") or "")
            ),
            "usage": usage,
            "requestMetadata": payload.get("requestMetadata")
            if isinstance(payload.get("requestMetadata"), dict)
            else {},
            "responseMetadata": payload.get("responseMetadata")
            if isinstance(payload.get("responseMetadata"), dict)
            else {},
        }
        payload = redact_value(payload)
        if not isinstance(payload, dict):
            return None
        return RequestLogEntry.model_validate(payload).model_dump(
            mode="json",
            by_alias=True,
        )
    except (ValueError, TypeError):
        return None


def _iter_lines_reverse(path: Path) -> Iterator[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        carry = b""
        skip_overlong_line = False
        while position > 0:
            read_size = min(_READ_CHUNK_BYTES, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + carry
            end = len(data)
            while True:
                newline = data.rfind(b"\n", 0, end)
                if newline < 0:
                    prefix = data[:end]
                    if skip_overlong_line or len(prefix) > _MAX_LOG_LINE_BYTES:
                        carry = b""
                        skip_overlong_line = True
                    else:
                        carry = prefix
                    break
                raw_line = data[newline + 1 : end].rstrip(b"\r")
                if skip_overlong_line:
                    skip_overlong_line = False
                elif raw_line and len(raw_line) <= _MAX_LOG_LINE_BYTES:
                    try:
                        yield raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                end = newline
        if carry and not skip_overlong_line and len(carry) <= _MAX_LOG_LINE_BYTES:
            try:
                yield carry.rstrip(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                pass


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_in_range(
    value: Any,
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> bool:
    if started_at is None and ended_at is None:
        return True
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
