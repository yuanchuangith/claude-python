from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings

REQUEST_ID = "88888888-8888-4888-8888-888888888888"
HEADERS = {
    "X-ActionDesign-Service-Token": "fixture-service-token-1234567890123456",
    "X-Request-Id": REQUEST_ID,
    "X-Correlation-Id": REQUEST_ID,
}

def make_app(tmp_path: Path):
    command = Path(sys.executable).resolve()
    settings = Settings(
        allow_origins=["https://actiondesign.example.test"],
        service_auth_mode="required",
        service_auth_token="fixture-service-token-1234567890123456",
        profile_admin_token="fixture-admin",
        log_root=tmp_path / "logs",
        request_log_path=tmp_path / "request.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        profile_store_path=tmp_path / "profiles.json",
        managed_secret_store_path=tmp_path / "secrets.json",
        mcp_server_store_path=tmp_path / "mcp.json",
        browser_api_key_store_path=tmp_path / "browser.json",
        client_api_key_store_path=tmp_path / "clients.json",
        mcp_proxy_enabled=True,
        mcp_allowed_executable_paths=[str(command)],
        mcp_servers=[{
            "id": "fixture", "transport": "stdio", "executablePath": str(command), "args": [],
        }],
    )
    return create_app(settings, profile_secret_store=MemoryProfileSecretStore())


class Proxy:
    def __init__(self, started=None, release=None):
        self.calls = 0
        self.started = started
        self.release = release

    async def list_tools(self, server_id):
        self.calls += 1
        if self.started:
            self.started.set()
            assert await asyncio.to_thread(self.release.wait, 5)
        return {"serverId": server_id, "serverName": "fixture", "serverVersion": "1", "tools": []}

    async def call_tool(self, server_id, tool_name, arguments):
        self.calls += 1
        return {
            "serverId": server_id, "serverName": "fixture", "serverVersion": "1",
            "toolName": tool_name, "durationMs": 0, "result": {"content": [], "isError": False},
        }


def test_stale_step_does_not_dispatch_tools_or_calls(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    proxy = Proxy()
    app.state.mcp_proxy = proxy
    monkeypatch.setattr(app.state.mcp_server_store, "effective_version", lambda _id: 2)
    with TestClient(app, headers=HEADERS) as client:
        path = "/api/actiondesign-agent/mcp/fixture"
        headers = {"X-MCP-Config-Version": "1"}
        stale = client.get(f"{path}/tools", headers=headers)
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "MCP_CONFIG_VERSION_CONFLICT"
        assert proxy.calls == 0
        identity = REQUEST_ID
        conversation_id, run_id = str(uuid.uuid4()), f"run_{uuid.uuid4()}"
        called = client.post(f"{path}/call", headers={
            **headers, "X-Conversation-Id": conversation_id, "X-Agent-Run-Id": run_id,
        }, json={
            "requestId": identity, "correlationId": identity, "toolName": "echo",
            "arguments": {}, "conversationId": conversation_id, "runId": run_id,
        })
        assert called.status_code == 409
        assert proxy.calls == 0
        for invalid in ("0", "-1", "1.0", "abc", "999999999999"):
            assert client.get(f"{path}/tools", headers={"X-MCP-Config-Version": invalid}).status_code == 422
        assert client.get(f"{path}/tools", headers={"X-MCP-Config-Version": "2"}).status_code == 200
        assert client.get(f"{path}/tools").status_code == 200  # Existing callers remain compatible.
        config = client.get(f"{path}/connection-config")
        assert config.status_code == 200
        assert config.headers["cache-control"] == "no-store"
        assert config.json()["transport"] == "stdio"
        assert "stdio" not in config.json()
        assert config.json()["streamableHttp"] is None
        assert TestClient(app).get(f"{path}/connection-config").status_code == 401


def test_inflight_request_keeps_old_proxy_after_replacement(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    started, release = threading.Event(), threading.Event()
    old, newest = Proxy(started, release), Proxy()
    app.state.mcp_proxy = old
    version = [1]
    monkeypatch.setattr(app.state.mcp_server_store, "effective_version", lambda _id: version[0])
    with TestClient(app, headers=HEADERS) as client, ThreadPoolExecutor(max_workers=1) as pool:
        path = "/api/actiondesign-agent/mcp/fixture/tools"
        pending = pool.submit(client.get, path, headers={"X-MCP-Config-Version": "1"})
        try:
            assert started.wait(5)
            app.state.mcp_proxy = newest
            version[0] = 2
            assert client.get(path, headers={"X-MCP-Config-Version": "1"}).status_code == 409
            assert client.get(path, headers={"X-MCP-Config-Version": "2"}).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
        assert old.calls == 1
        assert newest.calls == 1

