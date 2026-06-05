from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from fastapi import HTTPException


async def iter_sse_events(lines: AsyncIterable[str]) -> AsyncIterator[dict[str, Any]]:
    event_type = ""
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if data_lines:
                event = _parse_sse_event(event_type, data_lines)
                if event is not None:
                    yield event
                event_type = ""
                data_lines = []
            continue
        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    if data_lines:
        event = _parse_sse_event(event_type, data_lines)
        if event is not None:
            yield event


def event_usage(event: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    direct_usage = event.get("usage")
    if isinstance(direct_usage, dict):
        usage = merge_numeric_usage(usage, direct_usage)
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        usage = merge_numeric_usage(usage, message["usage"])
    return usage


def merge_numeric_usage(
    accumulated: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(accumulated)
    for key, value in usage.items():
        if is_number(value):
            previous = merged.get(key, 0)
            merged[key] = previous + value if is_number(previous) else value
        elif key not in merged:
            merged[key] = value
    return merged


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def extract_stop_reason(payload: dict[str, Any]) -> str:
    for key in ("stop_reason", "stopReason"):
        value = payload.get(key)
        if value:
            return str(value)
    delta = payload.get("delta")
    if isinstance(delta, dict):
        for key in ("stop_reason", "stopReason"):
            value = delta.get(key)
            if value:
                return str(value)
    message = payload.get("message")
    if isinstance(message, dict):
        for key in ("stop_reason", "stopReason"):
            value = message.get(key)
            if value:
                return str(value)
    return ""


def stream_text_delta(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if isinstance(delta, dict):
            return str(delta.get("text") or "")
    if event_type == "text_delta":
        return str(event.get("text") or "")
    return ""


def raise_for_stream_error_event(event: dict[str, Any]) -> None:
    if event.get("type") != "error":
        return
    error = event.get("error")
    if isinstance(error, dict):
        message = str(
            error.get("message")
            or error.get("error")
            or error.get("type")
            or "MiMo stream error"
        )
    else:
        message = str(error or event.get("message") or "MiMo stream error")
    raise _mimo_response_exception(502, "MIMO_STREAM_ERROR", message)


def _parse_sse_event(
    event_type: str,
    data_lines: list[str],
) -> dict[str, Any] | None:
    raw = "\n".join(data_lines)
    if raw == "[DONE]":
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _mimo_response_exception(
            502,
            "MIMO_RESPONSE_INVALID",
            "MiMo stream event is not valid JSON",
        ) from exc
    if not isinstance(event, dict):
        raise _mimo_response_exception(
            502,
            "MIMO_RESPONSE_INVALID",
            "MiMo stream event JSON is not an object",
        )
    if event_type and "type" not in event:
        event["type"] = event_type
    raise_for_stream_error_event(event)
    return event


def _mimo_response_exception(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )
