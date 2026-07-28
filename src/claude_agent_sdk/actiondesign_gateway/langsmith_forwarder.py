from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from .redaction import redact_value

_LANGSMITH_RUN_TYPES = {
    "graph": "chain",
    "chain": "chain",
    "llm": "llm",
    "tool": "tool",
    "retriever": "retriever",
    "interrupt": "chain",
}

_LANGSMITH_MAX_ATTEMPTS = 3
_LANGSMITH_RETRY_BASE_DELAY_SECONDS = 0.05
_LANGSMITH_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"actiondesign:{value}"))


def _headers(settings: Any) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "x-api-key": settings.langsmith_api_key}
    if settings.langsmith_workspace_id:
        headers["X-Tenant-Id"] = settings.langsmith_workspace_id
    return headers


def _run_type(value: Any) -> str:
    return _LANGSMITH_RUN_TYPES.get(str(value or "chain"), "chain")


def _object_payload(value: Any) -> dict[str, Any]:
    sanitized = redact_value(value)
    if sanitized is None:
        return {}
    if isinstance(sanitized, dict):
        return sanitized
    return {"value": sanitized}


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = _object_payload(event.get("metadata") or {})
    if event.get("event") == "error":
        error = event.get("error") or {}
        metadata["traceError"] = {
            "code": str(error.get("code") or "TRACE_RUN_ERROR"),
            "retryable": bool(error.get("retryable", False)),
        }
    return metadata


async def _send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> bool:
    for attempt in range(1, _LANGSMITH_MAX_ATTEMPTS + 1):
        try:
            if method == "POST":
                response = await client.post(url, headers=headers, json=payload)
            else:
                response = await client.patch(url, headers=headers, json=payload)
            status_code = int(getattr(response, "status_code", 200))
            if method == "POST" and status_code == 409:
                return True
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code not in _LANGSMITH_RETRYABLE_STATUS_CODES:
                return False
        except httpx.HTTPError:
            pass
        if attempt < _LANGSMITH_MAX_ATTEMPTS:
            await asyncio.sleep(_LANGSMITH_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return False


async def forward_trace_batch(settings: Any, events: list[dict[str, Any]]) -> tuple[int, int]:
    if not settings.langsmith_enabled or not settings.langsmith_api_key:
        return 0, len(events)
    accepted = 0
    rejected = 0
    base = str(settings.langsmith_endpoint).rstrip("/")
    async with httpx.AsyncClient(timeout=settings.langsmith_timeout_seconds) as client:
        for event in events:
            try:
                run_id = _uuid(str(event.get("traceRunId") or event.get("eventId") or uuid.uuid4()))
                trace_id = _uuid(str(event.get("traceId") or run_id))
                parent_run_id = _uuid(str(event["parentTraceRunId"])) if event.get("parentTraceRunId") else None
                metadata = _metadata(event)
                if event.get("event") == "start":
                    payload = {
                        "id": run_id,
                        "trace_id": trace_id,
                        "parent_run_id": parent_run_id,
                        "name": str(event.get("name") or "actiondesign"),
                        "run_type": _run_type(event.get("runType")),
                        "start_time": event.get("timestamp"),
                        "inputs": _object_payload(event.get("inputs")),
                        "session_name": settings.langsmith_project,
                        "tags": event.get("tags") or [],
                        "extra": {"metadata": metadata},
                    }
                    success = await _send_with_retry(
                        client,
                        "POST",
                        f"{base}/runs",
                        headers=_headers(settings),
                        payload=payload,
                    )
                else:
                    error = event.get("error") or {}
                    error_text = None
                    if event.get("event") == "error":
                        error_text = redact_value(
                            f"{error.get('code') or 'TRACE_RUN_ERROR'}: "
                            f"{error.get('message') or 'Trace run failed'} "
                            f"(retryable={bool(error.get('retryable', False))})"
                        )
                    payload = {
                        "id": run_id,
                        "trace_id": trace_id,
                        "parent_run_id": parent_run_id,
                        "session_name": settings.langsmith_project,
                        "end_time": event.get("timestamp"),
                        "outputs": _object_payload(event.get("outputs")),
                        "error": error_text,
                        "tags": event.get("tags") or [],
                        "extra": {"metadata": metadata},
                    }
                    success = await _send_with_retry(
                        client,
                        "PATCH",
                        f"{base}/runs/{run_id}",
                        headers=_headers(settings),
                        payload=payload,
                    )
                if success:
                    accepted += 1
                else:
                    rejected += 1
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                rejected += 1
    return accepted, rejected
