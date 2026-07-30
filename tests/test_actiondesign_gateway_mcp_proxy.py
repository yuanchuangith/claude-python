from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway import mcp_proxy as mcp_proxy_module
from claude_agent_sdk.actiondesign_gateway.mcp_proxy import (
    McpProxy,
    McpProxyError,
    McpServerConfig,
    _ValidatedDnsNetworkBackend,
)
from claude_agent_sdk.actiondesign_gateway.secret_store import ManagedSecretStore


class DictSecretBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, secret_ref: str) -> str | None:
        return self.values.get(secret_ref)

    def set(self, secret_ref: str, secret: str) -> None:
        self.values[secret_ref] = secret

    def delete(self, secret_ref: str) -> None:
        self.values.pop(secret_ref, None)


@pytest.fixture(autouse=True)
def _stable_private_mcp_dns(monkeypatch):
    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (ipaddress.ip_address("10.20.30.40"),),
    )


class FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, mode, by_alias):
        assert mode == "json"
        assert by_alias is True
        return self.payload


class FakeSession:
    def __init__(self, tools, result=None):
        self.tools = tools
        self.result = result or FakeResult({"content": [], "isError": False})
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def make_secret_store(tmp_path, value="mcp-secret-value"):
    backend = DictSecretBackend()
    store = ManagedSecretStore(tmp_path / "secret-metadata.json", backend)
    store.upsert(
        {
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP key",
            "value": value,
        }
    )
    return store, backend


def create_stdio_files(tmp_path):
    root = (tmp_path / "mcp-root").resolve()
    root.mkdir()
    command = root / "mcp-server.exe"
    command.write_text("placeholder", encoding="utf-8")
    server_script = root / "server.py"
    server_script.write_text("# placeholder", encoding="utf-8")
    return root, command, server_script


def stdio_config(command: Path, server_script: Path):
    return {
        "id": "local-mcp",
        "displayName": "Local MCP",
        "transport": "stdio",
        "stdio": {
            "command": str(command),
            "args": [str(server_script)],
            "cwd": str(command.parent),
            "secretBindings": [
                {
                    "secretId": "mcp.primary",
                    "target": "env",
                    "name": "MCP_API_KEY",
                }
            ],
        },
        "expectedServerName": "local-server",
    }


def http_config():
    return {
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
        "expectedServerName": "remote-server",
    }


def make_proxy(tmp_path):
    secret_store, backend = make_secret_store(tmp_path)
    root, command, server_script = create_stdio_files(tmp_path)
    proxy = McpProxy(
        [stdio_config(command, server_script), http_config()],
        secret_store,
        allowed_stdio_paths=[command],
        allowed_http_urls=["https://mcp.example.test/mcp"],
    )
    return proxy, secret_store, backend


def test_preconfigured_transports_enforce_https_exact_url_and_absolute_path_allowlists(
    tmp_path,
):
    secret_store, _ = make_secret_store(tmp_path)
    root, command, server_script = create_stdio_files(tmp_path)

    proxy = McpProxy(
        [stdio_config(command, server_script), http_config()],
        secret_store,
        allowed_stdio_paths=[command],
        allowed_http_urls=["https://mcp.example.test/mcp"],
    )
    assert proxy.list_servers() == [
        {
            "id": "local-mcp",
            "displayName": "Local MCP",
            "transport": "stdio",
            "enabled": True,
        },
        {
            "id": "remote-mcp",
            "displayName": "Remote MCP",
            "transport": "streamable_http",
            "enabled": True,
        },
    ]

    bad_http = http_config()
    bad_http["streamableHttp"]["url"] = "http://mcp.example.test/mcp"
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(bad_http)

    query_http = http_config()
    query_http["streamableHttp"]["url"] = "https://mcp.example.test/mcp?token=x"
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(query_http)

    crossed_http = http_config()
    crossed_http["streamableHttp"]["url"] = "https://mcp.example.test/other"
    with pytest.raises(ValueError, match="non-global"):
        McpProxy(
            [crossed_http],
            secret_store,
            allowed_http_urls=[
                "https://mcp.example.test/mcp",
                "https://other.example.test/other",
            ],
        )
    with pytest.raises(ValueError, match="non-global"):
        McpProxy(
            [http_config()],
            secret_store,
            allowed_http_urls=["https://other.example.test/mcp"],
        )

    outside = tmp_path / "outside.exe"
    outside.write_text("placeholder", encoding="utf-8")
    outside_config = stdio_config(outside.resolve(), server_script)
    with pytest.raises(ValueError, match="executable is not explicitly allowlisted"):
        McpProxy(
            [outside_config],
            secret_store,
            allowed_stdio_paths=[command],
        )


def test_secret_injection_is_transport_scoped_and_http_redirects_are_disabled(
    tmp_path,
    monkeypatch,
):
    proxy, _, _ = make_proxy(tmp_path)
    monkeypatch.setenv("LANGSMITH_API_KEY", "must-not-be-inherited")

    stdio_server = proxy._servers["local-mcp"]
    parameters, stdio_secrets = proxy._stdio_parameters(stdio_server)
    assert parameters.env["MCP_API_KEY"] == "mcp-secret-value"
    assert "LANGSMITH_API_KEY" not in parameters.env
    assert stdio_secrets == ("mcp-secret-value",)

    http_server = proxy._servers["remote-mcp"]
    url, headers, http_secrets = proxy._http_parameters(http_server)
    assert url == "https://mcp.example.test/mcp"
    assert headers == {
        "X-Client-Version": "1",
        "Authorization": "Bearer mcp-secret-value",
    }
    assert http_secrets == ("mcp-secret-value",)
    client = proxy._create_http_client(http_server, headers)
    try:
        assert client.follow_redirects is False
        assert client._trust_env is False
    finally:
        anyio.run(client.aclose)


def test_public_https_dns_must_resolve_only_global_addresses(tmp_path, monkeypatch):
    secret_store, _ = make_secret_store(tmp_path)
    public = http_config()
    public["streamableHttp"]["url"] = "https://public.example.test/mcp"

    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_sync",
        lambda _host, _port: (
            ipaddress.ip_address("8.8.8.8"),
            ipaddress.ip_address("2606:4700:4700::1111"),
        ),
    )
    proxy = McpProxy([public], secret_store)
    assert proxy.list_servers()[0]["id"] == "remote-mcp"

    rejected_sets = [
        ("8.8.8.8", "10.0.0.1"),
        ("127.0.0.1",),
        ("169.254.10.20",),
        ("224.0.0.1",),
        ("0.0.0.0",),
        ("::ffff:10.0.0.1",),
    ]
    for raw_addresses in rejected_sets:
        monkeypatch.setattr(
            mcp_proxy_module,
            "_resolve_host_addresses_sync",
            lambda _host, _port, values=raw_addresses: tuple(
                ipaddress.ip_address(value) for value in values
            ),
        )
        with pytest.raises(ValueError, match="non-global"):
            McpProxy([public], secret_store)


def test_network_backend_re_resolves_and_connects_only_validated_ip(monkeypatch):
    resolutions = [
        (ipaddress.ip_address("8.8.8.8"),),
        (ipaddress.ip_address("1.1.1.1"),),
    ]
    resolve_calls: list[tuple[str, int]] = []

    async def resolve(host, port):
        resolve_calls.append((host, port))
        return resolutions[len(resolve_calls) - 1]

    class FakeBackend:
        def __init__(self):
            self.connections = []

        async def connect_tcp(self, host, port, **kwargs):
            self.connections.append((host, port, kwargs))
            return SimpleNamespace()

        async def sleep(self, _seconds):
            return None

    monkeypatch.setattr(mcp_proxy_module, "_resolve_host_addresses_async", resolve)
    backend = _ValidatedDnsNetworkBackend(
        expected_host="public.example.test",
        expected_port=443,
        allow_private=False,
    )
    fake = FakeBackend()
    backend._backend = fake

    async def connect_twice():
        await backend.connect_tcp("public.example.test", 443)
        await backend.connect_tcp("public.example.test", 443)

    anyio.run(connect_twice)
    assert resolve_calls == [
        ("public.example.test", 443),
        ("public.example.test", 443),
    ]
    assert [item[0] for item in fake.connections] == ["8.8.8.8", "1.1.1.1"]

    async def resolve_private(_host, _port):
        return (ipaddress.ip_address("192.168.1.2"),)

    monkeypatch.setattr(
        mcp_proxy_module,
        "_resolve_host_addresses_async",
        resolve_private,
    )
    with pytest.raises(ValueError, match="non-global"):
        anyio.run(backend.connect_tcp, "public.example.test", 443)

    private_backend = _ValidatedDnsNetworkBackend(
        expected_host="private.example.test",
        expected_port=443,
        allow_private=True,
    )
    private_fake = FakeBackend()
    private_backend._backend = private_fake
    anyio.run(private_backend.connect_tcp, "private.example.test", 443)
    assert private_fake.connections[0][0] == "192.168.1.2"


def test_static_secret_headers_and_cross_transport_bindings_are_rejected():
    static_secret = http_config()
    static_secret["streamableHttp"]["headers"] = {"Authorization": "plaintext"}
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(static_secret)

    wrong_target = http_config()
    wrong_target["streamableHttp"]["secretBindings"][0]["target"] = "env"
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(wrong_target)

    coerced = http_config()
    coerced["enabled"] = "true"
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(coerced)


def test_list_tools_validates_and_passes_through_draft_2020_12_schemas(tmp_path):
    proxy, _, _ = make_proxy(tmp_path)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "apiKey": {"type": "string", "description": "A caller field, not a proxy Secret"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"items": {"type": "array"}},
    }
    tool = SimpleNamespace(
        name="search",
        description="Generic search transport test",
        inputSchema=schema,
        outputSchema=output_schema,
    )
    session = FakeSession([tool])

    @asynccontextmanager
    async def fake_session(_server):
        yield session, {"serverName": "remote-server", "serverVersion": "1.0"}, ()

    proxy._session = fake_session
    response = anyio.run(proxy.list_tools, "remote-mcp")
    assert response["tools"] == [
        {
            "name": "search",
            "description": "Generic search transport test",
            "inputSchema": schema,
            "outputSchema": output_schema,
        }
    ]


def test_call_tool_uses_caller_choice_validates_arguments_and_does_not_interpret_result(
    tmp_path,
):
    proxy, _, _ = make_proxy(tmp_path)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    }
    tool = SimpleNamespace(name="search", description="", inputSchema=schema)
    raw_result = {
        "content": [
            {
                "type": "text",
                "text": "server echoed mcp-secret-value without business normalization",
            }
        ],
        "structuredContent": {"arbitrary": [1, 2, 3]},
        "isError": False,
        "_meta": None,
    }
    session = FakeSession([tool], FakeResult(raw_result))

    @asynccontextmanager
    async def fake_session(_server):
        yield (
            session,
            {"serverName": "remote-server", "serverVersion": "1.0"},
            ("mcp-secret-value",),
        )

    proxy._session = fake_session
    response = anyio.run(
        proxy.call_tool,
        "remote-mcp",
        "search",
        {"query": "hello"},
    )
    assert session.calls == [("search", {"query": "hello"})]
    assert response["result"]["structuredContent"] == {"arbitrary": [1, 2, 3]}
    assert response["result"]["isError"] is False
    assert response["result"]["_meta"] == {}
    assert "mcp-secret-value" not in str(response)
    assert "[REDACTED]" in response["result"]["content"][0]["text"]
    assert "success" not in response

    with pytest.raises(McpProxyError) as exc_info:
        anyio.run(proxy.call_tool, "remote-mcp", "search", {"query": 123})
    assert exc_info.value.code == "MCP_ARGUMENTS_INVALID"
    assert session.calls == [("search", {"query": "hello"})]


def test_invalid_schema_bounded_json_unknown_server_and_errors_do_not_echo_secrets(
    tmp_path,
):
    proxy, _, _ = make_proxy(tmp_path)
    invalid_tool = SimpleNamespace(
        name="search",
        description="",
        inputSchema={"type": "not-a-json-schema-type"},
    )
    session = FakeSession([invalid_tool])

    @asynccontextmanager
    async def invalid_schema_session(_server):
        yield session, {"serverName": "remote-server", "serverVersion": "1.0"}, ()

    proxy._session = invalid_schema_session
    with pytest.raises(McpProxyError) as exc_info:
        anyio.run(proxy.list_tools, "remote-mcp")
    assert exc_info.value.code == "MCP_TOOL_SCHEMA_INVALID"

    with pytest.raises(McpProxyError) as exc_info:
        anyio.run(proxy.list_tools, "not-configured")
    assert exc_info.value.code == "MCP_SERVER_NOT_FOUND"

    valid_tool = SimpleNamespace(
        name="search",
        description="",
        inputSchema={"type": "object", "properties": {}},
    )

    @asynccontextmanager
    async def secret_error_session(_server):
        try:
            raise RuntimeError("Authorization: Bearer mcp-secret-value")
        except Exception as exc:
            raise McpProxyError(
                "MCP_TRANSPORT_ERROR",
                f"MCP transport failed ({type(exc).__name__})",
            ) from exc
        yield FakeSession([valid_tool]), {}, ("mcp-secret-value",)

    proxy._session = secret_error_session
    with pytest.raises(McpProxyError) as exc_info:
        anyio.run(proxy.list_tools, "remote-mcp")
    assert "mcp-secret-value" not in str(exc_info.value)

    deep = {}
    cursor = deep
    for _ in range(40):
        cursor["next"] = {}
        cursor = cursor["next"]

    @asynccontextmanager
    async def valid_session(_server):
        yield FakeSession([valid_tool]), {}, ()

    proxy._session = valid_session
    with pytest.raises(McpProxyError) as exc_info:
        anyio.run(proxy.call_tool, "remote-mcp", "search", deep)
    assert exc_info.value.code == "MCP_JSON_INVALID"


def test_tool_list_limits_match_public_response_model(tmp_path):
    proxy, _, _ = make_proxy(tmp_path)
    valid_schema = {"type": "object", "properties": {}}
    too_many = [
        SimpleNamespace(
            name=f"tool_{index}",
            description="",
            inputSchema=valid_schema,
            outputSchema=None,
        )
        for index in range(257)
    ]
    oversized_description = [
        SimpleNamespace(
            name="tool_one",
            description="x" * 8_001,
            inputSchema=valid_schema,
            outputSchema=None,
        )
    ]

    with pytest.raises(McpProxyError, match="tool list exceeds"):
        proxy._validated_tools(too_many)
    with pytest.raises(McpProxyError, match="metadata is invalid"):
        proxy._validated_tools(oversized_description)
