from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_actiondesign_mcp_live.py"
)


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False, sort_keys=True)


class _FakeClient:
    def __init__(
        self,
        *,
        fail_call: bool = False,
        leak_masked_field: bool = False,
        expose_mcp_policy: bool = False,
        leak_mcp_transport: bool = False,
    ) -> None:
        self.fail_call = fail_call
        self.leak_masked_field = leak_masked_field
        self.expose_mcp_policy = expose_mcp_policy
        self.leak_mcp_transport = leak_mcp_transport
        self.calls: list[dict[str, Any]] = []
        self.server_id = ""
        self.disabled = False
        self.deleted = False
        self.secret_id = ""
        self.secret_configured = False
        self.secret_values: list[str] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    @staticmethod
    def _tool() -> dict[str, Any]:
        return {
            "name": "read_wiki_structure",
            "description": "Read a public repository wiki structure",
            "inputSchema": {
                "type": "object",
                "properties": {"repoName": {"type": "string"}},
                "required": ["repoName"],
            },
        }

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _Response:
        path = urlsplit(url).path
        self.calls.append({
            "method": method,
            "path": path,
            "headers": dict(headers or {}),
            "json": json,
        })
        if method == "GET" and path == "/openapi.json":
            api = "/api/actiondesign-agent"
            return _Response(200, {
                "info": {"version": "3.1.0"},
                "paths": {
                    f"{api}/admin/mcp-servers": {},
                    f"{api}/admin/mcp-servers/{{server_id}}": {},
                    f"{api}/admin/mcp-servers/{{server_id}}/enabled": {},
                    f"{api}/admin/mcp-servers/{{server_id}}/connection-test": {},
                    f"{api}/admin/secrets": {},
                    f"{api}/admin/secrets/{{secret_id}}": {},
                    f"{api}/admin/secrets/{{secret_id}}/rotate": {},
                    f"{api}/mcp/{{server_id}}/tools": {},
                    f"{api}/mcp/{{server_id}}/call": {},
                },
            })
        if method == "GET" and path.endswith("/health"):
            return _Response(200, {"status": "ok", "mcpProxyEnabled": True})
        if method == "POST" and path.endswith("/admin/login"):
            assert json == {"password": "admin-password-probe"}
            return _Response(200, {"sessionToken": "session-credential-probe"})
        if method == "GET" and path.endswith("/admin/runtime-policy"):
            payload = {
                "profileAllowedHosts": ["api.example.test"],
                "providerPaths": {
                    "openai": "/v1/chat/completions",
                    "claude": "/v1/messages",
                },
            }
            if self.expose_mcp_policy:
                payload["mcpAllowedHosts"] = ["mcp.deepwiki.com"]
            return _Response(200, payload)
        if method == "GET" and path.endswith("/admin/secrets"):
            return _Response(200, [])
        if method == "PUT" and "/admin/secrets/" in path:
            assert json is not None
            self.secret_id = str(json["id"])
            self.secret_configured = True
            self.secret_values.append(str(json["value"]))
            return _Response(200, self._secret_public())
        if method == "POST" and path.endswith("/rotate") and "/admin/secrets/" in path:
            assert json is not None
            self.secret_values.append(str(json["value"]))
            return _Response(200, self._secret_public())
        if method == "DELETE" and "/admin/secrets/" in path:
            self.secret_configured = False
            return _Response(200, self._secret_public())
        if method == "GET" and "/admin/secrets/" in path:
            return _Response(200, self._secret_public())
        if method == "PUT" and path.endswith("/enabled"):
            self.disabled = json == {"enabled": False}
            return _Response(200, self._server_public(enabled=not self.disabled))
        if method == "DELETE" and "/admin/mcp-servers/" in path:
            self.deleted = True
            return _Response(200, {"id": self.server_id, "deleted": True})
        if method == "GET" and "/admin/mcp-servers/" in path:
            if self.deleted:
                return _Response(404, {"detail": {"code": "MCP_SERVER_NOT_FOUND"}})
        if method == "PUT" and "/admin/mcp-servers/" in path:
            assert json is not None
            self.server_id = str(json["id"])
            assert json["streamableHttp"] == {
                "url": "https://mcp.deepwiki.com/mcp",
                "secretBindings": [],
            }
            return _Response(200, self._server_public(enabled=True))
        if method == "POST" and path.endswith("/connection-test"):
            return _Response(200, {
                "id": self.server_id,
                "status": "healthy",
                "serverName": "DeepWiki",
                "serverVersion": "test",
                "toolCount": 1,
                "tools": [self._tool()],
                "errorCode": "",
            })
        if method == "GET" and path.endswith("/tools"):
            if self.disabled:
                return _Response(503, {"detail": {"code": "MCP_SERVER_DISABLED"}})
            return _Response(200, {
                "serverId": self.server_id,
                "serverName": "DeepWiki",
                "serverVersion": "test",
                "tools": [self._tool()],
            })
        if method == "POST" and path.endswith("/call"):
            assert headers is not None and json is not None
            assert headers["X-Request-Id"] == json["requestId"]
            assert headers["X-Correlation-Id"] == json["correlationId"]
            assert headers["X-Conversation-Id"] == json["conversationId"]
            assert headers["X-Agent-Run-Id"] == json["runId"]
            assert headers["X-ActionDesign-Api-Key"] == "session-credential-probe"
            assert json["toolName"] == "read_wiki_structure"
            assert json["arguments"] == {"repoName": "modelcontextprotocol/python-sdk"}
            if self.fail_call:
                return _Response(502, {"detail": {"code": "MCP_TRANSPORT_ERROR"}})
            return _Response(200, {
                "conversationId": json["conversationId"],
                "runId": json["runId"],
                "requestId": json["requestId"],
                "correlationId": json["correlationId"],
                "serverId": self.server_id,
                "serverName": "DeepWiki",
                "serverVersion": "test",
                "toolName": "read_wiki_structure",
                "result": {
                    "content": [{"type": "text", "text": "public structure"}],
                    "structuredContent": None,
                    "isError": False,
                    "_meta": {},
                },
                "durationMs": 1,
            })
        raise AssertionError(f"unexpected request: {method} {path}")

    def _secret_public(self) -> dict[str, Any]:
        payload = {
            "id": self.secret_id,
            "kind": "mcp-live-canary",
            "displayName": "MCP Live Secret Canary",
            "configured": self.secret_configured,
            "createdAt": "2026-07-30T00:00:00Z",
            "updatedAt": "2026-07-30T00:00:00Z",
            "rotatedAt": "",
            "revokedAt": "" if self.secret_configured else "2026-07-30T00:00:01Z",
        }
        if self.leak_masked_field:
            payload["masked"] = "****probe"
        return payload

    def _server_public(self, *, enabled: bool) -> dict[str, Any]:
        payload = {
            "id": self.server_id,
            "displayName": "DeepWiki Live Verification",
            "transport": "streamable_http",
            "expectedServerName": "DeepWiki",
            "timeoutSeconds": 30.0,
            "enabled": enabled,
            "source": "managed",
            "proxyEnabled": True,
            "browserConfigurable": True,
            "secretBindingStatus": [],
        }
        if self.leak_mcp_transport:
            payload["streamableHttp"] = {"url": "https://mcp.deepwiki.com/mcp"}
        return payload


def _load_script() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT_PATH), run_name="actiondesign_mcp_live_test")


def _set_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "ACTIONDESIGN_GATEWAY_BASE_URL",
        "https://gateway.example.test/api/actiondesign-agent",
    )
    monkeypatch.setenv("ACTIONDESIGN_ADMIN_PASSWORD", "admin-password-probe")
    monkeypatch.delenv("ACTIONDESIGN_MCP_LIVE_REPOSITORY", raising=False)


def test_mcp_live_verifier_returns_2_when_required_environment_is_missing(
    monkeypatch,
    capsys,
):
    script = _load_script()
    monkeypatch.delenv("ACTIONDESIGN_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("ACTIONDESIGN_ADMIN_PASSWORD", raising=False)

    exit_code = script["main"]([], client_factory=lambda **_kwargs: _FakeClient())
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["status"] == "blocked"
    assert "ACTIONDESIGN_GATEWAY_BASE_URL" in output["reason"]
    assert "ACTIONDESIGN_ADMIN_PASSWORD" in output["reason"]


def test_mcp_live_verifier_uses_session_identity_and_deletes_temporary_server(
    monkeypatch,
    capsys,
):
    script = _load_script()
    _set_environment(monkeypatch)
    client = _FakeClient()

    exit_code = script["main"](
        ["--secret-canary"],
        client_factory=lambda **_kwargs: client,
    )
    raw_output = capsys.readouterr().out
    output = json.loads(raw_output)

    assert exit_code == 0
    assert output["status"] == "passed"
    assert output["readWikiStructureCalled"] is True
    assert output["disabledRejected"] is True
    assert output["restored"] is True
    assert output["deleted"] is True
    assert output["secretReferenceCount"] == 0
    assert output["secretCanaryVerified"] is True
    assert client.deleted is True
    assert "admin-password-probe" not in raw_output
    assert "session-credential-probe" not in raw_output
    assert client.secret_configured is False
    assert len(client.secret_values) == 2
    assert all(value not in raw_output for value in client.secret_values)
    assert '"masked"' not in raw_output
    lifecycle = [
        (call["method"], call["path"].rsplit("/", 1)[-1])
        for call in client.calls
        if call["method"] in {"PUT", "DELETE"}
    ]
    assert lifecycle[-3:] == [
        ("PUT", "enabled"),
        ("PUT", "enabled"),
        ("DELETE", client.server_id),
    ]


def test_mcp_live_verifier_cleans_up_after_runtime_failure(monkeypatch, capsys):
    script = _load_script()
    _set_environment(monkeypatch)
    client = _FakeClient(fail_call=True)

    exit_code = script["main"]([], client_factory=lambda **_kwargs: client)
    raw_output = capsys.readouterr().out
    output = json.loads(raw_output)

    assert exit_code == 1
    assert output == {
        "status": "failed",
        "reason": "tools/call returned HTTP 502 (MCP_TRANSPORT_ERROR)",
    }
    assert client.deleted is True
    assert "admin-password-probe" not in raw_output
    assert "session-credential-probe" not in raw_output


def test_mcp_live_verifier_revokes_canary_when_public_response_exposes_masked_field(
    monkeypatch,
    capsys,
):
    script = _load_script()
    _set_environment(monkeypatch)
    client = _FakeClient(leak_masked_field=True)

    exit_code = script["main"](
        ["--secret-canary"],
        client_factory=lambda **_kwargs: client,
    )
    raw_output = capsys.readouterr().out
    output = json.loads(raw_output)

    assert exit_code == 1
    assert output["status"] == "failed"
    assert "exposed a masked Secret field" in output["reason"]
    assert "cleanup failed" in output["reason"]
    assert client.secret_configured is False
    assert all(value not in raw_output for value in client.secret_values)


def test_mcp_live_verifier_rejects_runtime_policy_mcp_target_disclosure(
    monkeypatch,
    capsys,
):
    script = _load_script()
    _set_environment(monkeypatch)
    client = _FakeClient(expose_mcp_policy=True)

    exit_code = script["main"]([], client_factory=lambda **_kwargs: client)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output == {
        "status": "failed",
        "reason": "runtime policy exposed MCP target configuration",
    }


def test_mcp_live_verifier_rejects_transport_details_in_create_response(
    monkeypatch,
    capsys,
):
    script = _load_script()
    _set_environment(monkeypatch)
    client = _FakeClient(leak_mcp_transport=True)

    exit_code = script["main"]([], client_factory=lambda **_kwargs: client)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output == {
        "status": "failed",
        "reason": "temporary MCP Server creation exposed MCP transport or Secret fields",
    }
    assert client.deleted is True
