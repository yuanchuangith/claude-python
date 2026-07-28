"""Fixed allowlist bridge to the external read-only GXP MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .settings import Settings
from .redaction import redact_value


GXP_READONLY_MCP_SERVER_ID = "gxp-lowcode-readonly"
GXP_LOWCODE_DEBUG_SKILL_ID = "gxp-lowcode-debug"
GXP_LOWCODE_DEBUG_SKILL_VERSION = "1.2.0"
GXP_READONLY_MCP_CONTRACT_VERSION = "1.0.0"
GXP_READONLY_MCP_CONTRACT_HASH = "sha256:a41173bd6bb9c86820259ace8feceb718132b867c8bbebf5949fc69eb1a3fe50"
GXP_READONLY_MCP_TOOLS = (
    "connection_status",
    "diagnose_codex_input",
    "resolve_action",
    "get_design_versions",
    "inspect_action",
    "compare_designs",
    "trace_dynamic_exception",
    "describe_table",
    "get_records",
    "evaluate_node_predicate",
    "readonly_sql",
)

def _strict_object(
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _array(items: dict[str, Any], maximum: int) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": maximum}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


_DATABASE_ID = {
    "anyOf": [
        {"type": "string", "maxLength": 256},
        {"type": "number"},
    ]
}
_DATABASE_FLAG = {
    "anyOf": [
        {"type": "boolean"},
        {"type": "integer", "minimum": 0, "maximum": 1},
    ]
}
_NULLABLE_TIMESTAMP = {
    "anyOf": [
        {"type": "string", "maxLength": 128},
        {"type": "null"},
    ]
}
_NULLABLE_DATABASE_ID = {"anyOf": [_DATABASE_ID, {"type": "null"}]}
_SHA256 = {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"}

_GXP_RESULT_DEFS: dict[str, dict[str, Any]] = {
    "json_value": {
        "anyOf": [
            {"type": "null"},
            {"type": "boolean"},
            {"type": "number"},
            {"type": "string"},
            _array(_ref("json_value"), 10_000),
            {
                "type": "object",
                "propertyNames": {"type": "string", "maxLength": 512},
                "additionalProperties": _ref("json_value"),
            },
        ]
    },
    "json_object": {
        "type": "object",
        "propertyNames": {"type": "string", "maxLength": 512},
        "additionalProperties": _ref("json_value"),
    },
    "action_identity": _strict_object(
        {
            "action_type": {"enum": ["public", "form", "unknown"]},
            "ref_id": _DATABASE_ID,
            "action_code": {"type": "string", "maxLength": 256},
            "action_name": {"type": "string", "maxLength": 2_000},
            "page_id": _NULLABLE_DATABASE_ID,
            "form_name": {"type": "string", "maxLength": 2_000},
        },
        ("action_type", "ref_id", "action_code", "action_name", "page_id"),
    ),
    "design_version": _strict_object(
        {
            "design_id": _NULLABLE_DATABASE_ID,
            "ref_id": _DATABASE_ID,
            "is_publish": _DATABASE_FLAG,
            "is_deleted": _DATABASE_FLAG,
            "modified_time": _NULLABLE_TIMESTAMP,
            "created_time": _NULLABLE_TIMESTAMP,
            "data_length": {"type": "integer", "minimum": 0},
            "data_sha256": _SHA256,
            "csharp_length": {"type": "integer", "minimum": 0},
            "csharp_sha256": _SHA256,
            "version": {"enum": ["draft", "published"]},
        },
        (
            "ref_id", "is_publish", "is_deleted", "data_length", "data_sha256",
            "csharp_length", "csharp_sha256", "version",
        ),
    ),
    "design_identity": _strict_object(
        {
            "design_id": _NULLABLE_DATABASE_ID,
            "version": {"enum": ["draft", "published"]},
            "is_deleted": _DATABASE_FLAG,
            "modified_time": _NULLABLE_TIMESTAMP,
            "data_sha256": _SHA256,
            "csharp_sha256": _SHA256,
        },
        ("version", "is_deleted", "data_sha256", "csharp_sha256"),
    ),
    "node_reference": _strict_object(
        {
            "canvas_line": {"type": "integer", "minimum": 1},
            "internal_index": {"type": "integer", "minimum": 0},
            "node_key": {"type": "string", "maxLength": 256},
            "node_name": {"type": "string", "maxLength": 2_000},
            "node_type": {"type": "string", "maxLength": 256},
        },
        ("canvas_line", "internal_index", "node_key", "node_name", "node_type"),
    ),
    "unresolved_node_reference": _strict_object(
        {
            "node_key": {"type": "string", "maxLength": 256},
            "unresolved": {"const": True},
        },
        ("node_key", "unresolved"),
    ),
    "call_target": _strict_object(
        {
            "code": {"type": "string", "maxLength": 256},
            "id": {"type": "string", "maxLength": 256},
            "name": {"type": "string", "maxLength": 2_000},
        },
        ("code", "id", "name"),
    ),
    "canvas_facts": _strict_object(
        {
            "model_or_table": {"type": "string", "maxLength": 256},
            "defined_variable": {"type": "string", "maxLength": 512},
            "variable_value": {"type": "string", "maxLength": 8_000},
            "assignment_target": {"type": "string", "maxLength": 512},
            "assignment_value": {"type": "string", "maxLength": 8_000},
            "loop_source": {"type": "string", "maxLength": 8_000},
            "loop_item": {"type": "string", "maxLength": 512},
            "condition": {"type": "string", "maxLength": 32_000},
            "filters": _array(
                _strict_object(
                    {
                        "field": {"type": "string", "maxLength": 256},
                        "operator": {"type": "string", "maxLength": 128},
                        "expression": {"type": "string", "maxLength": 32_000},
                    },
                    ("field", "operator", "expression"),
                ),
                1_000,
            ),
            "field_mappings": _array(
                _strict_object(
                    {
                        "field": {"type": "string", "maxLength": 256},
                        "expression": {"type": "string", "maxLength": 32_000},
                    },
                    ("field", "expression"),
                ),
                1_000,
            ),
            "output_variables": _array({"type": "string", "maxLength": 512}, 1_000),
            "called_action": _ref("call_target"),
            "call_parameters": _array(
                _strict_object(
                    {
                        "name": {"type": "string", "maxLength": 512},
                        "expression": {"type": "string", "maxLength": 32_000},
                    },
                    ("name", "expression"),
                ),
                1_000,
            ),
        }
    ),
    "canvas_node": _strict_object(
        {
            "action_group": {"type": "string", "maxLength": 2_000},
            "group_key": {"type": "string", "maxLength": 256},
            "canvas_line": {"type": "integer", "minimum": 1},
            "internal_index": {"type": "integer", "minimum": 0},
            "node_key": {"type": "string", "maxLength": 256},
            "node_name": {"type": "string", "maxLength": 2_000},
            "node_type": {"type": "string", "maxLength": 256},
            "description": {"type": "string", "maxLength": 32_000},
            "parent_path": _array(
                {"oneOf": [_ref("node_reference"), _ref("unresolved_node_reference")]},
                1_000,
            ),
            "previous_node": {"oneOf": [_ref("node_reference"), {"type": "null"}]},
            "next_node": {"oneOf": [_ref("node_reference"), {"type": "null"}]},
            "facts": _ref("canvas_facts"),
            "paramsValue": _ref("json_object"),
        },
        (
            "action_group", "group_key", "canvas_line", "internal_index", "node_key",
            "node_name", "node_type", "description", "parent_path", "previous_node",
            "next_node", "facts",
        ),
    ),
    "generated_csharp_context": _strict_object(
        {
            "requested_line": {"type": "integer", "minimum": 1},
            "line_count": {"type": "integer", "minimum": 0},
            "lines": _array(
                _strict_object(
                    {
                        "line": {"type": "integer", "minimum": 1},
                        "text": {"type": "string", "maxLength": 2_000_000},
                        "target": {"type": "boolean"},
                    },
                    ("line", "text", "target"),
                ),
                1_000,
            ),
        },
        ("requested_line", "line_count", "lines"),
    ),
    "design_diff": _strict_object(
        {
            "same": {"type": "boolean"},
            "left_sha256": _SHA256,
            "right_sha256": _SHA256,
            "added": _array(
                _strict_object(
                    {
                        "group_key": {"type": "string", "maxLength": 256},
                        "node_key": {"type": "string", "maxLength": 256},
                    },
                    ("group_key", "node_key"),
                ),
                100_000,
            ),
            "removed": _array(
                _strict_object(
                    {
                        "group_key": {"type": "string", "maxLength": 256},
                        "node_key": {"type": "string", "maxLength": 256},
                    },
                    ("group_key", "node_key"),
                ),
                100_000,
            ),
            "changed": _array(
                _strict_object(
                    {
                        "group_key": {"type": "string", "maxLength": 256},
                        "node_key": {"type": "string", "maxLength": 256},
                    },
                    ("group_key", "node_key"),
                ),
                100_000,
            ),
        },
        ("same", "left_sha256", "right_sha256", "added", "removed", "changed"),
    ),
    "parsed_exception_unmatched": _strict_object(
        {
            "matched": {"const": False},
            "message": {"type": "string", "maxLength": 2_000_000},
        },
        ("matched", "message"),
    ),
    "parsed_exception_matched": _strict_object(
        {
            "matched": {"const": True},
            "message": {"type": "string", "maxLength": 2_000_000},
            "namespace": {"type": "string", "maxLength": 2_000},
            "generated_class": {"type": "string", "maxLength": 256},
            "method": {"type": "string", "maxLength": 256},
            "generated_csharp_line": {"type": "integer", "minimum": 1},
        },
        ("matched", "message", "namespace", "generated_class", "method", "generated_csharp_line"),
    ),
    "exception_history_entry": _strict_object(
        {
            "design_id": _NULLABLE_DATABASE_ID,
            "version": {"enum": ["draft", "published"]},
            "is_deleted": {"type": "boolean"},
            "modified_time": _NULLABLE_TIMESTAMP,
            "data_sha256": _SHA256,
            "csharp_sha256": _SHA256,
            "exception_nodes": _array(_ref("canvas_node"), 100_000),
            "predicate_nodes_before_exception": _array(_ref("canvas_node"), 100_000),
            "generated_csharp_matches": _array(_ref("generated_csharp_context"), 100_000),
        },
        (
            "version", "is_deleted", "data_sha256", "csharp_sha256", "exception_nodes",
            "predicate_nodes_before_exception", "generated_csharp_matches",
        ),
    ),
    "exception_candidate": _strict_object(
        {
            "design_id": _NULLABLE_DATABASE_ID,
            "version": {"enum": ["draft", "published"]},
            "is_deleted": {"type": "boolean"},
            "modified_time": _NULLABLE_TIMESTAMP,
            "data_sha256": _SHA256,
            "csharp_sha256": _SHA256,
            "generated_csharp": _ref("generated_csharp_context"),
            "resolved_called_actions": _array(_ref("action_identity"), 1_000),
            "matching_canvas_nodes": _array(_ref("canvas_node"), 100_000),
            "exception_text_present": {"type": "boolean"},
            "evidence_score": {"type": "integer", "minimum": 0},
        },
        (
            "version", "is_deleted", "data_sha256", "csharp_sha256", "generated_csharp",
            "resolved_called_actions", "matching_canvas_nodes", "exception_text_present",
            "evidence_score",
        ),
    ),
    "current_design_comparison": {
        "oneOf": [
            _strict_object(
                {"data": _ref("design_diff"), "csharp_same": {"type": "boolean"}},
                ("data", "csharp_same"),
            ),
            _strict_object(
                {
                    "available": {"const": False},
                    "reason": {"type": "string", "maxLength": 16_000},
                },
                ("available", "reason"),
            ),
        ]
    },
    "exception_action": _strict_object(
        {
            "action": _ref("action_identity"),
            "candidate_versions": _array(_ref("exception_candidate"), 100),
            "called_action_exception_history": _array(
                _strict_object(
                    {
                        "action": _ref("action_identity"),
                        "exception_history": _array(_ref("exception_history_entry"), 10),
                        "history_count_after_deduplication": {"type": "integer", "minimum": 0},
                    },
                    ("action", "exception_history", "history_count_after_deduplication"),
                ),
                1_000,
            ),
            "current_published_vs_draft": _ref("current_design_comparison"),
        },
        ("action", "candidate_versions", "called_action_exception_history", "current_published_vs_draft"),
    ),
    "trace_dynamic_exception_result": {
        "oneOf": [
            _strict_object(
                {
                    "parsed": _ref("parsed_exception_unmatched"),
                    "error": {"type": "string", "maxLength": 16_000},
                },
                ("parsed", "error"),
            ),
            _strict_object(
                {
                    "parsed": _ref("parsed_exception_matched"),
                    "at_time": _NULLABLE_TIMESTAMP,
                    "actions": _array(_ref("exception_action"), 1_000),
                    "selection_rule": {"type": "string", "maxLength": 4_000},
                },
                ("parsed", "at_time", "actions", "selection_rule"),
            ),
        ]
    },
    "design_text_match": _strict_object(
        {
            "ref_id": _DATABASE_ID,
            "is_publish": _DATABASE_FLAG,
            "is_deleted": _DATABASE_FLAG,
            "modified_time": _NULLABLE_TIMESTAMP,
        },
        ("ref_id", "is_publish", "is_deleted"),
    ),
    "table_column": _strict_object(
        {
            "Field": {"type": "string", "minLength": 1, "maxLength": 128},
            "Type": {"type": "string", "maxLength": 2_000},
            "Null": {"type": "string", "maxLength": 16},
            "Key": {"type": "string", "maxLength": 64},
            "Default": _ref("json_value"),
            "Extra": {"type": "string", "maxLength": 2_000},
        },
        ("Field", "Type", "Null", "Key", "Default", "Extra"),
    ),
    "table_index": _strict_object(
        {
            "Table": {"type": "string", "maxLength": 128},
            "Non_unique": {
                "anyOf": [
                    {"type": "integer", "minimum": 0, "maximum": 1},
                    {"type": "string", "maxLength": 16},
                ]
            },
            "Key_name": {"type": "string", "minLength": 1, "maxLength": 256},
            "Seq_in_index": {
                "anyOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "string", "pattern": "^\\d+$"},
                ]
            },
            "Column_name": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 128}, {"type": "null"}]},
            "Collation": {"anyOf": [{"type": "string", "maxLength": 16}, {"type": "null"}]},
            "Cardinality": {"anyOf": [{"type": "number"}, {"type": "string", "maxLength": 64}, {"type": "null"}]},
            "Sub_part": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "maxLength": 64}, {"type": "null"}]},
            "Packed": {"anyOf": [{"type": "string", "maxLength": 64}, {"type": "null"}]},
            "Null": {"type": "string", "maxLength": 16},
            "Index_type": {"type": "string", "maxLength": 64},
            "Comment": {"type": "string", "maxLength": 2_000},
            "Index_comment": {"type": "string", "maxLength": 2_000},
            "Visible": {"type": "string", "maxLength": 16},
            "Expression": {"anyOf": [{"type": "string", "maxLength": 2_000}, {"type": "null"}]},
        },
        ("Non_unique", "Key_name", "Seq_in_index", "Column_name"),
    ),
}


def _result_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": _GXP_RESULT_DEFS,
        **schema,
    }


GXP_READONLY_MCP_RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "connection_status": _result_schema(_strict_object(
        {
            "configured": {"type": "boolean"},
            "reachable": {"type": "boolean"},
            "server_version": {"anyOf": [{"type": "string", "maxLength": 256}, {"type": "null"}]},
            "elapsed_ms": {"type": "number", "minimum": 0},
            "error": {"type": "string", "maxLength": 16_000},
        },
        ("configured",),
    )),
    "diagnose_codex_input": _result_schema({
        **_strict_object(
            {
                "input_kind": {"enum": ["dynamic_exception", "natural_language"]},
                "diagnosis": _ref("trace_dynamic_exception_result"),
                "resolved_actions": _array(_ref("action_identity"), 1_000),
                "mentioned_tables": _array({"type": "string", "maxLength": 128}, 20),
                "design_text_matches": _array(_ref("design_text_match"), 100),
                "next_tools": _array({"type": "string", "maxLength": 256}, 100),
            },
            ("input_kind", "next_tools"),
        ),
        "allOf": [
            {
                "if": {"properties": {"input_kind": {"const": "dynamic_exception"}}},
                "then": {"required": ["diagnosis"]},
            },
            {
                "if": {"properties": {"input_kind": {"const": "natural_language"}}},
                "then": {"required": ["resolved_actions", "mentioned_tables", "design_text_matches"]},
            },
        ],
    }),
    "resolve_action": _result_schema(_strict_object(
        {
            "identifier": {"type": "string", "maxLength": 2_000_000},
            "matches": _array(_ref("action_identity"), 1_000),
            "count": {"type": "integer", "minimum": 0},
        },
        ("identifier", "matches", "count"),
    )),
    "get_design_versions": _result_schema(_strict_object(
        {
            "action": _ref("action_identity"),
            "versions": _array(_ref("design_version"), 100),
            "count": {"type": "integer", "minimum": 0},
        },
        ("action", "versions", "count"),
    )),
    "inspect_action": _result_schema(_strict_object(
        {
            "action": _ref("action_identity"),
            "design": _ref("design_identity"),
            "nodes": _array(_ref("canvas_node"), 100_000),
            "node_count": {"type": "integer", "minimum": 0},
            "generated_csharp": _ref("generated_csharp_context"),
            "generated_csharp_matches": _array(_ref("generated_csharp_context"), 100_000),
        },
        ("action", "design", "nodes", "node_count"),
    )),
    "compare_designs": _result_schema(_strict_object(
        {
            "action": _ref("action_identity"),
            "data": _ref("design_diff"),
            "csharp_same": {"type": "boolean"},
            "published": _strict_object(
                {
                    "modified_time": _NULLABLE_TIMESTAMP,
                    "csharp_sha256": _SHA256,
                },
                ("csharp_sha256",),
            ),
            "draft": _strict_object(
                {
                    "modified_time": _NULLABLE_TIMESTAMP,
                    "csharp_sha256": _SHA256,
                },
                ("csharp_sha256",),
            ),
        },
        ("action", "data", "csharp_same", "published", "draft"),
    )),
    "trace_dynamic_exception": _result_schema(_ref("trace_dynamic_exception_result")),
    "describe_table": _result_schema(_strict_object(
        {
            "table": {"type": "string", "minLength": 1, "maxLength": 128},
            "columns": _array(_ref("table_column"), 500),
            "indexes": _array(_ref("table_index"), 500),
            "truncated": {"type": "boolean"},
        },
        ("table", "columns", "indexes", "truncated"),
    )),
    "get_records": _result_schema(_strict_object(
        {
            "table": {"type": "string", "minLength": 1, "maxLength": 128},
            "rows": _array(_ref("json_object"), 10_000),
            "row_count": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
        },
        ("table", "rows", "row_count", "truncated"),
    )),
    "evaluate_node_predicate": _result_schema(_strict_object(
        {
            "action": _ref("action_identity"),
            "version": {"enum": ["draft", "published"]},
            "node": _ref("canvas_node"),
            "evaluations": _array(
                _strict_object(
                    {
                        "field": {"type": "string", "maxLength": 256},
                        "operator": {"type": "string", "maxLength": 128},
                        "expression": {"type": "string", "maxLength": 32_000},
                        "record_value": _ref("json_value"),
                        "literal_resolved": {"type": "boolean"},
                        "matched": {"type": ["boolean", "null"]},
                    },
                    ("field", "operator", "expression", "record_value", "literal_resolved", "matched"),
                ),
                1_000,
            ),
            "all_definite_filters_match": {"type": ["boolean", "null"]},
            "note": {"type": "string", "maxLength": 16_000},
        },
        ("action", "version", "node", "evaluations", "all_definite_filters_match", "note"),
    )),
    "readonly_sql": _result_schema(_strict_object(
        {
            "reason": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "statement_type": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"},
            "sql_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "columns": _array({"type": "string", "maxLength": 128}, 1_000),
            "rows": _array(_ref("json_object"), 10_000),
            "row_count": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
            "elapsed_ms": {"type": "number", "minimum": 0},
        },
        ("reason", "statement_type", "sql_hash", "columns", "rows", "row_count", "truncated", "elapsed_ms"),
    )),
}

_MAX_MCP_RESULT_CHARACTERS = 2_000_000
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\r\n\t,;]+")
_UNIX_SENSITIVE_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|var|etc|opt|tmp)/[^\r\n\t,;]+")
_CONNECTION_PAIR = re.compile(
    r"\b(host|hostname|user|username|database|dsn|connection(?:_string)?|config(?:_path)?|port)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_COLON_PARAMETER = re.compile(r"(^|[^%]):[A-Za-z_][A-Za-z0-9_]*")
_POSITIONAL_PARAMETER = re.compile(r"(^|[^%])%s\b")
_MAPPING_PARAMETER = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")

_SAFE_SUBPROCESS_ENV = (
    "APPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PROGRAMDATA",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


class ReadonlyMcpError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _safe_subprocess_env() -> dict[str, str]:
    return {
        key: value
        for key in _SAFE_SUBPROCESS_ENV
        if (value := os.environ.get(key)) is not None
    }


def _tool_to_public(tool: Any) -> dict[str, Any]:
    return {
        "name": str(tool.name),
        "description": str(tool.description or ""),
        "inputSchema": dict(tool.inputSchema or {}),
    }


def _tool_contract_hash(tools: list[Any]) -> str:
    contract = {
        "serverId": GXP_READONLY_MCP_SERVER_ID,
        "tools": [
            {
                "name": str(tool.name),
                "inputSchema": dict(tool.inputSchema or {}),
            }
            for tool in tools
        ],
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", None) or []
    texts = [getattr(block, "text", "") for block in blocks if getattr(block, "type", "") == "text"]
    if not texts:
        return None
    text = "\n".join(item for item in texts if item)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _sanitize_result(tool_name: str, value: Any) -> Any:
    sanitized = redact_value(value)
    if tool_name != "connection_status" or not isinstance(sanitized, dict):
        return sanitized
    return {
        key: item
        for key, item in sanitized.items()
        if key.lower() not in {
            "config_path",
            "connection_string",
            "database",
            "dsn",
            "host",
            "password",
            "port",
            "user",
            "username",
        }
    }


def _sanitize_error_message(value: Any) -> str:
    message = str(redact_value(str(value) if value is not None else ""))
    message = _CONNECTION_PAIR.sub(r"\1\2[REDACTED]", message)
    message = _WINDOWS_PATH.sub("[REDACTED_PATH]", message)
    message = _UNIX_SENSITIVE_PATH.sub("[REDACTED_PATH]", message)
    return (message.strip() or "Read-only MCP tool returned an error")[:16_000]


def _sql_without_quoted_values(sql: str) -> str:
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            output.append(" ")
            if char == "\\":
                index += 2
                output.append(" ")
                continue
            if char == quote:
                if quote in {"'", '"'} and next_char == quote:
                    index += 2
                    output.append(" ")
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(" ")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _validate_readonly_sql_arguments(arguments: dict[str, Any]) -> None:
    sql = str(arguments.get("sql") or "")
    unquoted = _sql_without_quoted_values(sql)
    if _COLON_PARAMETER.search(unquoted):
        raise ReadonlyMcpError(
            "READONLY_MCP_ARGUMENTS_INVALID",
            "readonly_sql named parameters must use %(name)s placeholders",
            422,
        )
    if _POSITIONAL_PARAMETER.search(unquoted):
        raise ReadonlyMcpError(
            "READONLY_MCP_ARGUMENTS_INVALID",
            "readonly_sql positional %s placeholders are not supported with a params object",
            422,
        )
    params = arguments.get("params") or {}
    if not isinstance(params, dict):
        raise ReadonlyMcpError(
            "READONLY_MCP_ARGUMENTS_INVALID",
            "readonly_sql params must be an object",
            422,
        )
    placeholder_names = set(_MAPPING_PARAMETER.findall(unquoted))
    parameter_names = set(params)
    if placeholder_names != parameter_names:
        raise ReadonlyMcpError(
            "READONLY_MCP_ARGUMENTS_INVALID",
            "readonly_sql params must exactly match %(name)s placeholders",
            422,
        )


def _validate_result_payload(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReadonlyMcpError(
            "READONLY_MCP_RESULT_INVALID",
            "Read-only MCP returned a non-JSON result",
        ) from exc
    if len(encoded) > _MAX_MCP_RESULT_CHARACTERS:
        raise ReadonlyMcpError(
            "READONLY_MCP_RESULT_TOO_LARGE",
            "Read-only MCP result exceeds the 2000000 character limit",
        )
    return value


def validate_readonly_mcp_result(tool_name: str, value: Any) -> Any:
    schema = GXP_READONLY_MCP_RESULT_SCHEMAS.get(tool_name)
    if schema is None:
        raise ReadonlyMcpError(
            "READONLY_MCP_RESULT_INVALID",
            "Read-only MCP result schema is unavailable for the requested tool",
        )
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path) or "$"
        raise ReadonlyMcpError(
            "READONLY_MCP_RESULT_INVALID",
            f"Read-only MCP result failed the normalized result schema at {path}",
        ) from exc
    return value


class ReadonlyMcpGateway:
    """Launch and call only the configured gxp-lowcode-readonly stdio server."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.gxp_readonly_mcp_enabled)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ReadonlyMcpError("READONLY_MCP_DISABLED", "Read-only MCP is not enabled", 503)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[tuple[ClientSession, str]]:
        self._require_enabled()
        python_path = self.settings.gxp_readonly_mcp_python_path
        server_path = self.settings.gxp_readonly_mcp_server_path
        if python_path is None or server_path is None:
            raise ReadonlyMcpError("READONLY_MCP_NOT_CONFIGURED", "Read-only MCP executable paths are not configured", 503)
        parameters = StdioServerParameters(
            command=str(python_path),
            args=[str(server_path)],
            cwd=str(server_path.parent),
            env=_safe_subprocess_env(),
            encoding="utf-8",
            encoding_error_handler="strict",
        )
        try:
            async with asyncio.timeout(self.settings.gxp_readonly_mcp_timeout_seconds):
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        server_info = initialized.serverInfo
                        if server_info.name != GXP_READONLY_MCP_SERVER_ID:
                            raise ReadonlyMcpError(
                                "READONLY_MCP_SERVER_MISMATCH",
                                "Configured MCP process did not identify as gxp-lowcode-readonly",
                            )
                        yield session, str(server_info.version)
        except ReadonlyMcpError:
            raise
        except TimeoutError as exc:
            raise ReadonlyMcpError("READONLY_MCP_TIMEOUT", "Read-only MCP request timed out", 504) from exc
        except Exception as exc:
            raise ReadonlyMcpError(
                "READONLY_MCP_TRANSPORT_ERROR",
                f"Read-only MCP transport failed ({type(exc).__name__})",
            ) from exc

    @staticmethod
    async def _validated_tools(session: ClientSession) -> list[Any]:
        listed = await session.list_tools()
        tools = [tool for tool in listed.tools if tool.name in GXP_READONLY_MCP_TOOLS]
        names = [str(tool.name) for tool in tools]
        if len(names) != len(set(names)):
            raise ReadonlyMcpError(
                "READONLY_MCP_TOOL_CONTRACT_MISMATCH",
                "Configured MCP server returned duplicate read-only tool definitions",
            )
        available = {tool.name for tool in tools}
        missing = [name for name in GXP_READONLY_MCP_TOOLS if name not in available]
        if missing:
            raise ReadonlyMcpError(
                "READONLY_MCP_TOOL_CONTRACT_MISMATCH",
                "Configured MCP server is missing required read-only tools",
            )
        tool_by_name = {str(tool.name): tool for tool in tools}
        for name in GXP_READONLY_MCP_TOOLS:
            schema = dict(tool_by_name[name].inputSchema or {})
            if schema.get("type") != "object":
                raise ReadonlyMcpError(
                    "READONLY_MCP_TOOL_CONTRACT_MISMATCH",
                    f"Configured MCP tool {name} does not expose an object inputSchema",
                )
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ReadonlyMcpError(
                    "READONLY_MCP_TOOL_CONTRACT_MISMATCH",
                    f"Configured MCP tool {name} exposes an invalid inputSchema",
                ) from exc
        ordered_tools = [tool_by_name[name] for name in GXP_READONLY_MCP_TOOLS]
        actual_contract_hash = _tool_contract_hash(ordered_tools)
        if actual_contract_hash != GXP_READONLY_MCP_CONTRACT_HASH:
            raise ReadonlyMcpError(
                "READONLY_MCP_TOOL_CONTRACT_MISMATCH",
                "Configured MCP server tool schemas do not match the pinned read-only contract",
            )
        return ordered_tools

    async def list_tools(self) -> dict[str, Any]:
        async with self._session() as (session, server_version):
            tools = await self._validated_tools(session)
            return {
                "serverId": GXP_READONLY_MCP_SERVER_ID,
                "serverVersion": server_version,
                "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
                "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
                "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
                "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
                "tools": [_tool_to_public(tool) for tool in tools],
            }

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in GXP_READONLY_MCP_TOOLS:
            raise ReadonlyMcpError(
                "READONLY_MCP_TOOL_NOT_ALLOWED",
                "Requested MCP tool is not in the fixed read-only allowlist",
                403,
            )
        started = time.perf_counter()
        async with self._session() as (session, server_version):
            tools = await self._validated_tools(session)
            tool = next(item for item in tools if item.name == tool_name)
            try:
                Draft202012Validator(dict(tool.inputSchema or {})).validate(arguments)
            except ValidationError as exc:
                path = ".".join(str(item) for item in exc.absolute_path) or "$"
                raise ReadonlyMcpError(
                    "READONLY_MCP_ARGUMENTS_INVALID",
                    f"Read-only MCP arguments failed inputSchema validation at {path}",
                    422,
                ) from exc
            if tool_name == "readonly_sql":
                _validate_readonly_sql_arguments(arguments)
            result = await session.call_tool(tool_name, arguments)
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        payload = _validate_result_payload(_sanitize_result(tool_name, _text_payload(result)))
        if bool(getattr(result, "isError", False)):
            message = _sanitize_error_message(
                payload if isinstance(payload, str) else "Read-only MCP tool returned an error"
            )
            return {
                "serverId": GXP_READONLY_MCP_SERVER_ID,
                "serverVersion": server_version,
                "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
                "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
                "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
                "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
                "toolName": tool_name,
                "success": False,
                "error": message,
                "durationMs": duration_ms,
            }
        normalized_payload = validate_readonly_mcp_result(tool_name, payload)
        return {
            "serverId": GXP_READONLY_MCP_SERVER_ID,
            "serverVersion": server_version,
            "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
            "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
            "contractVersion": GXP_READONLY_MCP_CONTRACT_VERSION,
            "contractHash": GXP_READONLY_MCP_CONTRACT_HASH,
            "toolName": tool_name,
            "success": True,
            "result": normalized_payload,
            "durationMs": duration_ms,
        }
