"""Normalized request and response models for the thin ActionDesign gateway."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from re import compile as compile_regex
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identifiers import (
    validate_conversation_uuid4,
    validate_history_conversation_id,
    validate_history_run_id,
    validate_run_id,
    validate_safe_request_id,
    validate_uuid4_id,
)
MIMO_MODELS: dict[str, dict[str, Any]] = {
    "mimo-v2.5": {
        "name": "MiMo v2.5",
        "provider": "mimo",
        "supportsImages": True,
        "contextWindowTokens": 100000,
        "maxOutputTokens": 8192,
    },
    "mimo-v2.5-pro": {
        "name": "MiMo v2.5 Pro",
        "provider": "mimo",
        "supportsImages": False,
        "contextWindowTokens": 100000,
        "maxOutputTokens": 8192,
    },
}

class _GatewayBaseModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


IDENTIFIER_RE = compile_regex(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
PROFILE_ID_RE = compile_regex(r"^[A-Za-z0-9_-]{1,64}$")
MCP_SERVER_ID_RE = compile_regex(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TOOL_NAME_RE = compile_regex(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
MCP_TOOL_NAME_RE = compile_regex(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
SHA256_RE = compile_regex(r"^sha256:[a-fA-F0-9]{64}$")
MODEL_NAME_RE = compile_regex(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
MAX_PROMPT_CHARS = 2_000_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 8
MAX_TOOLS = 64
MAX_TOOL_ARGUMENTS_JSON_CHARS = 500_000
MAX_TOOL_RESULT_JSON_CHARS = 2_000_000
MAX_TRACE_VALUE_JSON_CHARS = 500_000
MAX_TRACE_BATCH_JSON_CHARS = 5_000_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000


def _validate_bounded_json(
    value: Any,
    *,
    label: str,
    max_chars: int,
) -> Any:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError(f"{label} exceeds {MAX_JSON_NODES} JSON nodes")
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{label} exceeds JSON depth {MAX_JSON_DEPTH}")
        if current is None or isinstance(current, (str, bool)):
            continue
        if isinstance(current, int):
            continue
        if isinstance(current, float):
            if current != current or current in {float("inf"), float("-inf")}:
                raise ValueError(f"{label} contains a non-finite number")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 1_024:
                    raise ValueError(f"{label} contains an invalid object key")
                stack.append((item, depth + 1))
            continue
        raise ValueError(f"{label} must contain JSON values only")
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    if len(serialized) > max_chars:
        raise ValueError(f"{label} exceeds {max_chars} JSON characters")
    return value


class ImageInput(_GatewayBaseModel):
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"] = "image/png"
    data: str = ""
    url: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> "ImageInput":
        data = self.data
        url = self.url
        if bool(data) == bool(url):
            raise ValueError("exactly one of image data or url is required")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("image url must be an absolute http(s) URL")
        if data:
            if len(data) > ((MAX_IMAGE_BYTES * 4) // 3) + 8:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} decoded bytes")
            try:
                decoded = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("image data must be valid base64") from exc
            if len(decoded) > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} decoded bytes")
        return self


class NativeToolDefinition(_GatewayBaseModel):
    name: str
    description: str = Field(default="", max_length=8000)
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="input_schema")

    @field_validator("name")
    def _validate_name(cls, value: str) -> str:
        if not TOOL_NAME_RE.fullmatch(value):
            raise ValueError("invalid native tool name")
        return value

    @field_validator("input_schema")
    def _validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_bounded_json(
            value,
            label="native tool input schema",
            max_chars=MAX_TOOL_ARGUMENTS_JSON_CHARS,
        )
        if value.get("type") != "object":
            raise ValueError("tool input_schema.type must be object")
        properties = value.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("tool input_schema.properties must be an object")
        required = value.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("tool input_schema.required must be a string array")
        missing = [item for item in required if item not in properties]
        if missing:
            raise ValueError(f"tool schema required properties are undefined: {', '.join(missing)}")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"tool input_schema is not valid JSON Schema: {exc.message}") from exc
        return value


class NativeToolChoice(_GatewayBaseModel):
    type: Literal["auto", "any", "none", "tool"] = "auto"
    name: str = ""

    @model_validator(mode="after")
    def _validate_choice(self) -> "NativeToolChoice":
        choice_type = self.type
        name = self.name
        if choice_type == "tool":
            if not TOOL_NAME_RE.fullmatch(name):
                raise ValueError("tool choice name is required and must be valid")
        elif name:
            raise ValueError("tool choice name is only allowed for type=tool")
        return self


class ContextManagementMetadata(_GatewayBaseModel):
    status: Literal["raw", "compressed", "sliced", "blocked"]
    estimated_tokens_before: int = Field(alias="estimatedTokensBefore", ge=0)
    estimated_tokens_after: int = Field(alias="estimatedTokensAfter", ge=0)
    system_prompt_tokens: int = Field(alias="systemPromptTokens", ge=0)
    normal_compression_attempts: int = Field(alias="normalCompressionAttempts", ge=0, le=3)
    fallback_slice_attempts: int = Field(alias="fallbackSliceAttempts", ge=0, le=20)
    removed_message_count: int = Field(alias="removedMessageCount", ge=0)
    retained_recent_turns: int = Field(alias="retainedRecentTurns", ge=0, le=20)


class ThinkingConfig(_GatewayBaseModel):
    type: Literal["disabled", "enabled"] = "disabled"
    budget_tokens: int | None = Field(default=None, ge=1024, le=32768)

    @model_validator(mode="after")
    def _validate_budget(self) -> "ThinkingConfig":
        if self.type == "enabled" and self.budget_tokens is None:
            raise ValueError("budget_tokens is required when thinking is enabled")
        if self.type == "disabled" and self.budget_tokens is not None:
            raise ValueError("budget_tokens is not allowed when thinking is disabled")
        return self


class RequestIdentity(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_run_id(cls, value: str) -> str:
        return validate_run_id(value)


class RequestCorrelation(_GatewayBaseModel):
    correlation_id: str = Field(alias="correlationId")
    retry_of: str = Field(default="", alias="retryOf")
    parent_request_id: str = Field(default="", alias="parentRequestId")

    @field_validator("correlation_id")
    def _validate_correlation_uuid(cls, value: str) -> str:
        return validate_uuid4_id(value, label="correlationId")

    @field_validator("retry_of")
    def _validate_retry_uuid(cls, value: str) -> str:
        return validate_uuid4_id(value, label="retryOf") if value else value

    @field_validator("parent_request_id")
    def _validate_parent_request_uuid(cls, value: str) -> str:
        return validate_uuid4_id(value, label="parentRequestId") if value else value


class RequestCorrelationIdentity(RequestCorrelation):
    """Caller-provided request identity validated at the transport boundary."""

    request_id: str = Field(alias="requestId")

    @field_validator("request_id")
    def _validate_request_uuid(cls, value: str) -> str:
        return validate_uuid4_id(value, label="requestId")


class AgentChatRequest(RequestCorrelationIdentity):
    provider: str = "auto"
    profile_id: str = Field(default="", alias="profileId")
    model: str = ""
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    topic_id: str = Field(default="", alias="topicId")
    user_id: str = Field(default="", alias="userId", max_length=256)
    checkpoint_id: str = Field(default="", alias="checkpointId")
    agent_mode: Literal["normal", "plan", "chat", ""] = Field(
        default="", alias="agentMode"
    )
    system_prompt: str = Field(alias="systemPrompt", min_length=1, max_length=500_000)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    stream: bool = False
    tools: list[NativeToolDefinition] = Field(default_factory=list)
    tool_choice: NativeToolChoice | None = Field(default=None, alias="toolChoice")
    images: list[ImageInput] = Field(default_factory=list)
    max_tokens: int = Field(default=8192, alias="maxTokens", ge=256, le=32768)
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    context_management: ContextManagementMetadata | None = Field(default=None, alias="contextManagement")
    protocol_version: str = Field(default="1.0", alias="protocolVersion")
    graph_version: str = Field(default="", alias="graphVersion")
    prompt_manifest_version: str = Field(default="", alias="promptManifestVersion")
    prompt_manifest_hash: str = Field(default="", alias="promptManifestHash")
    tool_catalog_version: str = Field(default="", alias="toolCatalogVersion")
    tool_catalog_hash: str = Field(default="", alias="toolCatalogHash")
    skill_catalog_version: str = Field(default="", alias="skillCatalogVersion")
    skill_catalog_hash: str = Field(default="", alias="skillCatalogHash")
    loaded_skill_versions: dict[str, str] = Field(default_factory=dict, alias="loadedSkillVersions")
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    trace_id: str = Field(default="", alias="traceId")
    parent_trace_run_id: str = Field(default="", alias="parentTraceRunId")
    trace_sampled: bool = Field(default=False, alias="traceSampled")

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("topic_id", "checkpoint_id", "trace_id", "parent_trace_run_id")
    def _validate_optional_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier is invalid")
        return value

    @field_validator("model")
    def _validate_model(cls, value: str) -> str:
        if value and not MODEL_NAME_RE.fullmatch(value):
            raise ValueError("model name is invalid")
        return value

    @field_validator("provider")
    def _validate_provider(cls, value: str) -> str:
        if value != "auto" and not PROFILE_ID_RE.fullmatch(value):
            raise ValueError("provider must be auto or a valid channel identifier")
        return value

    @field_validator("system_prompt")
    def _validate_system_prompt(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("systemPrompt must not contain only whitespace")
        return value

    @field_validator(
        "profile_id",
        "protocol_version",
        "graph_version",
        "prompt_manifest_version",
        "tool_catalog_version",
        "skill_catalog_version",
    )
    def _validate_bounded_metadata(cls, value: str) -> str:
        if len(value) > 256:
            raise ValueError("metadata value is too long")
        return value

    @field_validator("prompt_manifest_hash", "tool_catalog_hash", "skill_catalog_hash")
    def _validate_hash(cls, value: str) -> str:
        if value and not SHA256_RE.fullmatch(value):
            raise ValueError("value must be a sha256 hash")
        return value

    @field_validator("tools")
    def _validate_tools(cls, value: list[NativeToolDefinition]) -> list[NativeToolDefinition]:
        if len(value) > MAX_TOOLS:
            raise ValueError(f"at most {MAX_TOOLS} tools are allowed")
        names = [tool.name for tool in value]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return value

    @field_validator("loaded_skill_versions")
    def _validate_loaded_skill_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 loaded Skill versions are allowed")
        for skill_id, version in value.items():
            if not IDENTIFIER_RE.fullmatch(skill_id) or not version or len(version) > 32:
                raise ValueError("loaded Skill version metadata is invalid")
        return value

    @field_validator("mcp_server_ids")
    def _validate_mcp_server_ids(cls, value: list[str]) -> list[str]:
        if len(value) > 32 or any(not IDENTIFIER_RE.fullmatch(item) for item in value):
            raise ValueError("MCP server metadata is invalid")
        if len(value) != len(set(value)):
            raise ValueError("MCP server ids must be unique")
        return value

    @field_validator("images")
    def _validate_images(cls, value: list[ImageInput]) -> list[ImageInput]:
        if len(value) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} images are allowed")
        return value

    @model_validator(mode="after")
    def _validate_tool_choice(self) -> "AgentChatRequest":
        choice = self.tool_choice
        tools = self.tools
        if choice and choice.type == "tool" and choice.name not in {tool.name for tool in tools}:
            raise ValueError("toolChoice references a tool not present in tools")
        return self


class NativeToolCall(_GatewayBaseModel):
    id: str = Field(default="", max_length=256)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    def _validate_name(cls, value: str) -> str:
        if not TOOL_NAME_RE.fullmatch(value):
            raise ValueError("invalid native tool call name")
        return value

    @field_validator("id")
    def _validate_id(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("native tool call id is invalid")
        return value

    @field_validator("arguments")
    @classmethod
    def _validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="native tool call arguments",
            max_chars=MAX_TOOL_ARGUMENTS_JSON_CHARS,
        )


class AgentChatResponse(_GatewayBaseModel):
    provider: str
    model: str
    content: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    tool_calls: list[NativeToolCall] = Field(default_factory=list, max_length=128)
    success: bool = True
    error: str | None = None
    code: str | None = None
    duration_ms: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)

    @field_validator("usage")
    @classmethod
    def _validate_usage(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="provider usage",
            max_chars=50_000,
        )

    @field_validator("provider")
    def _validate_provider(cls, value: str) -> str:
        if not PROFILE_ID_RE.fullmatch(value):
            raise ValueError("response provider is invalid")
        return value

    @model_validator(mode="after")
    def _validate_success_shape(self) -> "AgentChatResponse":
        success = self.success
        content = self.content
        calls = self.tool_calls
        error = self.error
        if success and not content.strip() and not calls:
            raise ValueError("successful response must include content or native tool calls")
        if not success and not error:
            raise ValueError("failed response must include error")
        if success and error:
            raise ValueError("successful response must not include error")
        if not MODEL_NAME_RE.fullmatch(self.model):
            raise ValueError("response model name is invalid")
        ids = [call.id for call in calls if call.id]
        if len(ids) != len(set(ids)):
            raise ValueError("native tool call ids must be unique")
        return self


class AgentChatHttpResponse(AgentChatResponse):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    sequence: int = Field(default=1, ge=1)

    @field_validator("conversation_id")
    def _validate_http_response_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_http_response_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("request_id", "correlation_id")
    def _validate_http_response_request_ids(cls, value: str) -> str:
        return validate_uuid4_id(value)


class ToolResultRequest(RequestCorrelationIdentity):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    topic_id: str = Field(default="", alias="topicId")
    checkpoint_id: str = Field(default="", alias="checkpointId")
    turn: int = Field(default=0, ge=0, le=100000)
    tool_call_id: str = Field(alias="toolCallId")
    call_id: str = Field(default="", alias="callId")
    idempotency_key: str = Field(default="", alias="idempotencyKey")
    tool_name: str = Field(alias="toolName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "failed", "rejected", "cancelled"] = "success"
    result: Any = None
    error: str = ""
    timestamp: str = ""
    duration_ms: int = Field(default=0, alias="durationMs", ge=0, le=86_400_000)
    protocol_version: str = Field(default="1.0", alias="protocolVersion")
    graph_version: str = Field(default="", alias="graphVersion")
    skill_catalog_version: str = Field(default="", alias="skillCatalogVersion")
    skill_catalog_hash: str = Field(default="", alias="skillCatalogHash")
    loaded_skill_versions: dict[str, str] = Field(default_factory=dict, alias="loadedSkillVersions")
    mcp_server_ids: list[str] = Field(default_factory=list, alias="mcpServerIds")
    trace_id: str = Field(default="", alias="traceId")
    artifact_ref: str = Field(default="", alias="artifactRef", max_length=2048)

    @field_validator("run_id")
    def _validate_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("tool_call_id", "call_id", "idempotency_key")
    def _validate_required_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("value must be a valid identifier")
        return value

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("topic_id", "checkpoint_id", "trace_id")
    def _validate_optional_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier is invalid")
        return value

    @field_validator("tool_name")
    def _validate_tool_name(cls, value: str) -> str:
        if not TOOL_NAME_RE.fullmatch(value):
            raise ValueError("tool_name is invalid")
        return value

    @field_validator("arguments")
    @classmethod
    def _validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="tool result arguments",
            max_chars=MAX_TOOL_ARGUMENTS_JSON_CHARS,
        )

    @field_validator("result")
    @classmethod
    def _validate_result(cls, value: Any) -> Any:
        return _validate_bounded_json(
            value,
            label="tool result payload",
            max_chars=MAX_TOOL_RESULT_JSON_CHARS,
        )

    @field_validator("skill_catalog_version")
    def _validate_skill_catalog_version(cls, value: str) -> str:
        if len(value) > 32:
            raise ValueError("skillCatalogVersion is too long")
        return value

    @field_validator("skill_catalog_hash")
    def _validate_skill_catalog_hash(cls, value: str) -> str:
        if value and not SHA256_RE.fullmatch(value):
            raise ValueError("skillCatalogHash must be a sha256 hash")
        return value

    @field_validator("loaded_skill_versions")
    def _validate_tool_result_skill_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 loaded Skill versions are allowed")
        if any(
            not IDENTIFIER_RE.fullmatch(skill_id) or not version or len(version) > 32
            for skill_id, version in value.items()
        ):
            raise ValueError("loaded Skill version metadata is invalid")
        return value

    @field_validator("mcp_server_ids")
    def _validate_tool_result_mcp_servers(cls, value: list[str]) -> list[str]:
        if len(value) > 32 or any(not IDENTIFIER_RE.fullmatch(item) for item in value):
            raise ValueError("MCP server metadata is invalid")
        if len(value) != len(set(value)):
            raise ValueError("MCP server ids must be unique")
        return value

    @field_validator("timestamp")
    def _validate_timestamp(cls, value: str) -> str:
        if value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp must include a timezone offset")
        return value

    @field_validator("error")
    def _validate_error_length(cls, value: str) -> str:
        if len(value) > 16_000:
            raise ValueError("tool result error is too long")
        return value

    @model_validator(mode="after")
    def _validate_status_shape(self) -> "ToolResultRequest":
        if self.status == "success" and self.error:
            raise ValueError("successful tool results cannot include error")
        if self.status != "success" and not self.error:
            raise ValueError("failed, rejected, or cancelled tool results require error")
        return self


class ToolResultResponse(_GatewayBaseModel):
    success: bool
    message: str
    duplicate: bool = False
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    sequence: int = Field(default=1, ge=1)

    @field_validator("conversation_id")
    def _validate_tool_response_conversation(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_tool_response_run(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("request_id", "correlation_id")
    def _validate_tool_response_request_ids(cls, value: str) -> str:
        return validate_uuid4_id(value)


class McpToolDefinition(_GatewayBaseModel):
    name: str
    description: str = Field(max_length=8000)
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] | None = Field(default=None, alias="outputSchema")
    annotations: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    def _validate_name(cls, value: str) -> str:
        if not MCP_TOOL_NAME_RE.fullmatch(value):
            raise ValueError("MCP tool name is invalid")
        return value

    @field_validator("input_schema", "output_schema")
    def _validate_schema(
        cls,
        value: dict[str, Any] | None,
        info: Any,
    ) -> dict[str, Any] | None:
        if value is None:
            return value
        if info.field_name == "input_schema" and value.get("type") != "object":
            raise ValueError("MCP tool inputSchema type must be object")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(
                f"MCP tool Schema is not valid JSON Schema: {exc.message}"
            ) from exc
        return _validate_bounded_json(
            value,
            label="MCP tool Schema",
            max_chars=MAX_TOOL_RESULT_JSON_CHARS,
        )

    @field_validator("annotations")
    def _validate_annotations(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="MCP tool annotations",
            max_chars=MAX_TRACE_VALUE_JSON_CHARS,
        )


class McpToolListResponse(_GatewayBaseModel):
    server_id: str = Field(alias="serverId")
    server_name: str = Field(default="", alias="serverName", max_length=128)
    server_version: str = Field(alias="serverVersion", max_length=128)
    tools: list[McpToolDefinition] = Field(default_factory=list, max_length=256)

    @field_validator("server_id")
    def _validate_server_id(cls, value: str) -> str:
        if not MCP_SERVER_ID_RE.fullmatch(value):
            raise ValueError("MCP serverId is invalid")
        return value

    @model_validator(mode="after")
    def _validate_unique_tools(self) -> "McpToolListResponse":
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("MCP tool names must be unique")
        return self


class McpToolCallRequest(RequestCorrelationIdentity):
    tool_name: str = Field(alias="toolName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    topic_id: str = Field(default="", alias="topicId")
    checkpoint_id: str = Field(default="", alias="checkpointId")
    trace_id: str = Field(default="", alias="traceId")

    @field_validator("tool_name")
    def _validate_tool_name(cls, value: str) -> str:
        if not MCP_TOOL_NAME_RE.fullmatch(value):
            raise ValueError("MCP toolName is invalid")
        return value

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("topic_id", "checkpoint_id", "trace_id")
    def _validate_optional_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier is invalid")
        return value

    @field_validator("arguments")
    def _validate_arguments_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="MCP tool arguments",
            max_chars=MAX_TOOL_RESULT_JSON_CHARS,
        )


class McpCallResult(_GatewayBaseModel):
    content: list[Any] = Field(default_factory=list, max_length=10_000)
    structured_content: Any | None = Field(default=None, alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")
    meta: dict[str, Any] = Field(default_factory=dict, alias="_meta")

    @field_validator("content", "structured_content", "meta")
    def _validate_result_json(cls, value: Any) -> Any:
        return _validate_bounded_json(
            value,
            label="MCP tool result",
            max_chars=MAX_TOOL_RESULT_JSON_CHARS,
        )


class McpToolCallResponse(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    sequence: int = Field(default=1, ge=1)
    server_id: str = Field(alias="serverId")
    server_name: str = Field(default="", alias="serverName", max_length=128)
    server_version: str = Field(alias="serverVersion", max_length=128)
    tool_name: str = Field(alias="toolName")
    result: McpCallResult
    duration_ms: int = Field(alias="durationMs", ge=0)

    @field_validator("request_id")
    def _validate_request_id(cls, value: str) -> str:
        return validate_uuid4_id(value, label="requestId")

    @field_validator("correlation_id")
    def _validate_correlation_id(cls, value: str) -> str:
        return validate_uuid4_id(value, label="correlationId")

    @field_validator("conversation_id")
    def _validate_mcp_response_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_mcp_response_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("server_id")
    def _validate_server_id(cls, value: str) -> str:
        if not MCP_SERVER_ID_RE.fullmatch(value):
            raise ValueError("MCP serverId is invalid")
        return value

    @field_validator("tool_name")
    def _validate_tool_name(cls, value: str) -> str:
        if not MCP_TOOL_NAME_RE.fullmatch(value):
            raise ValueError("MCP toolName is invalid")
        return value


class ApiProfileModel(_GatewayBaseModel):
    id: str
    name: str = ""
    priority: int = Field(default=100, ge=1, le=10_000)
    supports_images: bool = Field(default=True, alias="supportsImages")
    context_window_tokens: int = Field(
        default=100_000,
        alias="contextWindowTokens",
        ge=1_024,
        le=10_000_000,
    )
    max_output_tokens: int = Field(
        default=8_192,
        alias="maxOutputTokens",
        ge=256,
        le=1_000_000,
    )
    supports_native_tools: bool = Field(default=True, alias="supportsNativeTools")
    supports_thinking: bool = Field(default=False, alias="supportsThinking")
    supports_streaming: bool = Field(default=True, alias="supportsStreaming")

    @field_validator("id")
    def _validate_id(cls, value: str) -> str:
        if not MODEL_NAME_RE.fullmatch(value):
            raise ValueError("model id is invalid")
        return value

    @model_validator(mode="after")
    def _default_name(self) -> "ApiProfileModel":
        if not self.name:
            self.name = self.id
        if len(self.name) > 256:
            raise ValueError("model name is too long")
        return self


class ApiProfileWriteRequest(_GatewayBaseModel):
    id: str
    provider: str
    display_name: str = Field(default="", alias="displayName", max_length=256)
    protocol: Literal["openai", "claude"] = "claude"
    base_url: str = Field(alias="baseUrl")
    api_key: str = Field(default="", alias="apiKey")
    default_model: str = Field(alias="defaultModel", min_length=1, max_length=256)
    models: list[ApiProfileModel] = Field(default_factory=list, max_length=256)
    auth_mode: Literal["api-key", "bearer"] = Field(default="api-key", alias="authMode")
    timeout_seconds: float = Field(default=120.0, alias="timeoutSeconds", ge=1.0, le=600.0)
    priority: int = Field(default=100, ge=1, le=10_000)
    enabled: bool = True

    @field_validator("id")
    def _validate_id(cls, value: str) -> str:
        if not PROFILE_ID_RE.fullmatch(value):
            raise ValueError("profile id is invalid")
        return value

    @field_validator("provider")
    def _validate_provider(cls, value: str) -> str:
        if not PROFILE_ID_RE.fullmatch(value):
            raise ValueError("provider is invalid")
        return value

    @field_validator("base_url")
    def _validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("baseUrl must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("baseUrl must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("baseUrl must not contain query parameters or fragments")
        return value.rstrip("/")

    @field_validator("api_key")
    def _validate_api_key(cls, value: str) -> str:
        if value and (len(value) < 8 or len(value) > 8192):
            raise ValueError("apiKey length is invalid")
        return value

    @field_validator("default_model")
    def _validate_default_model(cls, value: str) -> str:
        if value and not MODEL_NAME_RE.fullmatch(value):
            raise ValueError("defaultModel is invalid")
        return value

    @model_validator(mode="after")
    def _validate_models(self) -> "ApiProfileWriteRequest":
        if not self.display_name:
            self.display_name = self.provider
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("profile model ids must be unique")
        if not self.models:
            self.models = [ApiProfileModel(id=self.default_model, name=self.default_model)]
        elif self.default_model not in ids:
            raise ValueError("defaultModel must be present in models")
        return self


class ApiProfileResponse(_GatewayBaseModel):
    id: str
    provider: str
    display_name: str = Field(alias="displayName")
    protocol: Literal["openai", "claude"]
    base_url: str = Field(alias="baseUrl")
    provider_path: Literal["/v1/chat/completions", "/v1/messages"] = Field(
        alias="providerPath"
    )
    default_model: str = Field(default="", alias="defaultModel")
    models: list[ApiProfileModel] = Field(default_factory=list)
    auth_mode: Literal["api-key", "bearer"] = Field(alias="authMode")
    timeout_seconds: float = Field(alias="timeoutSeconds")
    priority: int = Field(ge=1, le=10_000)
    enabled: bool
    key_configured: bool = Field(alias="keyConfigured")
    secret_masked: str = Field(default="", alias="secretMasked", max_length=16)
    source: Literal["environment", "managed"]


class DefaultModelUpdateRequest(_GatewayBaseModel):
    model_id: str = Field(alias="modelId")

    @field_validator("model_id")
    def _validate_model_id(cls, value: str) -> str:
        if not MODEL_NAME_RE.fullmatch(value):
            raise ValueError("modelId is invalid")
        return value


class ProfileSecretWriteRequest(_GatewayBaseModel):
    value: str = Field(min_length=8, max_length=8192)


class ProfileSecretStatusResponse(_GatewayBaseModel):
    profile_id: str = Field(alias="profileId")
    configured: bool
    masked: str = Field(default="", max_length=16)
    source: Literal["environment", "managed"]


class ProviderConnectionTestRequest(_GatewayBaseModel):
    model: str = ""

    @field_validator("model")
    def _validate_model(cls, value: str) -> str:
        if value and not MODEL_NAME_RE.fullmatch(value):
            raise ValueError("model is invalid")
        return value


class ProviderHealthResponse(_GatewayBaseModel):
    profile_id: str = Field(alias="profileId")
    provider: str
    status: Literal["disabled", "unconfigured", "unknown", "healthy", "unhealthy"]
    checked_at: str = Field(default="", alias="checkedAt")
    latency_ms: int = Field(default=0, alias="latencyMs", ge=0)
    error_code: str = Field(default="", alias="errorCode", max_length=128)
    message: str = Field(default="", max_length=1000)


class DeleteModelResponse(_GatewayBaseModel):
    success: bool


class TraceError(_GatewayBaseModel):
    code: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=16_000)
    retryable: bool = False


class TraceMetadata(_GatewayBaseModel):
    environment: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(alias="conversationId")
    topic_id: str = Field(default="", alias="topicId")
    checkpoint_id: str = Field(default="", alias="checkpointId")
    graph_version: str = Field(alias="graphVersion", min_length=1, max_length=32)
    protocol_version: str = Field(alias="protocolVersion", min_length=1, max_length=32)
    prompt_manifest_version: str = Field(alias="promptManifestVersion", min_length=1, max_length=32)
    prompt_manifest_hash: str = Field(alias="promptManifestHash")
    tool_catalog_version: str = Field(alias="toolCatalogVersion", min_length=1, max_length=32)
    tool_catalog_hash: str = Field(alias="toolCatalogHash")
    skill_catalog_version: str = Field(default="", alias="skillCatalogVersion", max_length=32)
    skill_catalog_hash: str = Field(default="", alias="skillCatalogHash")
    agent_mode: Literal["normal", "plan", "chat"] = Field(alias="agentMode")
    node_name: str = Field(default="", alias="nodeName", max_length=256)
    tool_name: str = Field(default="", alias="toolName", max_length=128)
    tool_version: str = Field(default="", alias="toolVersion", max_length=32)
    provider: str = Field(default="", max_length=128)
    profile_id: str = Field(default="", alias="profileId", max_length=256)
    model: str = Field(default="", max_length=256)
    skill_id: str = Field(default="", alias="skillId", max_length=128)
    skill_version: str = Field(default="", alias="skillVersion", max_length=32)
    mcp_server_id: str = Field(default="", alias="mcpServerId", max_length=128)
    mcp_tool_name: str = Field(default="", alias="mcpToolName", max_length=128)

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("topic_id", "checkpoint_id", "profile_id")
    def _validate_optional_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier is invalid")
        return value

    @field_validator("skill_id", "mcp_server_id", "mcp_tool_name")
    def _validate_optional_skill_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("Skill/MCP identifier is invalid")
        return value

    @field_validator("prompt_manifest_hash", "tool_catalog_hash", "skill_catalog_hash")
    def _validate_hash(cls, value: str) -> str:
        if value and not SHA256_RE.fullmatch(value):
            raise ValueError("trace manifest hash must be sha256")
        return value


class TraceEnvelope(_GatewayBaseModel):
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    event_id: str = Field(alias="eventId")
    trace_id: str = Field(alias="traceId")
    trace_run_id: str = Field(alias="traceRunId")
    parent_trace_run_id: str = Field(default="", alias="parentTraceRunId")
    agent_run_id: str = Field(alias="agentRunId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    retry_of: str = Field(default="", alias="retryOf")
    name: str = Field(min_length=1, max_length=512)
    run_type: Literal["graph", "chain", "llm", "tool", "retriever", "interrupt"] = Field(alias="runType")
    event: Literal["start", "end", "error"]
    timestamp: str
    inputs: Any = None
    outputs: Any = None
    error: TraceError | None = None
    tags: list[str] = Field(default_factory=list, max_length=100)
    metadata: TraceMetadata

    @field_validator("event_id", "trace_id", "trace_run_id")
    def _validate_required_identifier(cls, value: str) -> str:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("trace identifier is invalid")
        return value

    @field_validator("agent_run_id")
    def _validate_agent_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("parent_trace_run_id")
    def _validate_optional_identifier(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("trace identifier is invalid")
        return value

    @field_validator("request_id")
    def _validate_request_id(cls, value: str) -> str:
        return validate_uuid4_id(value, label="requestId")

    @field_validator("correlation_id", "retry_of")
    def _validate_trace_correlation_ids(cls, value: str) -> str:
        return validate_uuid4_id(value) if value else value

    @field_validator("timestamp")
    def _validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("trace timestamp must include a timezone offset")
        return value

    @field_validator("tags")
    def _validate_tags(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("trace tags must be non-blank and at most 256 characters")
        return value

    @field_validator("inputs", "outputs")
    @classmethod
    def _validate_trace_value(cls, value: Any, info: Any) -> Any:
        return _validate_bounded_json(
            value,
            label=f"trace {info.field_name}",
            max_chars=MAX_TRACE_VALUE_JSON_CHARS,
        )

    @model_validator(mode="after")
    def _validate_error_event(self) -> "TraceEnvelope":
        if self.event == "error" and self.error is None:
            raise ValueError("trace error events require error details")
        return self


class TraceBatchRequest(RequestCorrelationIdentity):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    sequence: int = Field(ge=1)
    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    content_level: Literal["metadata", "sanitized", "full"] = Field(
        alias="contentLevel"
    )
    sent_at: str = Field(alias="sentAt")
    events: list[TraceEnvelope] = Field(default_factory=list)

    @field_validator("sent_at")
    def _validate_sent_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("sentAt must include a timezone offset")
        return value

    @field_validator("conversation_id")
    def _validate_batch_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_batch_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("events")
    def _validate_events(cls, value: list[TraceEnvelope]) -> list[TraceEnvelope]:
        if not value:
            raise ValueError("events must not be empty")
        if len(value) > 500:
            raise ValueError("at most 500 trace events are allowed")
        serialized_size = sum(
            len(event.model_dump_json(by_alias=True))
            for event in value
        )
        if serialized_size > MAX_TRACE_BATCH_JSON_CHARS:
            raise ValueError(
                f"trace batch exceeds {MAX_TRACE_BATCH_JSON_CHARS} JSON characters"
            )
        return value

    @model_validator(mode="after")
    def _validate_single_runtime_identity(self) -> "TraceBatchRequest":
        conversations = {event.metadata.conversation_id for event in self.events}
        runs = {event.agent_run_id for event in self.events}
        if (
            len(conversations) != 1
            or len(runs) != 1
            or conversations != {self.conversation_id}
            or runs != {self.run_id}
        ):
            raise ValueError("trace batch events must share one conversationId and agentRunId")
        return self


class TraceBatchResponse(_GatewayBaseModel):
    export_batch_id: str = Field(alias="exportBatchId")
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    sequence: int = Field(default=1, ge=1)
    accepted: int
    rejected: int
    status: Literal["accepted", "partial", "disabled"]

    @field_validator("conversation_id")
    def _validate_trace_response_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_trace_response_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("request_id", "correlation_id")
    def _validate_trace_response_request_ids(cls, value: str) -> str:
        return validate_uuid4_id(value)


class HealthResponse(_GatewayBaseModel):
    status: Literal["ok"]
    runtime: Literal["thin-gateway"]
    protocol_version: Literal["1.0"] = Field(alias="protocolVersion")
    langsmith_enabled: bool = Field(alias="langsmithEnabled")
    mcp_proxy_enabled: bool = Field(alias="mcpProxyEnabled")
    full_conversation_log_enabled: bool = Field(alias="fullConversationLogEnabled")


class RetryPolicyResponse(_GatewayBaseModel):
    mode: Literal["retryable-failover"] = "retryable-failover"
    attempts_per_route: Literal[1] = Field(default=1, alias="attemptsPerRoute")
    retryable_errors_only: Literal[True] = Field(default=True, alias="retryableErrorsOnly")
    partial_stream_failover: Literal[False] = Field(default=False, alias="partialStreamFailover")


class AdminRuntimePolicyResponse(_GatewayBaseModel):
    profile_allowed_hosts: list[str] = Field(alias="profileAllowedHosts", max_length=256)
    mcp_allowed_hosts: list[str] = Field(alias="mcpAllowedHosts", max_length=256)
    provider_paths: dict[Literal["openai", "claude"], str] = Field(alias="providerPaths")
    retry_policy: RetryPolicyResponse = Field(alias="retryPolicy")


class ModelCapability(_GatewayBaseModel):
    id: str
    name: str
    provider: str
    priority: int = Field(ge=1, le=10_000)
    supports_images: bool = Field(alias="supportsImages")
    context_window_tokens: int = Field(alias="contextWindowTokens", gt=0)
    max_output_tokens: int = Field(alias="maxOutputTokens", gt=0)
    supports_native_tools: bool = Field(alias="supportsNativeTools")
    supports_thinking: bool = Field(alias="supportsThinking")
    supports_streaming: bool = Field(alias="supportsStreaming")


class ProviderCapability(_GatewayBaseModel):
    display_name: str = Field(alias="displayName")
    protocol: Literal["openai", "claude"]
    default_profile_id: str = Field(alias="defaultProfileId")
    status: Literal["ready", "unavailable"]
    default_model: str = Field(alias="defaultModel")
    priority: int = Field(ge=1, le=10_000)
    models: list[ModelCapability] = Field(default_factory=list, max_length=256)
    error: str = Field(default="", max_length=4000)


class RoutingOrderItem(_GatewayBaseModel):
    position: int = Field(ge=1)
    provider: str
    profile_id: str = Field(alias="profileId")
    protocol: Literal["openai", "claude"]
    model: str
    profile_priority: int = Field(alias="profilePriority", ge=1, le=10_000)
    model_priority: int = Field(alias="modelPriority", ge=1, le=10_000)
    is_default_profile: bool = Field(alias="isDefaultProfile")
    is_default_model: bool = Field(alias="isDefaultModel")


class AgentModelsResponse(_GatewayBaseModel):
    default_provider: str = Field(alias="defaultProvider")
    default_model: str = Field(alias="defaultModel")
    supported_protocol_versions: list[Literal["1.0"]] = Field(alias="supportedProtocolVersions")
    providers: dict[str, ProviderCapability]
    routing_order: list[RoutingOrderItem] = Field(default_factory=list, alias="routingOrder")


class RequestLogEntry(_GatewayBaseModel):
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    parent_request_id: str = Field(default="", alias="parentRequestId")
    retry_of: str = Field(default="", alias="retryOf")
    timestamp: str
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    trace_id: str = Field(default="", alias="traceId")
    endpoint: str = Field(default="", max_length=512)
    provider: str
    profile_id: str = Field(alias="profileId")
    protocol: Literal["openai", "claude"]
    model: str
    attempt: int = Field(default=1, ge=1, le=10_000)
    stream: bool
    status: Literal["success", "error", "cancelled"]
    duration_ms: int = Field(alias="durationMs", ge=0)
    http_status: int = Field(default=200, alias="httpStatus", ge=0, le=599)
    error_code: str = Field(default="", alias="errorCode", max_length=256)
    error_message: str = Field(default="", alias="errorMessage", max_length=4_000)
    prompt_characters: int = Field(alias="promptCharacters", ge=0)
    prompt_sha256: str = Field(alias="promptSha256")
    system_prompt_characters: int = Field(alias="systemPromptCharacters", ge=0)
    system_prompt_sha256: str = Field(alias="systemPromptSha256")
    image_count: int = Field(alias="imageCount", ge=0)
    tool_names: list[str] = Field(default_factory=list, alias="toolNames", max_length=64)
    response_tool_names: list[str] = Field(default_factory=list, alias="responseToolNames", max_length=128)
    input_characters: int = Field(alias="inputCharacters", ge=0)
    output_characters: int = Field(alias="outputCharacters", ge=0)
    finish_reason: str = Field(default="", alias="finishReason", max_length=256)
    stop_reason: str = Field(default="", alias="stopReason", max_length=256)
    usage: dict[str, Any] = Field(default_factory=dict)
    request_metadata: dict[str, Any] = Field(default_factory=dict, alias="requestMetadata")
    response_metadata: dict[str, Any] = Field(default_factory=dict, alias="responseMetadata")

    @field_validator("request_id")
    def _validate_request_id(cls, value: str) -> str:
        try:
            return validate_uuid4_id(value, label="requestId")
        except ValueError:
            return validate_safe_request_id(value)

    @field_validator("correlation_id", "parent_request_id", "retry_of")
    def _validate_history_correlation_ids(cls, value: str) -> str:
        if not value:
            return value
        try:
            return validate_uuid4_id(value)
        except ValueError:
            return validate_safe_request_id(value)

    @field_validator("request_metadata", "response_metadata")
    @classmethod
    def _validate_log_metadata(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label=info.field_name,
            max_chars=MAX_TRACE_VALUE_JSON_CHARS,
        )

    @field_validator("conversation_id")
    def _validate_history_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)

    @field_validator("run_id")
    def _validate_history_run_id(cls, value: str) -> str:
        return validate_history_run_id(value)


class RequestLogListResponse(_GatewayBaseModel):
    items: list[RequestLogEntry]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class RequestLogDetailResponse(RequestLogEntry):
    attempt_count: int = Field(alias="attemptCount", ge=1, le=10_000)
    attempts: list[RequestLogEntry] = Field(min_length=1, max_length=10_000)


class DeleteProfileResponse(_GatewayBaseModel):
    success: bool


class AdminLoginRequest(_GatewayBaseModel):
    password: str = Field(min_length=1, max_length=8192)


class AdminLoginResponse(_GatewayBaseModel):
    session_token: str = Field(alias="sessionToken", min_length=32, max_length=8192)
    expires_at: str = Field(alias="expiresAt")
    expires_in_seconds: int = Field(alias="expiresInSeconds", ge=300, le=86_400)


class AccessKeyRotateRequest(_GatewayBaseModel):
    api_key: str = Field(default="", alias="apiKey", max_length=8192)


class AccessKeyStatusResponse(_GatewayBaseModel):
    configured: bool
    masked: str = Field(default="", max_length=64)


class AccessKeyRotateResponse(AccessKeyStatusResponse):
    api_key: str = Field(alias="apiKey", min_length=16, max_length=8192)


class AuditLogEntry(_GatewayBaseModel):
    timestamp: str
    action: str = Field(max_length=128)
    success: bool
    actor: str = Field(max_length=256)
    request_id: str = Field(alias="requestId", min_length=1, max_length=256)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_id")
    def _validate_request_id(cls, value: str) -> str:
        return validate_safe_request_id(value)


class AuditLogListResponse(_GatewayBaseModel):
    items: list[AuditLogEntry]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class ConversationSummary(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    created_at: str = Field(default="", alias="createdAt")
    started_at: str = Field(alias="startedAt")
    updated_at: str = Field(alias="updatedAt")
    title: str = Field(default="", max_length=512)
    user_id: str = Field(default="", alias="userId", max_length=256)
    topic_id: str = Field(default="", alias="topicId", max_length=256)
    first_run_id: str = Field(default="", alias="firstRunId", max_length=256)
    latest_run_id: str = Field(default="", alias="latestRunId", max_length=256)
    size_bytes: int = Field(alias="sizeBytes", ge=0)
    event_count: int = Field(alias="eventCount", ge=0)
    message_count: int = Field(default=0, alias="messageCount", ge=0)
    tool_result_count: int = Field(default=0, alias="toolResultCount", ge=0)
    last_sequence: int = Field(default=0, alias="lastSequence", ge=0)
    status: Literal["open", "completed", "aborted"] = "open"
    incomplete_reason: str = Field(default="", alias="incompleteReason", max_length=256)

    @field_validator("conversation_id")
    def _validate_history_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)


class ConversationListResponse(_GatewayBaseModel):
    items: list[ConversationSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class ConversationEventsResponse(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    items: list[dict[str, Any]] = Field(max_length=500)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)

    @field_validator("conversation_id")
    def _validate_history_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)


class MessageRecord(_GatewayBaseModel):
    message_id: str = Field(alias="messageId")
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(default="", alias="correlationId")
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    tool_calls: list[NativeToolCall] = Field(default_factory=list, alias="toolCalls", max_length=128)
    tool_result_refs: list[str] = Field(default_factory=list, alias="toolResultRefs", max_length=256)
    incomplete_reason: str = Field(default="", alias="incompleteReason", max_length=256)
    sequence: int = Field(gt=0)
    created_at: str = Field(alias="createdAt")

    @field_validator("message_id")
    def _validate_message_id(cls, value: str) -> str:
        return validate_uuid4_id(value, label="messageId")

    @field_validator("conversation_id")
    def _validate_message_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)

    @field_validator("run_id")
    def _validate_message_run_id(cls, value: str) -> str:
        return validate_history_run_id(value)

    @field_validator("request_id", "correlation_id")
    def _validate_message_request_ids(cls, value: str) -> str:
        return validate_uuid4_id(value) if value else value


class ConversationTranscriptResponse(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    messages: list[MessageRecord] = Field(max_length=10_000)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
    incomplete_reason: str = Field(default="", alias="incompleteReason", max_length=256)

    @field_validator("conversation_id")
    def _validate_transcript_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)


class ToolResultLogRecord(_GatewayBaseModel):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    parent_request_id: str = Field(default="", alias="parentRequestId")
    retry_of: str = Field(default="", alias="retryOf")
    topic_id: str = Field(default="", alias="topicId", max_length=256)
    checkpoint_id: str = Field(default="", alias="checkpointId", max_length=256)
    protocol_version: str = Field(default="1.0", alias="protocolVersion", max_length=32)
    graph_version: str = Field(default="", alias="graphVersion", max_length=32)
    skill_catalog_version: str = Field(
        default="", alias="skillCatalogVersion", max_length=32
    )
    skill_catalog_hash: str = Field(default="", alias="skillCatalogHash", max_length=71)
    loaded_skill_versions: dict[str, str] = Field(
        default_factory=dict,
        alias="loadedSkillVersions",
        max_length=32,
    )
    mcp_server_ids: list[str] = Field(
        default_factory=list,
        alias="mcpServerIds",
        max_length=32,
    )
    tool_call_id: str = Field(alias="toolCallId")
    call_id: str = Field(alias="callId")
    idempotency_key: str = Field(alias="idempotencyKey")
    tool_name: str = Field(alias="toolName")
    status: Literal["success", "failed", "rejected", "cancelled"]
    trace_id: str = Field(default="", alias="traceId")
    turn: int = Field(default=0, ge=0)
    duplicate: bool = False
    success: bool
    duration_ms: int = Field(default=0, alias="durationMs", ge=0)
    sanitized_input: dict[str, Any] = Field(alias="sanitizedInput")
    sanitized_output: Any = Field(alias="sanitizedOutput")
    result_metadata: dict[str, Any] = Field(alias="resultMetadata")
    artifact_ref: str = Field(default="", alias="artifactRef", max_length=2048)
    source_artifact_ref: str = Field(
        default="", alias="sourceArtifactRef", max_length=2048
    )
    inline_result: Any = Field(default=None, alias="inlineResult")
    error: str = Field(default="", max_length=16_000)
    created_at: str = Field(alias="createdAt")

    @field_validator("conversation_id")
    def _validate_tool_log_conversation_id(cls, value: str) -> str:
        return validate_history_conversation_id(value)

    @field_validator("run_id")
    def _validate_tool_log_run_id(cls, value: str) -> str:
        return validate_history_run_id(value)

    @field_validator("request_id", "correlation_id", "parent_request_id", "retry_of")
    def _validate_tool_log_request_ids(cls, value: str) -> str:
        return validate_uuid4_id(value) if value else value

    @field_validator("topic_id", "checkpoint_id")
    def _validate_tool_log_optional_ids(cls, value: str) -> str:
        if value and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("tool result audit identifier is invalid")
        return value

    @field_validator("skill_catalog_hash")
    def _validate_tool_log_skill_catalog_hash(cls, value: str) -> str:
        if value and not SHA256_RE.fullmatch(value):
            raise ValueError("skillCatalogHash must be a sha256 hash")
        return value

    @field_validator("loaded_skill_versions")
    def _validate_tool_log_skill_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not IDENTIFIER_RE.fullmatch(skill_id) or not version or len(version) > 32
            for skill_id, version in value.items()
        ):
            raise ValueError("loaded Skill version metadata is invalid")
        return value

    @field_validator("mcp_server_ids")
    def _validate_tool_log_mcp_servers(cls, value: list[str]) -> list[str]:
        if any(not IDENTIFIER_RE.fullmatch(item) for item in value):
            raise ValueError("MCP server metadata is invalid")
        if len(value) != len(set(value)):
            raise ValueError("MCP server ids must be unique")
        return value

    @field_validator("result_metadata")
    @classmethod
    def _validate_result_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="tool result metadata",
            max_chars=MAX_TRACE_VALUE_JSON_CHARS,
        )

    @field_validator("sanitized_input")
    @classmethod
    def _validate_sanitized_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="sanitized tool input",
            max_chars=MAX_TOOL_ARGUMENTS_JSON_CHARS,
        )

    @field_validator("sanitized_output")
    @classmethod
    def _validate_sanitized_output(cls, value: Any) -> Any:
        return _validate_bounded_json(
            value,
            label="sanitized tool output",
            max_chars=MAX_TRACE_VALUE_JSON_CHARS,
        )

    @field_validator("inline_result")
    @classmethod
    def _validate_inline_result(cls, value: Any) -> Any:
        return _validate_bounded_json(
            value,
            label="inline tool result",
            max_chars=32_768,
        )


class ConversationSealRequest(_GatewayBaseModel):
    status: Literal["completed", "aborted"]


class ConversationSealResponse(_GatewayBaseModel):
    success: bool
    status: Literal["completed", "aborted"]


class GatewayEventEnvelope(_GatewayBaseModel):
    protocol_version: Literal["1.0"] = Field(alias="protocolVersion")
    sequence: int = Field(gt=0)
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(alias="runId")
    trace_id: str = Field(alias="traceId")
    request_id: str = Field(alias="requestId")
    correlation_id: str = Field(alias="correlationId")
    timestamp: str
    event_type: Literal["model.delta", "model.completed", "gateway.error"] = Field(alias="type")
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_bounded_json(
            value,
            label="gateway event payload",
            max_chars=MAX_TOOL_RESULT_JSON_CHARS + MAX_TOOL_ARGUMENTS_JSON_CHARS,
        )

    @field_validator("conversation_id")
    def _validate_conversation_id(cls, value: str) -> str:
        return validate_conversation_uuid4(value)

    @field_validator("run_id")
    def _validate_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @field_validator("trace_id")
    def _validate_identifier(cls, value: str) -> str:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("gateway event identifier is invalid")
        return value

    @field_validator("request_id", "correlation_id")
    def _validate_request_correlation_ids(cls, value: str) -> str:
        return validate_uuid4_id(value)

    @field_validator("timestamp")
    def _validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("gateway event timestamp must include a timezone offset")
        return value
