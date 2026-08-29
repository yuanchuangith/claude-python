from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


TOOL = {
    "name": "canary_echo",
    "description": "Return a canary value without side effects.",
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}


def response_for(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "actiondesign-mcp-canary", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        value = str((message.get("params") or {}).get("arguments", {}).get("value", ""))
        result = {"content": [{"type": "text", "text": value}], "isError": False}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "ActionDesignMcpCanary/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        payload = response_for(message)
        if payload is None:
            self.send_response(202)
            self.end_headers()
            return
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if message.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", "actiondesign-canary-session")
        self.end_headers()
        self.wfile.write(encoded)

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_stdio() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        payload = response_for(message)
        if payload is not None:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        run_stdio()
    else:
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
