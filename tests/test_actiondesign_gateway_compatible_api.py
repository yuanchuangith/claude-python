from __future__ import annotations

import ipaddress
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway import app as gateway_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway import (
    mcp_proxy as mcp_proxy_module,  # noqa: E402
)
from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.mcp_proxy import McpProxy  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.secret_store import (  # noqa: E402
    ManagedSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402

CONVERSATION_ID = "77777777-7777-4777-8777-777777777777"
RUN_ID = "run_77777777-7777-4777-8777-777777777777"
REQUEST_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"
ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}


class _DictSecretBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, secret_ref: str) -> str | None:
        return self.values.get(secret_ref)

    def set(self, secret_ref: str, secret: str) -> None:
        self.values[secret_ref] = secret

    def delete(self, secret_ref: str) -> None:
        self.values.pop(secret_ref, None)


class _FakeMcpResult:
    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
        assert mode == "json"
        assert by_alias is True
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class _FakeMcpSession:
    def __init__(self) -> None:
        self.tool = SimpleNamespace(
            name="search",
            description="Search through the configured MCP server",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[self.tool])

    async def call_tool(
        self, _name: str, _arguments: dict[str, object]
    ) -> _FakeMcpResult:
        return _FakeMcpResult()


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "default_provider": "mimo",
        "allow_origins": ["*"],
        "log_root": tmp_path / "audit",
        "full_conversation_log_enabled": True,
        "full_conversation_log_root": tmp_path / "conversations",
        "request_log_path": tmp_path / "request-logs.jsonl",
        "mimo_api_key": "mimo-provider-secret",
        "mimo_messages_url": "https://provider.example.test/v1/messages",
        "mimo_default_model": "mimo-v2.5",
        "claude_code_api_key": "claude-provider-secret",
        "claude_code_messages_url": "https://claude.example.test/v1/messages",
        "claude_code_default_model": "claude-sonnet-4-5",
        "claude_code_models": ["claude-sonnet-4-5"],
        "profile_store_path": tmp_path / "profiles.json",
        "managed_secret_store_path": tmp_path / "managed-secrets.json",
        "mcp_server_store_path": tmp_path / "mcp-servers.json",
        "browser_api_key_store_path": tmp_path / "browser-api-key.json",
        "client_api_key_store_path": tmp_path / "client-api-keys.json",
        "profile_admin_token": "admin-token",
        "admin_allow_remote": True,
        "admin_require_https_remote": False,
        "profile_allowed_hosts": [
            "provider.example.test",
            "claude.example.test",
        ],
        "langsmith_enabled": False,
        "langsmith_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def _client(tmp_path, **overrides) -> TestClient:
    return TestClient(
        create_app(
            _settings(tmp_path, **overrides),
            profile_secret_store=MemoryProfileSecretStore(),
        )
    )


def _create_key(
    client: TestClient, name: str = "Compatibility client"
) -> dict[str, object]:
    response = client.post(
        "/admin/client-api-keys",
        headers=ADMIN_HEADERS,
        json={"name": name},
    )
    assert response.status_code == 201
    return response.json()


def _provider_result(req, content: str = "compatible response") -> dict[str, object]:
    return {
        "provider": "mimo",
        "model": req.model or "mimo-v2.5",
        "content": content,
        "tool_calls": [],
        "success": True,
        "error": None,
        "code": None,
        "duration_ms": 2,
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "finish_reason": "stop",
        },
    }


def test_client_api_key_full_value_is_one_time_and_cannot_administer_keys(tmp_path):
    client = _client(tmp_path)
    created = _create_key(client)
    key = str(created["key"])

    assert key.startswith(f"adk_{created['id']}_")
    assert key.startswith(str(created["prefix"]))

    listed = client.get("/admin/client-api-keys", headers=ADMIN_HEADERS)
    alias_listed = client.get(
        "/api/actiondesign-agent/admin/client-api-keys",
        headers=ADMIN_HEADERS,
    )
    assert listed.status_code == alias_listed.status_code == 200
    assert listed.json() == alias_listed.json()
    assert listed.json()["items"] == [
        {
            "id": created["id"],
            "name": created["name"],
            "prefix": created["prefix"],
            "createdAt": created["createdAt"],
            "lastUsedAt": None,
        }
    ]
    assert "key" not in listed.text.lower()
    assert key not in listed.text

    client_auth = {"Authorization": f"Bearer {key}"}
    forbidden_create = client.post(
        "/admin/client-api-keys",
        headers=client_auth,
        json={"name": "must not be created"},
    )
    forbidden_delete = client.delete(
        f"/admin/client-api-keys/{created['id']}",
        headers=client_auth,
    )
    assert forbidden_create.status_code == forbidden_delete.status_code == 401
    assert len(client.app.state.client_api_keys.list_public()) == 1


def test_client_api_key_requires_exact_match_and_revocation_is_immediate(tmp_path):
    client = _client(tmp_path)
    created = _create_key(client)
    key = str(created["key"])
    changed_last = "A" if key[-1] != "A" else "B"
    near_match = key[:-1] + changed_last

    accepted = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    rejected = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {near_match}"},
    )
    deleted = client.delete(
        f"/admin/client-api-keys/{created['id']}",
        headers=ADMIN_HEADERS,
    )
    revoked = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})

    assert accepted.status_code == 200
    assert accepted.json()["object"] == "list"
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "CLIENT_API_KEY_INVALID"
    assert deleted.status_code == 200
    assert deleted.json() == {"keyId": created["id"], "deleted": True}
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "CLIENT_API_KEY_INVALID"


def test_client_key_headers_models_access_and_admin_boundaries(tmp_path):
    client = _client(tmp_path)
    key = str(_create_key(client)["key"])
    client_headers = [
        {"Authorization": f"Bearer {key}"},
        {"x-api-key": key},
        {"X-ActionDesign-Api-Key": key},
    ]

    for headers in client_headers:
        response = client.get("/api/actiondesign-agent/models", headers=headers)
        assert response.status_code == 200
        assert response.json()["routingOrder"]

    admin_models = client.get(
        "/api/actiondesign-agent/models",
        headers=ADMIN_HEADERS,
    )
    conflicting = client.get(
        "/api/actiondesign-agent/models",
        headers={
            "Authorization": f"Bearer {key}",
            "x-api-key": f"{key[:-1]}{'A' if key[-1] != 'A' else 'B'}",
        },
    )
    malformed_authorization = client.get(
        "/api/actiondesign-agent/models",
        headers={"Authorization": "Basic invalid", "x-api-key": key},
    )
    assert admin_models.status_code == 200
    assert conflicting.status_code == 401
    assert conflicting.json()["detail"]["code"] == "CLIENT_API_KEY_REQUIRED"
    assert malformed_authorization.status_code == 401
    assert malformed_authorization.json()["detail"]["code"] == "CLIENT_API_KEY_REQUIRED"

    forbidden_paths = [
        "/api/actiondesign-agent/admin/profiles",
        "/api/actiondesign-agent/admin/providers",
        "/api/actiondesign-agent/admin/client-api-keys",
        "/api/actiondesign-agent/admin/request-logs",
        "/api/actiondesign-agent/admin/audit-logs",
    ]
    for path in forbidden_paths:
        response = client.get(path, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code in {401, 403}, path


def test_client_key_cors_is_limited_to_the_configured_actiondesign_origin(tmp_path):
    allowed_origin = "http://cpm.rongchang.gxp2.com:51124"
    client = _client(tmp_path, allow_origins=[allowed_origin])

    accepted = client.options(
        "/api/actiondesign-agent/models",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    rejected = client.options(
        "/api/actiondesign-agent/models",
        headers={
            "Origin": "http://untrusted.example.test",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert accepted.status_code == 200
    assert accepted.headers["access-control-allow-origin"] == allowed_origin
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers


def test_client_key_mcp_config_server_and_secret_lifecycle_is_redacted(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (ipaddress.ip_address("10.20.30.40"),),
    )
    client = _client(
        tmp_path,
        mcp_allowed_http_urls=["https://mcp.example.test/mcp"],
    )
    key = str(_create_key(client, "ActionDesign MCP client")["key"])
    headers = {"Authorization": f"Bearer {key}"}
    prefix = "/api/actiondesign-agent/mcp-config"

    secret = client.put(
        f"{prefix}/secrets/mcp-client-secret",
        headers=headers,
        json={
            "id": "mcp-client-secret",
            "kind": "mcp_key",
            "displayName": "MCP client secret",
            "value": "initial-secret-value",
        },
    )
    assert secret.status_code == 200
    assert secret.json()["configured"] is True
    assert "value" not in secret.json()
    assert "masked" not in secret.json()

    server_payload = {
        "id": "client-managed-mcp",
        "displayName": "Client managed MCP",
        "transport": "streamable_http",
        "streamableHttp": {
            "url": "https://mcp.example.test/mcp",
            "headers": {},
            "secretBindings": [
                {
                    "secretId": "mcp-client-secret",
                    "target": "header",
                    "name": "Authorization",
                    "valuePrefix": "Bearer ",
                }
            ],
            "sseReadTimeoutSeconds": 30,
        },
        "expectedServerName": "",
        "timeoutSeconds": 30,
        "executionMode": "gateway_proxy",
        "authMode": "backend_secret",
        "enabled": False,
    }
    saved = client.put(
        f"{prefix}/servers/client-managed-mcp",
        headers=headers,
        json=server_payload,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["secretBindingStatus"] == [
        {"secretId": "mcp-client-secret", "configured": True}
    ]
    for sensitive_key in (
        "streamableHttp",
        "stdio",
        "url",
        "headers",
        "command",
        "cwd",
        "args",
        "secretBindings",
    ):
        assert sensitive_key not in saved.json()

    listed_servers = client.get(f"{prefix}/servers", headers=headers)
    listed_secrets = client.get(f"{prefix}/secrets", headers=headers)
    connection = client.post(
        f"{prefix}/servers/client-managed-mcp/connection-test",
        headers=headers,
    )
    disabled = client.put(
        f"{prefix}/servers/client-managed-mcp/enabled",
        headers=headers,
        json={"enabled": False},
    )
    rotated = client.post(
        f"{prefix}/secrets/mcp-client-secret/rotate",
        headers=headers,
        json={"value": "rotated-secret-value"},
    )
    assert listed_servers.status_code == listed_secrets.status_code == 200
    assert connection.status_code == 200
    assert connection.json()["status"] in {"healthy", "unhealthy"}
    assert disabled.status_code == 200
    assert rotated.status_code == 200
    assert "value" not in listed_secrets.text
    assert "masked" not in listed_secrets.text

    deleted_server = client.delete(
        f"{prefix}/servers/client-managed-mcp",
        headers=headers,
    )
    revoked_secret = client.delete(
        f"{prefix}/secrets/mcp-client-secret",
        headers=headers,
    )
    assert deleted_server.status_code == 200
    assert deleted_server.json()["deleted"] is True
    assert revoked_secret.status_code == 200
    assert revoked_secret.json()["configured"] is False
    assert "value" not in revoked_secret.json()
    assert "masked" not in revoked_secret.json()


def test_openai_and_anthropic_nonstream_share_existing_provider_chain_without_key_leak(
    tmp_path,
    monkeypatch,
):
    captured = []

    async def fake_call(req, _profile):
        captured.append(req)
        return _provider_result(req)

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    client = _client(tmp_path)
    key = str(_create_key(client)["key"])

    openai = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "mimo-v2.5",
            "messages": [
                {"role": "system", "content": "Keep the response concise."},
                {"role": "user", "content": "OpenAI request"},
            ],
        },
    )
    anthropic = client.post(
        "/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "mimo-v2.5",
            "max_tokens": 256,
            "system": "Keep the response concise.",
            "messages": [{"role": "user", "content": "Anthropic request"}],
        },
    )

    assert openai.status_code == anthropic.status_code == 200
    assert openai.json()["object"] == "chat.completion"
    assert openai.json()["choices"][0]["message"]["content"] == "compatible response"
    assert anthropic.json()["type"] == "message"
    assert anthropic.json()["content"] == [
        {"type": "text", "text": "compatible response"}
    ]
    assert [request.prompt for request in captured] == [
        "OpenAI request",
        "Anthropic request",
    ]
    assert all(
        key not in request.model_dump_json(by_alias=True) for request in captured
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    )
    assert key not in persisted


def test_compatible_requests_use_backend_default_only_when_model_is_omitted(
    tmp_path,
    monkeypatch,
):
    captured = []

    async def fake_call(req, _profile):
        captured.append(req)
        return _provider_result(req)

    monkeypatch.setattr(gateway_app, "call_provider", fake_call)
    client = _client(tmp_path)
    key = str(_create_key(client)["key"])

    openai_default = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"messages": [{"role": "user", "content": "Use backend default"}]},
    )
    anthropic_default = client.post(
        "/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Use backend default"}],
        },
    )
    explicitly_selected = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Use selected model"}],
        },
    )

    assert openai_default.status_code == 200
    assert anthropic_default.status_code == 200
    assert explicitly_selected.status_code == 200
    assert [request.model for request in captured] == [
        "mimo-v2.5",
        "mimo-v2.5",
        "mimo-v2.5-pro",
    ]
    assert openai_default.headers["x-actual-model"] == "mimo-v2.5"
    assert anthropic_default.headers["x-actual-model"] == "mimo-v2.5"
    assert explicitly_selected.headers["x-actual-model"] == "mimo-v2.5-pro"


def test_openai_and_anthropic_streaming_wire_formats(tmp_path, monkeypatch):
    async def fake_stream(req, _profile):
        yield {"kind": "delta", "content": "streamed "}
        yield {"kind": "delta", "content": "response"}
        yield {
            "kind": "completed",
            "payload": _provider_result(req, "streamed response"),
        }

    monkeypatch.setattr(gateway_app, "stream_provider", fake_stream)
    client = _client(tmp_path)
    key = str(_create_key(client)["key"])

    openai = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "mimo-v2.5",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Stream with OpenAI"}],
        },
    )
    anthropic = client.post(
        "/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": "mimo-v2.5",
            "max_tokens": 256,
            "stream": True,
            "messages": [{"role": "user", "content": "Stream with Anthropic"}],
        },
    )

    assert openai.status_code == anthropic.status_code == 200
    assert openai.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in openai.text
    openai_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in openai.text.splitlines()
        if line.startswith("data: {")
    ]
    assert openai_payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert any(
        item.get("choices") == [] and item.get("usage", {}).get("total_tokens") == 5
        for item in openai_payloads
    )

    anthropic_events = [
        line.removeprefix("event: ")
        for line in anthropic.text.splitlines()
        if line.startswith("event: ")
    ]
    assert anthropic_events == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert '"type":"text_delta","text":"streamed "' in anthropic.text
    assert '"type":"message_stop"' in anthropic.text


def test_mcp_proxy_uses_client_key_only_for_gateway_auth_and_independent_mcp_secret(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (ipaddress.ip_address("8.8.8.8"),),
    )
    client = _client(
        tmp_path,
        mcp_proxy_enabled=True,
        service_auth_mode="required",
        service_auth_token="service-token-" + ("x" * 32),
        allow_origins=["https://app.example.test"],
    )
    client_key = str(_create_key(client)["key"])
    mcp_secret = "mcp-upstream-secret-value"
    backend = _DictSecretBackend()
    secret_store = ManagedSecretStore(tmp_path / "mcp-secret-metadata.json", backend)
    secret_store.upsert(
        {
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP secret",
            "value": mcp_secret,
        }
    )
    proxy = McpProxy(
        [
            {
                "id": "remote-mcp",
                "displayName": "Remote MCP",
                "transport": "streamable_http",
                "streamableHttp": {
                    "url": "https://mcp.example.test/mcp",
                    "headers": {"X-Client-Version": "1"},
                    "secretBindings": [
                        {
                            "secretId": "mcp.primary",
                            "target": "header",
                            "name": "Authorization",
                            "valuePrefix": "Bearer ",
                        }
                    ],
                },
            }
        ],
        secret_store,
    )
    captured = {}
    session = _FakeMcpSession()

    @asynccontextmanager
    async def fake_session(server):
        url, headers, secret_values = proxy._http_parameters(server)
        captured.update(url=url, headers=headers, secret_values=secret_values)
        yield session, {"serverName": "remote", "serverVersion": "1.0"}, secret_values

    proxy._session = fake_session
    client.app.state.mcp_proxy = proxy
    headers = {
        "X-ActionDesign-Api-Key": client_key,
        "X-Request-Id": REQUEST_ID,
        "X-Correlation-Id": CORRELATION_ID,
    }

    response = client.get(
        "/api/actiondesign-agent/mcp/remote-mcp/tools",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["tools"][0]["name"] == "search"
    assert captured == {
        "url": "https://mcp.example.test/mcp",
        "headers": {
            "X-Client-Version": "1",
            "Authorization": f"Bearer {mcp_secret}",
        },
        "secret_values": (mcp_secret,),
    }
    assert client_key != mcp_secret
    assert client_key not in json.dumps(captured, ensure_ascii=False)


def test_client_key_reads_only_minimal_mcp_runtime_config_and_has_no_admin_access(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (ipaddress.ip_address("8.8.8.8"),),
    )
    client = _client(
        tmp_path,
        mcp_proxy_enabled=True,
        service_auth_mode="required",
        service_auth_token="service-token-" + ("x" * 32),
        allow_origins=["https://app.example.test"],
    )
    candidate = client.app.state.mcp_server_store.create_upsert_candidate(
        {
            "id": "direct-mcp",
            "displayName": "Direct MCP",
            "transport": "streamable_http",
            "streamableHttp": {
                "url": "https://mcp.example.test/private-runtime-path",
                "headers": {},
                "secretBindings": [],
            },
            "expectedServerName": "private-runtime-name",
            "timeoutSeconds": 17.0,
            "executionMode": "auto",
            "authMode": "none",
            "enabled": True,
        }
    )
    client.app.state.mcp_server_store.commit_candidate(
        candidate,
        client.app.state.mcp_proxy,
        lambda runtime: runtime,
        connection_status="disconnected",
    )
    key = str(_create_key(client)["key"])
    runtime_headers = {
        "X-ActionDesign-Api-Key": key,
        "X-Request-Id": REQUEST_ID,
        "X-Correlation-Id": CORRELATION_ID,
    }

    missing_key = client.get(
        "/api/actiondesign-agent/mcp/direct-mcp/config",
        headers={
            "X-Request-Id": REQUEST_ID,
            "X-Correlation-Id": CORRELATION_ID,
        },
    )
    runtime = client.get(
        "/api/actiondesign-agent/mcp/direct-mcp/config",
        headers=runtime_headers,
    )
    admin = client.get(
        "/api/actiondesign-agent/admin/mcp-servers/direct-mcp",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert missing_key.status_code == 401
    assert runtime.status_code == 200
    assert runtime.json() == {
        "id": "direct-mcp",
        "transport": "streamable_http",
        "executionMode": "auto",
        "authMode": "none",
        "browserDirectUrl": "https://mcp.example.test/private-runtime-path",
        "enabled": True,
        "proxyEnabled": True,
        "configVersion": 1,
        "activeVersion": 1,
        "connectionStatus": "disconnected",
    }
    assert set(runtime.json()) == {
        "id",
        "transport",
        "executionMode",
        "authMode",
        "browserDirectUrl",
        "enabled",
        "proxyEnabled",
        "configVersion",
        "activeVersion",
        "connectionStatus",
    }
    assert "Direct MCP" not in runtime.text
    assert "private-runtime-name" not in runtime.text
    assert "timeoutSeconds" not in runtime.text
    assert admin.status_code == 401
