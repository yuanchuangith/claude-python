from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.mcp_proxy import McpServerConfig
from claude_agent_sdk.actiondesign_gateway.mcp_server_store import (
    McpServerCandidateStaleError,
    McpServerStore,
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


def http_server(
    server_id: str,
    *,
    execution_mode: str | None = None,
    secret_id: str = "",
    display_name: str = "MCP Server",
) -> dict:
    payload = {
        "id": server_id,
        "displayName": display_name,
        "transport": "streamable_http",
        "streamableHttp": {
            "url": f"https://{server_id}.example.test/mcp",
            "secretBindings": (
                [
                    {
                        "secretId": secret_id,
                        "target": "header",
                        "name": "Authorization",
                        "valuePrefix": "Bearer ",
                    }
                ]
                if secret_id
                else []
            ),
        },
        "enabled": True,
    }
    if execution_mode is not None:
        payload["executionMode"] = execution_mode
    if secret_id:
        payload["authMode"] = "backend_secret"
    elif execution_mode is not None:
        payload["authMode"] = "none"
    return payload


def secret_store(tmp_path) -> ManagedSecretStore:
    return ManagedSecretStore(tmp_path / "secrets.json", DictSecretBackend())


def test_hybrid_contract_defaults_to_proxy_and_only_projects_safe_direct_url(tmp_path):
    store = McpServerStore(tmp_path / "servers.json", [])
    runtime = object()

    legacy, _ = store.upsert(
        http_server("legacy"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )
    direct, _ = store.upsert(
        http_server("direct", execution_mode="browser_direct"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )
    automatic, _ = store.upsert(
        http_server("automatic", execution_mode="auto"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )
    secret, _ = store.upsert(
        http_server("secret", secret_id="mcp.secret"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )

    assert legacy.execution_mode == "gateway_proxy"
    assert legacy.auth_mode == "none"
    assert direct.execution_mode == "browser_direct"
    assert automatic.execution_mode == "auto"
    assert secret.execution_mode == "gateway_proxy"
    assert secret.auth_mode == "backend_secret"

    public = {
        item["id"]: item
        for item in store.list_public(secret_store(tmp_path), proxy_enabled=True)
    }
    assert public["legacy"]["browserDirectUrl"] == ""
    assert public["secret"]["browserDirectUrl"] == ""
    assert public["direct"]["browserDirectUrl"] == (
        "https://direct.example.test/mcp"
    )
    assert public["automatic"]["browserDirectUrl"] == (
        "https://automatic.example.test/mcp"
    )
    assert public["direct"]["configVersion"] == 1
    assert public["direct"]["activeVersion"] == 1
    assert public["direct"]["connectionStatus"] == "disconnected"


@pytest.mark.parametrize(
    "payload",
    [
        {
            **http_server("secret-direct", secret_id="mcp.secret"),
            "executionMode": "browser_direct",
        },
        {
            "id": "stdio-auto",
            "displayName": "stdio",
            "transport": "stdio",
            "stdio": {"command": "C:\\tools\\mcp.exe", "secretBindings": []},
            "executionMode": "auto",
            "authMode": "none",
            "enabled": True,
        },
        {
            **http_server("mismatched-auth"),
            "authMode": "backend_secret",
        },
    ],
)
def test_stdio_and_backend_secret_are_forced_to_gateway_proxy(payload):
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(payload)


@pytest.mark.parametrize("execution_mode", ["auto", "browser_direct"])
def test_static_http_headers_force_gateway_proxy(execution_mode):
    payload = http_server("static-headers", execution_mode=execution_mode)
    payload["streamableHttp"]["headers"] = {"X-Client-Version": "1"}

    with pytest.raises(
        ValidationError,
        match="static headers must use gateway_proxy execution",
    ):
        McpServerConfig.model_validate(payload)

    payload["executionMode"] = "gateway_proxy"
    assert McpServerConfig.model_validate(payload).streamable_http.headers == {
        "X-Client-Version": "1"
    }


def test_candidate_failure_keeps_active_then_success_publishes_and_drains(tmp_path):
    path = tmp_path / "servers.json"
    store = McpServerStore(path, [])
    old_runtime = object()
    runtime_holder = {"active": old_runtime}
    saved, _ = store.upsert(
        http_server("managed"),
        lambda _configs: old_runtime,
        lambda _runtime: None,
    )
    assert saved.config_version == 1

    candidate = store.create_upsert_candidate(
        http_server("managed", display_name="Candidate")
    )
    candidate_config = candidate.config("managed")
    assert candidate_config is not None
    assert candidate_config.config_version == 2
    assert store.list_configs()[0].display_name == "MCP Server"

    candidate_runtime = object()

    def failed_publish(_runtime):
        raise RuntimeError("simulated publish failure")

    with pytest.raises(RuntimeError, match="simulated publish failure"):
        store.commit_candidate(candidate, candidate_runtime, failed_publish)

    assert runtime_holder["active"] is old_runtime
    assert store.list_configs()[0].display_name == "MCP Server"
    assert json.loads(path.read_text(encoding="utf-8"))["servers"][0][
        "displayName"
    ] == "MCP Server"

    def publish(runtime):
        previous = runtime_holder["active"]
        runtime_holder["active"] = runtime
        return previous

    transition = store.commit_candidate(candidate, candidate_runtime, publish)
    assert transition.active_runtime is candidate_runtime
    assert transition.draining_runtime is old_runtime
    assert runtime_holder["active"] is candidate_runtime
    assert store.list_configs()[0].display_name == "Candidate"
    public = store.get_public("managed", secret_store(tmp_path), proxy_enabled=True)
    assert public is not None
    assert public["configVersion"] == 2
    assert public["activeVersion"] == 2
    assert public["connectionStatus"] == "healthy"


def test_committed_candidate_makes_parallel_candidate_stale(tmp_path):
    store = McpServerStore(tmp_path / "servers.json", [])
    first = store.create_upsert_candidate(http_server("first"))
    stale = store.create_upsert_candidate(http_server("stale"))
    store.commit_candidate(first, object(), lambda _runtime: None)

    with pytest.raises(McpServerCandidateStaleError):
        store.commit_candidate(stale, object(), lambda _runtime: None)
    assert [server.id for server in store.list_configs()] == ["first"]


def test_secret_candidate_overlay_is_memory_only_and_targets_bound_servers(tmp_path):
    secrets = secret_store(tmp_path)
    secrets.upsert(
        {
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP key",
            "value": "old-secret-value",
        }
    )
    overlay = secrets.candidate_overlay({"mcp.primary": "new-secret-value"})
    assert secrets.resolve_value("mcp.primary") == "old-secret-value"
    assert overlay.resolve_value("mcp.primary") == "new-secret-value"
    assert "new-secret-value" not in repr(overlay)
    assert "new-secret-value" not in secrets.path.read_text(encoding="utf-8")

    store = McpServerStore(tmp_path / "servers.json", [])
    runtime = object()
    store.upsert(
        http_server("bound", secret_id="mcp.primary"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )
    store.upsert(
        http_server("other", secret_id="mcp.other"),
        lambda _configs: runtime,
        lambda _runtime: None,
    )
    candidate = store.create_secret_candidate(overlay.secret_ids)
    assert candidate.server_ids == ("bound",)

    new_runtime = object()
    transition = store.commit_candidate(
        candidate,
        new_runtime,
        lambda _runtime: runtime,
    )
    assert transition.draining_runtime is runtime
    bound = store.get_public("bound", overlay, proxy_enabled=True)
    other = store.get_public("other", overlay, proxy_enabled=True)
    assert bound is not None and other is not None
    assert bound["configVersion"] == 2
    assert bound["activeVersion"] == 2
    assert bound["connectionStatus"] == "healthy"
    assert other["activeVersion"] == 1
    assert other["connectionStatus"] == "disconnected"


@pytest.mark.parametrize("operation", ["upsert", "rotate"])
def test_secret_publish_failure_rollback_restores_metadata_and_value(
    tmp_path,
    operation,
):
    secrets = secret_store(tmp_path)
    secrets.upsert(
        {
            "id": "mcp.primary",
            "kind": "mcp_key",
            "displayName": "Primary MCP key",
            "value": "old-secret-value",
        }
    )
    before = secrets.get("mcp.primary")
    snapshot = secrets.snapshot_for_rollback("mcp.primary")

    if operation == "rotate":
        secrets.rotate("mcp.primary", {"value": "new-secret-value"})
    else:
        secrets.upsert(
            {
                "id": "mcp.primary",
                "kind": "mcp_key",
                "displayName": "Candidate MCP key",
                "value": "new-secret-value",
            }
        )
    assert secrets.resolve_value("mcp.primary") == "new-secret-value"

    restored = secrets.restore_snapshot(snapshot)
    assert restored == before
    assert secrets.get("mcp.primary") == before
    assert secrets.resolve_value("mcp.primary") == "old-secret-value"
    persisted = secrets.path.read_text(encoding="utf-8")
    assert "Candidate MCP key" not in persisted
    assert "old-secret-value" not in persisted
    assert "new-secret-value" not in persisted
    assert "new-secret-value" not in repr(snapshot)


def test_failed_first_secret_publish_can_remove_uncommitted_secret(tmp_path):
    secrets = secret_store(tmp_path)
    snapshot = secrets.snapshot_for_rollback("mcp.new")
    secrets.upsert(
        {
            "id": "mcp.new",
            "kind": "mcp_key",
            "displayName": "Candidate MCP key",
            "value": "candidate-secret-value",
        }
    )

    assert secrets.restore_snapshot(snapshot) is None
    assert secrets.get("mcp.new") is None
    with pytest.raises(KeyError):
        secrets.resolve_value("mcp.new")
    persisted = json.loads(secrets.path.read_text(encoding="utf-8"))
    assert persisted["secrets"] == []
