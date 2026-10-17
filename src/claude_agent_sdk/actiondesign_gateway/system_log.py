"""Opt-in, plaintext HTTP debugging logs, separate from redacted audit logs."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_logger = logging.getLogger(__name__)
_SYSTEM_LOG_PATH = "/api/actiondesign-agent/admin/system-logs"
_capture: ContextVar[tuple[SystemLogStore, str, dict[str, Any]] | None] = ContextVar(
    "system_log_capture", default=None
)


def _body(data: bytes) -> dict[str, Any]:
    """Keep exact bytes recoverable, including binary bodies, without clipping."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "text": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
        }
    result: dict[str, Any] = {"encoding": "utf-8", "text": text, "bytes": len(data)}
    with suppress(ValueError, RecursionError, UnicodeError):
        parsed = json.loads(text)
        # Invalid JSON numbers and escaped lone surrogates stay inspectable as
        # raw text without breaking the admin JSON response encoder.
        json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
        result["json"] = parsed
    return result


def _identity(body: bytes, headers: list[list[str]]) -> dict[str, str]:
    header_map = {key.lower(): value for key, value in headers}
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    identity = {
        "requestId": str(
            payload.get("requestId") or header_map.get("x-request-id", "")
        ),
        "conversationId": str(
            payload.get("conversationId") or header_map.get("x-conversation-id", "")
        ),
        "runId": str(payload.get("runId") or header_map.get("x-agent-run-id", "")),
        "model": str(payload.get("model") or ""),
        "provider": str(payload.get("provider") or ""),
    }
    return {
        key: value.encode("utf-8", "backslashreplace").decode("utf-8")
        for key, value in identity.items()
    }


def _response_body(entry: dict[str, Any], data: bytes) -> None:
    response = entry["response"]
    response["body"] = _body(data)
    headers = response.get("headers", [])
    if any(key.lower() == "content-encoding" for key, _ in headers):
        try:
            decoded = httpx.Response(200, headers=headers, content=data).content
            if decoded != data:
                response["wireBody"] = response["body"]
                response["body"] = _body(decoded)
        except httpx.DecodingError as exc:
            response["decodingError"] = str(exc)


class SystemLogStore:
    """SQLite detail storage; list queries never return request/response bodies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        if not self._ready:
            connection.execute("""CREATE TABLE IF NOT EXISTS logs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, timestamp TEXT NOT NULL,
                direction TEXT NOT NULL, method TEXT NOT NULL, url TEXT NOT NULL,
                path TEXT NOT NULL, status INTEGER NOT NULL, duration REAL NOT NULL,
                request_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                run_id TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
                parent_id TEXT NOT NULL, error TEXT NOT NULL, detail TEXT NOT NULL
            )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS logs_parent ON logs(parent_id)"
            )
            connection.commit()
            with suppress(OSError):
                self.path.chmod(0o600)
            self._ready = True
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, entry: dict[str, Any]) -> None:
        # Debugging must not break a runtime request if the disk is unavailable.
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO logs (id,timestamp,direction,method,url,path,status,
                    duration,request_id,conversation_id,run_id,model,provider,parent_id,error,detail)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                    duration=excluded.duration, request_id=excluded.request_id,
                    conversation_id=excluded.conversation_id, run_id=excluded.run_id,
                    model=excluded.model, provider=excluded.provider,
                    error=excluded.error, detail=excluded.detail""",
                    (
                        entry["id"],
                        entry["timestamp"],
                        entry["direction"],
                        entry["method"],
                        entry["url"],
                        entry["path"],
                        entry.get("status", 0),
                        entry.get("durationMs", 0),
                        entry.get("requestId", ""),
                        entry.get("conversationId", ""),
                        entry.get("runId", ""),
                        entry.get("model", ""),
                        entry.get("provider", ""),
                        entry.get("parentId", ""),
                        entry.get("error", ""),
                        json.dumps(entry, ensure_ascii=False, allow_nan=False)
                        .encode("utf-8", "backslashreplace")
                        .decode("utf-8"),
                    ),
                )
        except (OSError, sqlite3.Error):
            _logger.exception("Unable to persist system debug log")

    def list(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        for key, column in {
            "direction": "direction",
            "method": "method",
            "status": "status",
            "requestId": "request_id",
            "conversationId": "conversation_id",
            "runId": "run_id",
            "model": "model",
            "provider": "provider",
            "parentId": "parent_id",
        }.items():
            if filters.get(key) not in (None, ""):
                clauses.append(f"{column} = ?")
                values.append(filters[key])
        for key, operator in (("startedAt", ">="), ("endedAt", "<=")):
            if filters.get(key):
                clauses.append(f"timestamp {operator} ?")
                values.append(filters[key])
        if filters.get("errorsOnly"):
            clauses.append("(status >= 400 OR error != '')")
        if filters.get("q"):
            # instr searches literal text, including JSON bodies and headers.
            clauses.append("instr(lower(detail), lower(?)) > 0")
            values.append(filters["q"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = connection.execute(
                "SELECT count(*) FROM logs" + where, values
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT id,timestamp,direction,method,url,path,status,duration AS durationMs,
                request_id AS requestId,conversation_id AS conversationId,run_id AS runId,
                model,provider,parent_id AS parentId,error FROM logs"""
                + where
                + " ORDER BY sequence DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get(self, log_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT detail FROM logs WHERE id = ?", (log_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self) -> int:
        with self._connect() as connection:
            connection.execute("PRAGMA secure_delete=ON")
            count = connection.execute("SELECT count(*) FROM logs").fetchone()[0]
            connection.execute("DELETE FROM logs")
            connection.commit()
            connection.execute("VACUUM")
        return count


def _entry(direction: str, method: str, url: str, path: str) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "method": method,
        "url": url,
        "path": path,
        "status": 0,
        "durationMs": 0,
        "request": {},
        "response": {},
        "complete": False,
        "error": "",
    }


class SystemLogMiddleware:
    """Observe ASGI messages without consuming ahead or buffering SSE delivery."""

    def __init__(self, app: ASGIApp, store: SystemLogStore, error_handler: Any) -> None:
        self.app = app
        self.store = store
        self.error_handler = error_handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Avoid recursively persisting the log viewer's own detail responses.
        if scope["type"] != "http" or (
            scope["path"] == _SYSTEM_LOG_PATH
            or scope["path"].startswith(_SYSTEM_LOG_PATH + "/")
        ):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        headers = [
            [key.decode("latin-1"), value.decode("latin-1")]
            for key, value in scope.get("headers", [])
        ]
        host = next(
            (value for key, value in headers if key.lower() == "host"), "localhost"
        )
        query = scope.get("query_string", b"").decode("latin-1")
        raw_path = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
        url = f"{scope.get('scheme', 'http')}://{host}{raw_path}" + (
            f"?{query}" if query else ""
        )
        entry = _entry("inbound", scope["method"], url, scope["path"])
        entry["request"] = {
            "headers": headers,
            "queryString": query,
            "queryParams": parse_qsl(query, keep_blank_values=True),
            "client": list(scope.get("client") or []),
        }
        request_body = bytearray()
        response_body = bytearray()
        request_complete = False
        token = _capture.set((self.store, entry["id"], entry))
        await asyncio.to_thread(self.store.save, entry)

        async def observed_receive() -> Message:
            nonlocal request_complete
            message = await receive()
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
                request_complete = not message.get("more_body", False)
            return message

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
                entry["response"]["headers"] = [
                    [key.decode("latin-1"), value.decode("latin-1")]
                    for key, value in message.get("headers", [])
                ]
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                entry["complete"] = not message.get("more_body", False)
            await send(message)

        try:
            # Read once and replay so even authentication/validation rejections
            # retain all submitted parameters. Response delivery remains streamed.
            messages: list[Message] = []
            while not request_complete:
                message = await observed_receive()
                messages.append(message)
                if message["type"] == "http.disconnect":
                    break

            async def replay_receive() -> Message:
                if messages:
                    return messages.pop(0)
                return await receive()

            entry["request"]["body"] = _body(bytes(request_body))
            entry.update(_identity(bytes(request_body), headers))
            await asyncio.to_thread(self.store.save, entry)
            await self.app(scope, replay_receive, observed_send)
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()
            if not entry["status"] and isinstance(exc, Exception):
                response = await self.error_handler(Request(scope), exc)
                await response(scope, receive, observed_send)
            elif not entry["status"]:
                entry["status"] = 500
            raise
        finally:
            _capture.reset(token)
            entry["durationMs"] = round((time.monotonic() - started) * 1000, 2)
            entry["request"]["body"] = _body(bytes(request_body))
            entry["request"]["complete"] = request_complete
            _response_body(entry, bytes(response_body))
            entry.update(_identity(bytes(request_body), headers))
            await asyncio.to_thread(self.store.save, entry)


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        store: SystemLogStore,
        entry: dict[str, Any],
        started: float,
    ) -> None:
        self.stream, self.store, self.entry, self.started = (
            stream,
            store,
            entry,
            started,
        )
        self.body = bytearray()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.stream:
                self.body.extend(chunk)
                yield chunk
            self.entry["complete"] = True
        except BaseException as exc:
            self.entry["error"] = f"{type(exc).__name__}: {exc}"
            self.entry["traceback"] = traceback.format_exc()
            raise

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            self.entry["durationMs"] = round(
                (time.monotonic() - self.started) * 1000, 2
            )
            _response_body(self.entry, bytes(self.body))
            await asyncio.to_thread(self.store.save, self.entry)


def provider_log_hooks(profile: Any) -> dict[str, list[Any]]:
    """Capture the actual HTTPX request after protocol conversion, for each attempt."""
    context = _capture.get()
    if context is None:
        return {}
    store, parent_id, parent_entry = context

    async def request_hook(request: httpx.Request) -> None:
        entry = _entry("outbound", request.method, str(request.url), request.url.path)
        entry.update(
            {
                "parentId": parent_id,
                "provider": profile.provider,
                "profileId": profile.id,
                "protocol": profile.protocol,
            }
        )
        body = await request.aread()
        headers = [list(pair) for pair in request.headers.multi_items()]
        entry.update(_identity(body, headers))
        for key in ("requestId", "conversationId", "runId"):
            if not entry.get(key):
                entry[key] = parent_entry.get(key, "")
        entry["provider"] = profile.provider
        entry["request"] = {
            "headers": headers,
            "queryString": request.url.query.decode("ascii"),
            "queryParams": list(request.url.params.multi_items()),
            "body": _body(body),
        }
        request.extensions["system_log"] = (entry, time.monotonic())
        await asyncio.to_thread(store.save, entry)

    async def response_hook(response: httpx.Response) -> None:
        entry, started = response.request.extensions["system_log"]
        entry["status"] = response.status_code
        entry["response"] = {
            "headers": [list(pair) for pair in response.headers.multi_items()]
        }
        if response.is_stream_consumed:
            entry["response"]["body"] = _body(response.content)
            entry["complete"] = True
            entry["durationMs"] = round((time.monotonic() - started) * 1000, 2)
            await asyncio.to_thread(store.save, entry)
            return
        response.stream = _ObservedStream(response.stream, store, entry, started)

    return {"request": [request_hook], "response": [response_hook]}


class SystemLogHttpClient(httpx.AsyncClient):
    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except BaseException as exc:
            pending = request.extensions.get("system_log")
            context = _capture.get()
            if pending and context:
                entry, started = pending
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["traceback"] = traceback.format_exc()
                entry["durationMs"] = round((time.monotonic() - started) * 1000, 2)
                await asyncio.to_thread(context[0].save, entry)
            raise
