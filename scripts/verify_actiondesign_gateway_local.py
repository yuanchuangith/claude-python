"""Local HTTP success-path verifier for the ActionDesign thin gateway.

The verifier starts an ephemeral loopback-only Anthropic-compatible provider,
then drives the real FastAPI gateway through its public HTTP routes. It uses no
external network, real credentials, database, MCP process, or persistent files.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings


class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        model = str(request.get("model") or "mimo-v2.5")
        if request.get("stream"):
            events = [
                {
                    "type": "message_start",
                    "message": {"model": model, "usage": {"input_tokens": 12}},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_local",
                        "name": "create_node",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"elementKey":"ExitAction"}',
                    },
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
            body = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                for event in events
            ).encode()
            self._write(200, "text/event-stream", body)
            return

        body = json.dumps(
            {
                "id": "msg_local",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_local",
                        "name": "create_node",
                        "input": {"elementKey": "ExitAction"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
            separators=(",", ":"),
        ).encode()
        self._write(200, "application/json", body)

    def _write(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _payload(*, stream: bool, profile_id: str = "") -> dict[str, Any]:
    sha_a = f"sha256:{'a' * 64}"
    sha_b = f"sha256:{'b' * 64}"
    return {
        "provider": "mimo",
        "profileId": profile_id,
        "conversationId": "99999999-9999-4999-8999-999999999999",
        "runId": (
            "run_99999999-9999-4999-8999-999999999998"
            if stream
            else "run_99999999-9999-4999-8999-999999999999"
        ),
        "agentMode": "normal",
        "systemPrompt": "You are the ActionDesign system agent.",
        "prompt": "Create one ExitAction node",
        "stream": stream,
        "protocolVersion": "1.0",
        "graphVersion": "2.0.0",
        "promptManifestVersion": "2.2.0",
        "promptManifestHash": sha_a,
        "toolCatalogVersion": "1.8.0",
        "toolCatalogHash": sha_b,
        "tools": [
            {
                "name": "create_node",
                "description": "Create one canvas node",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"elementKey": {"type": "string"}},
                    "required": ["elementKey"],
                },
            }
        ],
    }


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix="actiondesign-gateway-smoke-") as directory:
            root = Path(directory)
            settings = Settings(
                default_provider="mimo",
                allow_origins=["http://127.0.0.1:3111"],
                log_root=root / "audit",
                full_conversation_log_enabled=True,
                full_conversation_log_root=root / "conversations",
                mimo_api_key="local-smoke-key",
                mimo_messages_url=f"http://127.0.0.1:{server.server_port}/v1/messages",
                mimo_default_model="mimo-v2.5",
                claude_code_api_key="",
                claude_code_messages_url="https://api.anthropic.com/v1/messages",
                claude_code_default_model="claude-sonnet-4-5",
                claude_code_models=["claude-sonnet-4-5"],
                profile_store_path=root / "profiles.json",
                profile_admin_token="local-admin-token",
                admin_allow_remote=True,
                profile_allowed_hosts=["127.0.0.1", "api.anthropic.com"],
                langsmith_enabled=False,
                langsmith_api_key="",
            )
            client = TestClient(
                create_app(settings, profile_secret_store=MemoryProfileSecretStore())
            )
            health = client.get("/api/actiondesign-agent/health")
            openapi = client.get("/openapi.json")
            models = client.get("/api/actiondesign-agent/models")
            admin_headers = {"Authorization": "Bearer local-admin-token"}
            managed = client.put(
                "/api/actiondesign-agent/admin/profiles/local-managed",
                headers=admin_headers,
                json={
                    "id": "local-managed",
                    "provider": "mimo",
                    "baseUrl": f"http://127.0.0.1:{server.server_port}/v1/messages",
                    "apiKey": "local-managed-key",
                    "defaultModel": "mimo-v2.5",
                    "authMode": "api-key",
                    "timeoutSeconds": 30,
                    "enabled": True,
                },
            )
            managed_list = client.get(
                "/api/actiondesign-agent/admin/profiles", headers=admin_headers
            )
            chat_payload = _payload(stream=False, profile_id="local-managed")
            stream_payload = _payload(stream=True, profile_id="local-managed")
            chat = client.post(
                "/api/actiondesign-agent/mimo/chat",
                headers={
                    "X-Conversation-Id": chat_payload["conversationId"],
                    "X-Agent-Run-Id": chat_payload["runId"],
                },
                json=chat_payload,
            )
            stream = client.post(
                "/api/actiondesign-agent/mimo/chat/stream",
                headers={
                    "X-Conversation-Id": stream_payload["conversationId"],
                    "X-Agent-Run-Id": stream_payload["runId"],
                },
                json=stream_payload,
            )
            deleted = client.delete(
                "/api/actiondesign-agent/admin/profiles/local-managed",
                headers=admin_headers,
            )
            for response in (
                health,
                openapi,
                models,
                managed,
                managed_list,
                chat,
                stream,
                deleted,
            ):
                response.raise_for_status()

            chat_body = chat.json()
            managed_body = managed.json()
            listed_profile = next(
                profile
                for profile in managed_list.json()
                if profile["id"] == "local-managed"
            )
            log_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*.jsonl")
            )
            result = {
                "status": "passed",
                "runtime": health.json()["runtime"],
                "openapiVersion": openapi.json()["info"]["version"],
                "providerStatus": models.json()["providers"]["mimo"]["status"],
                "nonStreamSuccess": chat_body["success"],
                "nativeTool": chat_body["tool_calls"][0]["name"],
                "nativeArgument": chat_body["tool_calls"][0]["arguments"]["elementKey"],
                "streamCompleted": '"type": "model.completed"' in stream.text,
                "streamNativeTool": "create_node" in stream.text,
                "managedProfileKeyConfigured": listed_profile["keyConfigured"],
                "managedProfileSecretExposed": "apiKey" in managed_body
                or "apiKey" in listed_profile,
                "managedProfileDeleted": deleted.json()["success"],
                "secretLeaked": "local-smoke-key" in log_text
                or "local-managed-key" in log_text,
            }
            if result != {
                "status": "passed",
                "runtime": "thin-gateway",
                "openapiVersion": "2.2.0",
                "providerStatus": "ready",
                "nonStreamSuccess": True,
                "nativeTool": "create_node",
                "nativeArgument": "ExitAction",
                "streamCompleted": True,
                "streamNativeTool": True,
                "managedProfileKeyConfigured": True,
                "managedProfileSecretExposed": False,
                "managedProfileDeleted": True,
                "secretLeaked": False,
            }:
                raise RuntimeError(f"local gateway verification failed: {result}")
            print(json.dumps(result, ensure_ascii=False))
            return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
