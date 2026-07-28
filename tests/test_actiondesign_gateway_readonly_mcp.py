import json
import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway import readonly_mcp  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.models import (  # noqa: E402
    ReadonlyMcpToolCallResponse,
)
from claude_agent_sdk.actiondesign_gateway.readonly_mcp import (  # noqa: E402
    GXP_LOWCODE_DEBUG_SKILL_ID,
    GXP_LOWCODE_DEBUG_SKILL_VERSION,
    GXP_READONLY_MCP_CONTRACT_HASH,
    GXP_READONLY_MCP_CONTRACT_VERSION,
    GXP_READONLY_MCP_RESULT_SCHEMAS,
    GXP_READONLY_MCP_TOOLS,
    ReadonlyMcpError,
    ReadonlyMcpGateway,
    _safe_subprocess_env,
    validate_readonly_mcp_result,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


ORIGIN = "https://actiondesign.example.test"
SERVICE_TOKEN = "readonly-mcp-service-token-" + ("x" * 32)
SERVICE_HEADERS = {
    "Origin": ORIGIN,
    "X-ActionDesign-Service-Token": SERVICE_TOKEN,
}
CONVERSATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_ID = "run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def make_settings(tmp_path, **overrides):
    server_path = tmp_path / "server.py"
    server_path.write_text("print('test')", encoding="utf-8")
    values = {
        "default_provider": "mimo",
        "allow_origins": [ORIGIN],
        "log_root": tmp_path / "audit",
        "full_conversation_log_enabled": True,
        "full_conversation_log_root": tmp_path / "conversations",
        "mimo_api_key": "mimo-secret",
        "mimo_messages_url": "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages",
        "mimo_default_model": "mimo-v2.5",
        "claude_code_api_key": "claude-secret",
        "claude_code_messages_url": "https://api.anthropic.com/v1/messages",
        "claude_code_default_model": "claude-sonnet-4-5",
        "claude_code_models": ["claude-sonnet-4-5"],
        "profile_store_path": tmp_path / "profiles.json",
        "profile_admin_token": "admin-token",
        "profile_allowed_hosts": ["token-plan-cn.xiaomimimo.com", "api.anthropic.com"],
        "langsmith_enabled": False,
        "langsmith_api_key": "",
        "service_auth_mode": "required",
        "service_auth_token": SERVICE_TOKEN,
        "gxp_readonly_mcp_enabled": True,
        "gxp_readonly_mcp_python_path": __import__("pathlib").Path(sys.executable),
        "gxp_readonly_mcp_server_path": server_path,
        "gxp_readonly_mcp_timeout_seconds": 10,
    }
    values.update(overrides)
    return Settings(**values)


def tool_definitions():
    return [
        {
            "name": name,
            "description": f"Read-only {name}",
            "inputSchema": {"type": "object", "properties": {}},
        }
        for name in GXP_READONLY_MCP_TOOLS
    ]


HASH_A = "a" * 64
HASH_B = "b" * 64
ACTION = {
    "action_type": "public",
    "ref_id": "action-1",
    "action_code": "TRAINING_CREATE",
    "action_name": "Training create",
    "page_id": None,
}
DESIGN_VERSION = {
    "design_id": "design-1",
    "ref_id": "action-1",
    "is_publish": 1,
    "is_deleted": 0,
    "modified_time": "2026-07-27 10:00:00",
    "created_time": "2026-07-20 10:00:00",
    "data_length": 1024,
    "data_sha256": HASH_A,
    "csharp_length": 2048,
    "csharp_sha256": HASH_B,
    "version": "published",
}
NODE = {
    "action_group": "Main",
    "group_key": "main",
    "canvas_line": 3,
    "internal_index": 2,
    "node_key": "node-call",
    "node_name": "Call training action",
    "node_type": "CallPublicAction",
    "description": "Call target action",
    "parent_path": [],
    "previous_node": None,
    "next_node": None,
    "facts": {
        "called_action": {
            "code": "TRAINING_DETAIL",
            "id": "action-2",
            "name": "Training detail",
        }
    },
}
DESIGN_DIFF = {
    "same": False,
    "left_sha256": HASH_A,
    "right_sha256": HASH_B,
    "added": [{"group_key": "main", "node_key": "node-new"}],
    "removed": [],
    "changed": [{"group_key": "main", "node_key": "node-call"}],
}
CSHARP_CONTEXT = {
    "requested_line": 42,
    "line_count": 100,
    "lines": [{"line": 42, "text": 'throw new Exception("failure");', "target": True}],
}
TABLE_COLUMN = {
    "Field": "id",
    "Type": "varchar(36)",
    "Null": "NO",
    "Key": "PRI",
    "Default": None,
    "Extra": "",
}
TABLE_INDEX = {
    "Table": "training_task",
    "Non_unique": 0,
    "Key_name": "PRIMARY",
    "Seq_in_index": 1,
    "Column_name": "id",
    "Collation": "A",
    "Cardinality": 100,
    "Sub_part": None,
    "Packed": None,
    "Null": "",
    "Index_type": "BTREE",
    "Comment": "",
    "Index_comment": "",
    "Visible": "YES",
    "Expression": None,
}

VALID_TOOL_RESULTS = {
    "connection_status": {"configured": True, "server_version": "8.0.17", "elapsed_ms": 1},
    "diagnose_codex_input": {
        "input_kind": "natural_language",
        "resolved_actions": [],
        "mentioned_tables": [],
        "design_text_matches": [],
        "next_tools": ["resolve_action", "get_design_versions"],
    },
    "resolve_action": {"identifier": "TRAINING_CREATE", "matches": [], "count": 0},
    "get_design_versions": {"action": ACTION, "versions": [DESIGN_VERSION], "count": 1},
    "inspect_action": {
        "action": ACTION,
        "design": {
            "design_id": "design-1",
            "version": "published",
            "is_deleted": 0,
            "modified_time": "2026-07-27 10:00:00",
            "data_sha256": HASH_A,
            "csharp_sha256": HASH_B,
        },
        "nodes": [NODE],
        "node_count": 1,
        "generated_csharp": CSHARP_CONTEXT,
    },
    "compare_designs": {
        "action": ACTION,
        "data": DESIGN_DIFF,
        "csharp_same": False,
        "published": {"modified_time": "2026-07-27 10:00:00", "csharp_sha256": HASH_A},
        "draft": {"modified_time": "2026-07-27 11:00:00", "csharp_sha256": HASH_B},
    },
    "trace_dynamic_exception": {
        "parsed": {
            "matched": True,
            "message": "failure",
            "namespace": "Gxp.Action.Action",
            "generated_class": "Action",
            "method": "Run",
            "generated_csharp_line": 42,
        },
        "at_time": "2026-07-27 10:00:00",
        "actions": [{
            "action": ACTION,
            "candidate_versions": [{
                "design_id": "design-1",
                "version": "published",
                "is_deleted": False,
                "modified_time": "2026-07-27 10:00:00",
                "data_sha256": HASH_A,
                "csharp_sha256": HASH_B,
                "generated_csharp": CSHARP_CONTEXT,
                "resolved_called_actions": [ACTION],
                "matching_canvas_nodes": [NODE],
                "exception_text_present": True,
                "evidence_score": 16,
            }],
            "called_action_exception_history": [{
                "action": ACTION,
                "exception_history": [{
                    "design_id": "design-1",
                    "version": "published",
                    "is_deleted": False,
                    "modified_time": "2026-07-27 10:00:00",
                    "data_sha256": HASH_A,
                    "csharp_sha256": HASH_B,
                    "exception_nodes": [NODE],
                    "predicate_nodes_before_exception": [],
                    "generated_csharp_matches": [CSHARP_CONTEXT],
                }],
                "history_count_after_deduplication": 1,
            }],
            "current_published_vs_draft": {"data": DESIGN_DIFF, "csharp_same": False},
        }],
        "selection_rule": "Prefer the highest evidence score.",
    },
    "describe_table": {
        "table": "training_task",
        "columns": [TABLE_COLUMN],
        "indexes": [TABLE_INDEX],
        "truncated": False,
    },
    "get_records": {"table": "training_task", "rows": [], "row_count": 0, "truncated": False},
    "evaluate_node_predicate": {
        "action": ACTION,
        "version": "published",
        "node": NODE,
        "evaluations": [{
            "field": "id",
            "operator": "EqualTo",
            "expression": "'1'",
            "record_value": "1",
            "literal_resolved": True,
            "matched": True,
        }],
        "all_definite_filters_match": True,
        "note": "Expression-based values remain unresolved and require caller variables.",
    },
    "readonly_sql": {
        "reason": "Need a narrow fallback query",
        "statement_type": "Select",
        "sql_hash": "0" * 64,
        "columns": ["id"],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "elapsed_ms": 1.0,
    },
}


def test_normalized_result_schemas_cover_allowlist_and_accept_plugin_shapes():
    assert tuple(GXP_READONLY_MCP_RESULT_SCHEMAS) == GXP_READONLY_MCP_TOOLS
    for tool_name, schema in GXP_READONLY_MCP_RESULT_SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        result = VALID_TOOL_RESULTS[tool_name]
        assert validate_readonly_mcp_result(tool_name, result) is result


@pytest.mark.parametrize("tool_name", GXP_READONLY_MCP_TOOLS)
def test_normalized_result_schemas_reject_malformed_success_payloads(tool_name):
    with pytest.raises(ReadonlyMcpError) as raised:
        validate_readonly_mcp_result(tool_name, {"unexpected": True})
    assert raised.value.code == "READONLY_MCP_RESULT_INVALID"

    with pytest.raises(ReadonlyMcpError) as unexpected:
        validate_readonly_mcp_result(tool_name, {
            **VALID_TOOL_RESULTS[tool_name],
            "unexpected": True,
        })
    assert unexpected.value.code == "READONLY_MCP_RESULT_INVALID"


def test_normalized_result_schemas_reject_unknown_nested_fields():
    with pytest.raises(ReadonlyMcpError) as action_error:
        validate_readonly_mcp_result("resolve_action", {
            "identifier": "TRAINING_CREATE",
            "matches": [{**ACTION, "arbitrary": True}],
            "count": 1,
        })
    assert action_error.value.code == "READONLY_MCP_RESULT_INVALID"

    with pytest.raises(ReadonlyMcpError) as column_error:
        validate_readonly_mcp_result("describe_table", {
            "table": "training_task",
            "columns": [{**TABLE_COLUMN, "arbitrary": True}],
            "indexes": [TABLE_INDEX],
            "truncated": False,
        })
    assert column_error.value.code == "READONLY_MCP_RESULT_INVALID"

    candidate = {
        **VALID_TOOL_RESULTS["trace_dynamic_exception"]["actions"][0]["candidate_versions"][0],
        "arbitrary": True,
    }
    with pytest.raises(ReadonlyMcpError) as candidate_error:
        validate_readonly_mcp_result("trace_dynamic_exception", {
            **VALID_TOOL_RESULTS["trace_dynamic_exception"],
            "actions": [{
                **VALID_TOOL_RESULTS["trace_dynamic_exception"]["actions"][0],
                "candidate_versions": [candidate],
            }],
        })
    assert candidate_error.value.code == "READONLY_MCP_RESULT_INVALID"


def test_response_model_revalidates_successful_mcp_results():
    with pytest.raises(ValueError, match="normalized result schema"):
        ReadonlyMcpToolCallResponse.model_validate({
            "serverId": "gxp-lowcode-readonly",
            "serverVersion": "test-1.0",
            "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
            "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
            "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
            "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
            "toolName": "readonly_sql",
            "success": True,
            "result": {"rows": []},
            "durationMs": 1,
        })

    success = ReadonlyMcpToolCallResponse.model_validate({
        "serverId": "gxp-lowcode-readonly",
        "serverVersion": "test-1.0",
        "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
        "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
        "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
        "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
        "toolName": "resolve_action",
        "success": True,
        "result": VALID_TOOL_RESULTS["resolve_action"],
        "durationMs": 1,
    }).model_dump(by_alias=True, exclude_none=True)
    assert "error" not in success

    failure = ReadonlyMcpToolCallResponse.model_validate({
        "serverId": "gxp-lowcode-readonly",
        "serverVersion": "test-1.0",
        "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
        "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
        "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
        "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
        "toolName": "resolve_action",
        "success": False,
        "error": "Read-only MCP failed",
        "durationMs": 1,
    }).model_dump(by_alias=True, exclude_none=True)
    assert "result" not in failure


class FakeGateway:
    enabled = True

    async def list_tools(self):
        return {
            "serverId": "gxp-lowcode-readonly",
            "serverVersion": "test-1.0",
            "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
            "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
            "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
            "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
            "tools": tool_definitions(),
        }

    async def call_tool(self, tool_name, arguments):
        if tool_name not in GXP_READONLY_MCP_TOOLS:
            raise ReadonlyMcpError("READONLY_MCP_TOOL_NOT_ALLOWED", "not allowed", 403)
        return {
            "serverId": "gxp-lowcode-readonly",
            "serverVersion": "test-1.0",
            "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
            "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
            "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
            "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
            "toolName": tool_name,
            "success": True,
            "result": {
                "identifier": arguments.get("identifier", ""),
                "matches": [],
                "count": 0,
            },
            "durationMs": 4,
        }


def call_payload(**overrides):
    payload = {
        "toolName": "resolve_action",
        "arguments": {"identifier": "TRAINING_CREATE"},
        "conversationId": CONVERSATION_ID,
        "runId": RUN_ID,
        "traceId": "trace_mcp",
        "skillId": "gxp-lowcode-debug",
        "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
    }
    payload.update(overrides)
    return payload


def test_fixed_routes_require_explicit_allowed_origin_and_log_only_metadata(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    app.state.readonly_mcp_gateway = FakeGateway()
    client = TestClient(app)

    unauthenticated = client.get(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/tools",
        headers={"Origin": ORIGIN},
    )
    assert unauthenticated.status_code == 401

    rejected = client.get(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/tools",
        headers={"X-ActionDesign-Service-Token": SERVICE_TOKEN},
    )
    assert rejected.status_code == 403

    listed = client.get(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/tools",
        headers=SERVICE_HEADERS,
    )
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["tools"]] == list(GXP_READONLY_MCP_TOOLS)

    called = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=SERVICE_HEADERS,
        json=call_payload(),
    )
    assert called.status_code == 200
    assert "error" not in called.json()
    assert called.json()["result"] == {
        "identifier": "TRAINING_CREATE",
        "matches": [],
        "count": 0,
    }

    log_files = list((tmp_path / "conversations").glob("*.jsonl"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "readonly_mcp_call" in log_text
    assert "argumentKeys" in log_text
    assert "TRAINING_CREATE" not in log_text
    event = json.loads(log_text.splitlines()[-1])
    assert event["toolName"] == "resolve_action"
    assert event["resultMetadata"]["success"] is True


def test_request_model_rejects_non_allowlisted_tools_and_wrong_skill_version(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.readonly_mcp_gateway = FakeGateway()
    client = TestClient(app)
    headers = SERVICE_HEADERS

    unknown = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=headers,
        json=call_payload(toolName="write_database"),
    )
    assert unknown.status_code == 422

    wrong_version = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=headers,
        json=call_payload(skillVersion="0.9.0"),
    )
    assert wrong_version.status_code == 422

    legacy_conversation = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=headers,
        json=call_payload(conversationId="conversation_mcp"),
    )
    legacy_run = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=headers,
        json=call_payload(runId="run_mcp"),
    )
    assert legacy_conversation.status_code == 422
    assert legacy_run.status_code == 422


def test_current_frontend_skill_version_is_accepted(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.readonly_mcp_gateway = FakeGateway()
    client = TestClient(app)

    response = client.post(
        "/api/actiondesign-agent/mcp/gxp-lowcode-readonly/call",
        headers=SERVICE_HEADERS,
        json=call_payload(skillVersion="1.2.0"),
    )

    assert GXP_LOWCODE_DEBUG_SKILL_VERSION == "1.2.0"
    assert response.status_code == 200


def test_enabled_mcp_rejects_wildcard_origin(tmp_path):
    with pytest.raises(ValueError, match="must be explicit"):
        make_settings(tmp_path, allow_origins=["*"])


def test_enabled_mcp_requires_service_auth(tmp_path):
    with pytest.raises(ValueError, match="must be required"):
        make_settings(
            tmp_path,
            service_auth_mode="disabled",
            service_auth_token="",
        )


def test_mcp_subprocess_environment_does_not_inherit_provider_secrets(monkeypatch):
    monkeypatch.setenv("GXP_MIMO_API_KEY", "provider-secret")
    monkeypatch.setenv("LANGSMITH_API_KEY", "langsmith-secret")
    monkeypatch.setenv("ACTIONDESIGN_PROFILE_ADMIN_TOKEN", "admin-secret")
    environment = _safe_subprocess_env()
    assert "GXP_MIMO_API_KEY" not in environment
    assert "LANGSMITH_API_KEY" not in environment
    assert "ACTIONDESIGN_PROFILE_ADMIN_TOKEN" not in environment


def test_gateway_validates_remote_allowlist_and_sanitizes_connection_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(
        readonly_mcp,
        "_tool_contract_hash",
        lambda _tools: GXP_READONLY_MCP_CONTRACT_HASH,
    )
    gateway = ReadonlyMcpGateway(make_settings(tmp_path))

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=name,
                        description=f"Read-only {name}",
                        inputSchema={"type": "object", "properties": {}},
                    )
                    for name in GXP_READONLY_MCP_TOOLS
                ]
            )

        async def call_tool(self, tool_name, arguments):
            assert tool_name == "connection_status"
            assert arguments == {}
            return SimpleNamespace(
            structuredContent={
                "configured": True,
                "config_path": "C:/secret/database.json",
                "server_version": "8.0.17",
            },
                content=[],
                isError=False,
            )

    @asynccontextmanager
    async def fake_session():
        yield FakeSession(), "test-1.0"

    gateway._session = fake_session
    response = asyncio.run(gateway.call_tool("connection_status", {}))
    assert response["success"] is True
    assert response["result"] == {"configured": True, "server_version": "8.0.17"}
    with pytest.raises(ReadonlyMcpError, match="fixed read-only allowlist"):
        asyncio.run(gateway.call_tool("write_database", {}))


def test_gateway_validates_arguments_against_remote_input_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(
        readonly_mcp,
        "_tool_contract_hash",
        lambda _tools: GXP_READONLY_MCP_CONTRACT_HASH,
    )
    gateway = ReadonlyMcpGateway(make_settings(tmp_path))
    call_count = 0

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=name,
                        description=f"Read-only {name}",
                        inputSchema=(
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"identifier": {"type": "string", "minLength": 1}},
                                "required": ["identifier"],
                            }
                            if name == "resolve_action"
                            else {"type": "object", "properties": {}}
                        ),
                    )
                    for name in GXP_READONLY_MCP_TOOLS
                ]
            )

        async def call_tool(self, tool_name, arguments):
            nonlocal call_count
            call_count += 1
            return SimpleNamespace(
                structuredContent={"identifier": arguments.get("identifier", ""), "matches": [], "count": 0},
                content=[],
                isError=False,
            )

    @asynccontextmanager
    async def fake_session():
        yield FakeSession(), "test-1.0"

    gateway._session = fake_session
    with pytest.raises(ReadonlyMcpError) as raised:
        asyncio.run(gateway.call_tool("resolve_action", {"unexpected": True}))
    assert raised.value.code == "READONLY_MCP_ARGUMENTS_INVALID"
    assert raised.value.status_code == 422
    assert call_count == 0


def test_gateway_requires_pymysql_mapping_parameters_for_readonly_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(
        readonly_mcp,
        "_tool_contract_hash",
        lambda _tools: GXP_READONLY_MCP_CONTRACT_HASH,
    )
    gateway = ReadonlyMcpGateway(make_settings(tmp_path))
    calls = []

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=name,
                        description=f"Read-only {name}",
                        inputSchema=(
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "reason": {"type": "string"},
                                    "sql": {"type": "string"},
                                    "params": {"type": "object"},
                                },
                                "required": ["reason", "sql"],
                            }
                            if name == "readonly_sql"
                            else {"type": "object", "properties": {}}
                        ),
                    )
                    for name in GXP_READONLY_MCP_TOOLS
                ]
            )

        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            return SimpleNamespace(
                structuredContent=VALID_TOOL_RESULTS["readonly_sql"],
                content=[],
                isError=False,
            )

    @asynccontextmanager
    async def fake_session():
        yield FakeSession(), "test-1.0"

    gateway._session = fake_session
    with pytest.raises(ReadonlyMcpError) as colon_error:
        asyncio.run(gateway.call_tool("readonly_sql", {
            "reason": "Need a narrow fallback query",
            "sql": "SELECT id FROM training_task WHERE id = :record_id",
            "params": {"record_id": "1"},
        }))
    assert colon_error.value.code == "READONLY_MCP_ARGUMENTS_INVALID"

    with pytest.raises(ReadonlyMcpError) as missing_parameter:
        asyncio.run(gateway.call_tool("readonly_sql", {
            "reason": "Need a narrow fallback query",
            "sql": "SELECT id FROM training_task WHERE id = %(record_id)s",
            "params": {},
        }))
    assert missing_parameter.value.code == "READONLY_MCP_ARGUMENTS_INVALID"

    response = asyncio.run(gateway.call_tool("readonly_sql", {
        "reason": "Need a narrow fallback query",
        "sql": "SELECT id FROM training_task WHERE id = %(record_id)s",
        "params": {"record_id": "1"},
    }))
    assert response["success"] is True
    assert len(calls) == 1


def test_gateway_sanitizes_mcp_errors_and_rejects_non_json_results(tmp_path, monkeypatch):
    monkeypatch.setattr(
        readonly_mcp,
        "_tool_contract_hash",
        lambda _tools: GXP_READONLY_MCP_CONTRACT_HASH,
    )
    gateway = ReadonlyMcpGateway(make_settings(tmp_path))
    mode = "error"

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=name,
                        description=f"Read-only {name}",
                        inputSchema={"type": "object", "properties": {}},
                    )
                    for name in GXP_READONLY_MCP_TOOLS
                ]
            )

        async def call_tool(self, tool_name, arguments):
            if mode == "error":
                return SimpleNamespace(
                    structuredContent=None,
                    content=[SimpleNamespace(
                        type="text",
                        text=(
                            "password=hunter2 host=db.internal "
                            "config_path=C:\\Users\\agent\\database.json Bearer abc.def"
                        ),
                    )],
                    isError=True,
                )
            if mode == "schema_invalid":
                return SimpleNamespace(
                    structuredContent={"unexpected": True},
                    content=[],
                    isError=False,
                )
            return SimpleNamespace(
                structuredContent={"value": object()},
                content=[],
                isError=False,
            )

    @asynccontextmanager
    async def fake_session():
        yield FakeSession(), "test-1.0"

    gateway._session = fake_session
    response = asyncio.run(gateway.call_tool("resolve_action", {}))
    assert response["success"] is False
    assert "hunter2" not in response["error"]
    assert "db.internal" not in response["error"]
    assert "C:\\Users" not in response["error"]
    assert "abc.def" not in response["error"]

    mode = "non_json"
    with pytest.raises(ReadonlyMcpError) as invalid_result:
        asyncio.run(gateway.call_tool("resolve_action", {}))
    assert invalid_result.value.code == "READONLY_MCP_RESULT_INVALID"

    mode = "schema_invalid"
    with pytest.raises(ReadonlyMcpError) as schema_invalid_result:
        asyncio.run(gateway.call_tool("resolve_action", {}))
    assert schema_invalid_result.value.code == "READONLY_MCP_RESULT_INVALID"


def test_gateway_maps_unconfigured_timeout_and_server_mismatch(monkeypatch, tmp_path):
    unconfigured = ReadonlyMcpGateway(SimpleNamespace(
        gxp_readonly_mcp_enabled=True,
        gxp_readonly_mcp_python_path=None,
        gxp_readonly_mcp_server_path=None,
        gxp_readonly_mcp_timeout_seconds=1,
    ))

    async def open_unconfigured():
        async with unconfigured._session():
            pass

    with pytest.raises(ReadonlyMcpError) as missing_config:
        asyncio.run(open_unconfigured())
    assert missing_config.value.code == "READONLY_MCP_NOT_CONFIGURED"

    settings = SimpleNamespace(
        gxp_readonly_mcp_enabled=True,
        gxp_readonly_mcp_python_path=tmp_path / "python.exe",
        gxp_readonly_mcp_server_path=tmp_path / "server.py",
        gxp_readonly_mcp_timeout_seconds=1,
    )

    @asynccontextmanager
    async def timeout_stdio(_parameters):
        raise TimeoutError("simulated timeout")
        yield

    monkeypatch.setattr(readonly_mcp, "stdio_client", timeout_stdio)
    timed_gateway = ReadonlyMcpGateway(settings)

    async def open_timeout():
        async with timed_gateway._session():
            pass

    with pytest.raises(ReadonlyMcpError) as timeout_error:
        asyncio.run(open_timeout())
    assert timeout_error.value.code == "READONLY_MCP_TIMEOUT"
    assert timeout_error.value.status_code == 504

    @asynccontextmanager
    async def mismatch_stdio(_parameters):
        yield object(), object()

    class MismatchClientSession:
        def __init__(self, _read_stream, _write_stream):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(name="unexpected-server", version="1.0"))

    monkeypatch.setattr(readonly_mcp, "stdio_client", mismatch_stdio)
    monkeypatch.setattr(readonly_mcp, "ClientSession", MismatchClientSession)
    mismatch_gateway = ReadonlyMcpGateway(settings)

    async def open_mismatch():
        async with mismatch_gateway._session():
            pass

    with pytest.raises(ReadonlyMcpError) as mismatch_error:
        asyncio.run(open_mismatch())
    assert mismatch_error.value.code == "READONLY_MCP_SERVER_MISMATCH"


def test_gateway_rejects_missing_duplicate_and_invalid_remote_tool_schemas(tmp_path):
    gateway = ReadonlyMcpGateway(make_settings(tmp_path))

    class ContractSession:
        def __init__(self, tools):
            self.tools = tools

        async def list_tools(self):
            return SimpleNamespace(tools=self.tools)

    def tool(name, schema=None):
        return SimpleNamespace(
            name=name,
            description=f"Read-only {name}",
            inputSchema=schema or {"type": "object", "properties": {}},
        )

    missing = [tool(name) for name in GXP_READONLY_MCP_TOOLS[:-1]]
    with pytest.raises(ReadonlyMcpError) as missing_error:
        asyncio.run(gateway._validated_tools(ContractSession(missing)))
    assert missing_error.value.code == "READONLY_MCP_TOOL_CONTRACT_MISMATCH"

    duplicate = [tool(name) for name in GXP_READONLY_MCP_TOOLS]
    duplicate.append(tool(GXP_READONLY_MCP_TOOLS[0]))
    with pytest.raises(ReadonlyMcpError) as duplicate_error:
        asyncio.run(gateway._validated_tools(ContractSession(duplicate)))
    assert duplicate_error.value.code == "READONLY_MCP_TOOL_CONTRACT_MISMATCH"

    invalid_schema = [tool(name) for name in GXP_READONLY_MCP_TOOLS]
    invalid_schema[3] = tool(GXP_READONLY_MCP_TOOLS[3], {"type": "array"})
    with pytest.raises(ReadonlyMcpError) as schema_error:
        asyncio.run(gateway._validated_tools(ContractSession(invalid_schema)))
    assert schema_error.value.code == "READONLY_MCP_TOOL_CONTRACT_MISMATCH"

    drifted_schema = [tool(name) for name in GXP_READONLY_MCP_TOOLS]
    with pytest.raises(ReadonlyMcpError) as drift_error:
        asyncio.run(gateway._validated_tools(ContractSession(drifted_schema)))
    assert drift_error.value.code == "READONLY_MCP_TOOL_CONTRACT_MISMATCH"
    assert "pinned" in str(drift_error.value)
