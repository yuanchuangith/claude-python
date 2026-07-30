"""OpenAI and Anthropic wire adapters for the existing thin gateway runtime.

The functions in this module only translate protocol shapes.  Provider/Profile
selection, failover, secrets, logging, and execution remain owned by the
existing ``AgentChatRequest`` call chain.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import AgentChatRequest, AgentChatResponse, ImageInput

_DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
_MAX_COMPAT_ITEMS = 10_000
_MAX_COMPAT_JSON_CHARS = 2_000_000


class ProtocolCompatibilityError(ValueError):
    """A request cannot be represented by the canonical gateway model."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        param: str = "",
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param
        self.status_code = status_code


class _CompatibilityRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class OpenAIStreamOptions(_CompatibilityRequest):
    include_usage: bool = False


class OpenAIChatCompletionRequest(_CompatibilityRequest):
    model: str
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=_MAX_COMPAT_ITEMS)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)
    stream: bool = False
    stream_options: OpenAIStreamOptions | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    tool_choice: str | dict[str, Any] | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    user: str = Field(default="", max_length=256)

    @field_validator("messages", "tools")
    def _validate_json_fields(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _validate_compat_json(value)
        return value

    @model_validator(mode="after")
    def _validate_token_aliases(self) -> "OpenAIChatCompletionRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens and max_completion_tokens are mutually exclusive")
        return self


class AnthropicMessagesRequest(_CompatibilityRequest):
    model: str
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=_MAX_COMPAT_ITEMS)
    max_tokens: int = Field(ge=1, le=32768)
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    tool_choice: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("messages", "tools")
    def _validate_json_lists(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _validate_compat_json(value)
        return value

    @field_validator("system", "tool_choice", "thinking", "metadata")
    def _validate_optional_json(cls, value: Any) -> Any:
        if value is not None:
            _validate_compat_json(value)
        return value


def openai_to_agent_request(
    payload: OpenAIChatCompletionRequest | Mapping[str, Any],
    **identity: str,
) -> AgentChatRequest:
    """Convert one OpenAI Chat Completions request to the canonical model."""

    request = (
        payload
        if isinstance(payload, OpenAIChatCompletionRequest)
        else OpenAIChatCompletionRequest.model_validate(payload)
    )
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    images: list[ImageInput] = []
    last_user_index = max(
        (
            index
            for index, message in enumerate(request.messages)
            if message.get("role") == "user"
        ),
        default=-1,
    )
    for index, message in enumerate(request.messages):
        role = _required_string(message, "role", f"messages.{index}.role")
        if role in {"system", "developer"}:
            system_parts.append(
                _openai_content_text(
                    message.get("content"),
                    f"messages.{index}.content",
                )
            )
            continue
        if role == "user":
            text, found_images = _openai_user_content(
                message.get("content"),
                f"messages.{index}.content",
            )
            if found_images and index != last_user_index:
                raise ProtocolCompatibilityError(
                    "unsupported_message_content",
                    "Only the most recent user message may contain images",
                    param=f"messages.{index}.content",
                )
            images.extend(found_images)
            messages.append(
                {
                    "role": "user",
                    "content": text,
                    **_optional_message_name(message),
                }
            )
            continue
        if role == "assistant":
            content = _openai_content_text(
                message.get("content"),
                f"messages.{index}.content",
                allow_none=True,
            )
            tool_calls = _openai_tool_calls(
                message.get("tool_calls") or [],
                f"messages.{index}.tool_calls",
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "toolCalls": tool_calls,
                }
            )
            continue
        if role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "content": _openai_content_text(
                        message.get("content"),
                        f"messages.{index}.content",
                        allow_none=True,
                    ),
                    "toolCallId": _required_string(
                        message,
                        "tool_call_id",
                        f"messages.{index}.tool_call_id",
                    ),
                    **_optional_message_name(message),
                }
            )
            continue
        raise ProtocolCompatibilityError(
            "unsupported_message_role",
            f"Unsupported OpenAI message role: {role}",
            param=f"messages.{index}.role",
        )

    tools = _openai_tools(request.tools)
    max_tokens = request.max_completion_tokens or request.max_tokens or 8192
    return AgentChatRequest.model_validate(
        {
            **_agent_identity(identity),
            "provider": identity.get("provider", "auto"),
            "profileId": identity.get("profile_id", ""),
            "model": request.model,
            "userId": request.user,
            "systemPrompt": _system_prompt(system_parts),
            "prompt": _latest_prompt(messages),
            "messages": messages,
            "images": [image.model_dump(mode="json") for image in images],
            "stream": request.stream,
            "tools": tools,
            "toolChoice": _openai_tool_choice(request.tool_choice),
            "thinking": _openai_thinking(request.reasoning_effort),
            "maxTokens": max_tokens,
        }
    )


def anthropic_to_agent_request(
    payload: AnthropicMessagesRequest | Mapping[str, Any],
    **identity: str,
) -> AgentChatRequest:
    """Convert one Anthropic Messages request to the canonical model."""

    request = (
        payload
        if isinstance(payload, AnthropicMessagesRequest)
        else AnthropicMessagesRequest.model_validate(payload)
    )
    system_parts = _anthropic_system_parts(request.system)
    messages: list[dict[str, Any]] = []
    images: list[ImageInput] = []
    last_user_index = max(
        (
            index
            for index, message in enumerate(request.messages)
            if message.get("role") == "user"
        ),
        default=-1,
    )
    for index, message in enumerate(request.messages):
        role = _required_string(message, "role", f"messages.{index}.role")
        if role not in {"user", "assistant"}:
            raise ProtocolCompatibilityError(
                "unsupported_message_role",
                f"Unsupported Anthropic message role: {role}",
                param=f"messages.{index}.role",
            )
        content = message.get("content", "")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list):
            raise ProtocolCompatibilityError(
                "invalid_message_content",
                "Anthropic message content must be a string or an array",
                param=f"messages.{index}.content",
            )
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block_index, block in enumerate(blocks):
            param = f"messages.{index}.content.{block_index}"
            if not isinstance(block, dict):
                raise ProtocolCompatibilityError(
                    "invalid_content_block",
                    "Anthropic content blocks must be objects",
                    param=param,
                )
            block_type = _required_string(block, "type", f"{param}.type")
            if block_type == "text":
                text_parts.append(_required_string(block, "text", f"{param}.text"))
            elif block_type == "image":
                if role != "user" or index != last_user_index:
                    raise ProtocolCompatibilityError(
                        "unsupported_message_content",
                        "Only the most recent user message may contain images",
                        param=param,
                    )
                images.append(_anthropic_image(block, param))
            elif block_type == "tool_use" and role == "assistant":
                arguments = block.get("input", {})
                if not isinstance(arguments, dict):
                    raise ProtocolCompatibilityError(
                        "invalid_tool_arguments",
                        "Anthropic tool input must be an object",
                        param=f"{param}.input",
                    )
                tool_calls.append(
                    {
                        "id": _required_string(block, "id", f"{param}.id"),
                        "name": _required_string(block, "name", f"{param}.name"),
                        "arguments": arguments,
                    }
                )
            elif block_type == "tool_result" and role == "user":
                is_error = block.get("is_error", False)
                if not isinstance(is_error, bool):
                    raise ProtocolCompatibilityError(
                        "invalid_tool_result",
                        "tool_result.is_error must be a boolean",
                        param=f"{param}.is_error",
                    )
                tool_results.append(
                    {
                        "role": "tool",
                        "content": _anthropic_tool_result_text(block.get("content", ""), param),
                        "toolCallId": _required_string(
                            block,
                            "tool_use_id",
                            f"{param}.tool_use_id",
                        ),
                        "isError": is_error,
                    }
                )
            else:
                raise ProtocolCompatibilityError(
                    "unsupported_content_block",
                    f"Unsupported Anthropic {role} content block: {block_type}",
                    param=param,
                )
        messages.extend(tool_results)
        text = "".join(text_parts)
        if text or tool_calls or (role == "user" and not tool_results):
            messages.append(
                {
                    "role": role,
                    "content": text,
                    **({"toolCalls": tool_calls} if tool_calls else {}),
                }
            )

    return AgentChatRequest.model_validate(
        {
            **_agent_identity(identity),
            "provider": identity.get("provider", "auto"),
            "profileId": identity.get("profile_id", ""),
            "model": request.model,
            "systemPrompt": _system_prompt(system_parts),
            "prompt": _latest_prompt(messages),
            "messages": messages,
            "images": [image.model_dump(mode="json") for image in images],
            "stream": request.stream,
            "tools": _anthropic_tools(request.tools),
            "toolChoice": _anthropic_tool_choice(request.tool_choice),
            "thinking": _anthropic_thinking(request.thinking),
            "maxTokens": request.max_tokens,
        }
    )


def openai_models_response(payload: Mapping[str, Any], *, created: int = 0) -> dict[str, Any]:
    """Convert the gateway /models payload to an OpenAI model list."""

    candidates: list[tuple[str, str]] = []
    routing_order = payload.get("routingOrder")
    if isinstance(routing_order, list):
        for item in routing_order:
            if not isinstance(item, dict):
                continue
            model = item.get("model")
            provider = item.get("provider")
            if isinstance(model, str) and model:
                candidates.append((model, str(provider or "actiondesign")))
    providers = payload.get("providers")
    if isinstance(providers, dict):
        for provider, metadata in providers.items():
            if not isinstance(metadata, dict):
                continue
            for model in metadata.get("models") or []:
                if isinstance(model, dict) and isinstance(model.get("id"), str):
                    candidates.append((model["id"], str(provider)))
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model, provider in candidates:
        if model in seen:
            continue
        seen.add(model)
        data.append(
            {
                "id": model,
                "object": "model",
                "created": created,
                "owned_by": provider,
            }
        )
    return {"object": "list", "data": data}


def openai_chat_completion_response(
    response: AgentChatResponse | Mapping[str, Any],
    *,
    completion_id: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    normalized = _agent_response(response)
    return {
        "id": completion_id or f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()) if created is None else created,
        "model": normalized.model,
        "choices": [
            {
                "index": 0,
                "message": _openai_assistant_message(normalized),
                "finish_reason": _openai_finish_reason(normalized),
            }
        ],
        "usage": _openai_usage(normalized.usage),
    }


def anthropic_message_response(
    response: AgentChatResponse | Mapping[str, Any],
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    normalized = _agent_response(response)
    return {
        "id": message_id or f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": normalized.model,
        "content": _anthropic_response_blocks(normalized),
        "stop_reason": _anthropic_stop_reason(normalized),
        "stop_sequence": None,
        "usage": _anthropic_usage(normalized.usage),
    }


async def openai_stream_response(
    events: AsyncIterable[dict[str, Any] | str | bytes],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
    include_usage: bool = False,
) -> AsyncIterator[str]:
    completion_id = completion_id or f"chatcmpl-{uuid4().hex}"
    created = int(time.time()) if created is None else created
    yield _openai_sse(
        _openai_chunk(completion_id, created, model, {"role": "assistant"})
    )
    async for kind, payload in _normalized_stream_events(events):
        if kind == "delta":
            content = str(payload.get("content") or "")
            if content:
                yield _openai_sse(
                    _openai_chunk(completion_id, created, model, {"content": content})
                )
            continue
        if kind == "error":
            yield _openai_sse(openai_error_response(payload)[0])
            yield "data: [DONE]\n\n"
            return
        response = _agent_response(payload)
        for index, call in enumerate(response.tool_calls):
            yield _openai_sse(
                _openai_chunk(
                    completion_id,
                    created,
                    response.model,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        call.arguments,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                        ]
                    },
                )
            )
        yield _openai_sse(
            _openai_chunk(
                completion_id,
                created,
                response.model,
                {},
                finish_reason=_openai_finish_reason(response),
            )
        )
        if include_usage:
            yield _openai_sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": response.model,
                    "choices": [],
                    "usage": _openai_usage(response.usage),
                }
            )
        yield "data: [DONE]\n\n"
        return
    yield _openai_sse(
        openai_error_response(
            {"code": "PROVIDER_STREAM_INVALID", "message": "Stream ended without completion"}
        )[0]
    )
    yield "data: [DONE]\n\n"


async def anthropic_stream_response(
    events: AsyncIterable[dict[str, Any] | str | bytes],
    *,
    model: str,
    message_id: str | None = None,
) -> AsyncIterator[str]:
    message_id = message_id or f"msg_{uuid4().hex}"
    yield _anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    text_started = False
    next_index = 0
    async for kind, payload in _normalized_stream_events(events):
        if kind == "delta":
            content = str(payload.get("content") or "")
            if not content:
                continue
            if not text_started:
                yield _anthropic_sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": next_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                text_started = True
            yield _anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": next_index,
                    "delta": {"type": "text_delta", "text": content},
                },
            )
            continue
        if kind == "error":
            yield _anthropic_sse("error", anthropic_error_response(payload)[0])
            return
        response = _agent_response(payload)
        if text_started:
            yield _anthropic_sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": next_index},
            )
            next_index += 1
        for call in response.tool_calls:
            yield _anthropic_sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": next_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": {},
                    },
                },
            )
            yield _anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": next_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                },
            )
            yield _anthropic_sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": next_index},
            )
            next_index += 1
        yield _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _anthropic_stop_reason(response),
                    "stop_sequence": None,
                },
                "usage": {
                    "output_tokens": _anthropic_usage(response.usage)["output_tokens"]
                },
            },
        )
        yield _anthropic_sse("message_stop", {"type": "message_stop"})
        return
    yield _anthropic_sse(
        "error",
        anthropic_error_response(
            {"code": "PROVIDER_STREAM_INVALID", "message": "Stream ended without completion"}
        )[0],
    )


def openai_error_response(
    error: Mapping[str, Any] | Exception,
    *,
    status_code: int | None = None,
) -> tuple[dict[str, Any], int]:
    code, message, param, resolved_status = _error_fields(error, status_code)
    if resolved_status == 401:
        error_type = "authentication_error"
    elif resolved_status == 429:
        error_type = "rate_limit_error"
    elif resolved_status in {400, 404, 409, 422}:
        error_type = "invalid_request_error"
    else:
        error_type = "api_error"
    return (
        {
            "error": {
                "message": message,
                "type": error_type,
                "param": param or None,
                "code": code,
            }
        },
        resolved_status,
    )


def anthropic_error_response(
    error: Mapping[str, Any] | Exception,
    *,
    status_code: int | None = None,
) -> tuple[dict[str, Any], int]:
    _, message, _, resolved_status = _error_fields(error, status_code)
    if resolved_status == 401:
        error_type = "authentication_error"
    elif resolved_status == 429:
        error_type = "rate_limit_error"
    elif resolved_status in {400, 404, 409, 422}:
        error_type = "invalid_request_error"
    else:
        error_type = "api_error"
    return (
        {"type": "error", "error": {"type": error_type, "message": message}},
        resolved_status,
    )


def _agent_identity(identity: Mapping[str, str]) -> dict[str, str]:
    conversation_id = identity.get("conversation_id") or str(uuid4())
    request_id = identity.get("request_id") or str(uuid4())
    return {
        "conversationId": conversation_id,
        "runId": identity.get("run_id") or f"run_{uuid4()}",
        "requestId": request_id,
        "correlationId": identity.get("correlation_id") or request_id,
        "retryOf": identity.get("retry_of", ""),
        "parentRequestId": identity.get("parent_request_id", ""),
    }


def _system_prompt(parts: list[str]) -> str:
    value = "\n\n".join(part for part in parts if part.strip()).strip()
    return value or _DEFAULT_SYSTEM_PROMPT


def _latest_prompt(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") not in {"user", "tool"}:
            continue
        content = str(message.get("content") or "")
        if content.strip():
            return content
    return "Continue the conversation."


def _optional_message_name(message: Mapping[str, Any]) -> dict[str, str]:
    name = message.get("name")
    if name is None:
        return {}
    if not isinstance(name, str) or not name:
        raise ProtocolCompatibilityError(
            "invalid_message_name",
            "Message name must be a non-empty string",
            param="messages.name",
        )
    return {"name": name}


def _openai_content_text(value: Any, param: str, *, allow_none: bool = False) -> str:
    if value is None and allow_none:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ProtocolCompatibilityError(
            "invalid_message_content",
            "OpenAI message content must be a string or an array",
            param=param,
        )
    parts: list[str] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict) or block.get("type") not in {"text", "input_text"}:
            raise ProtocolCompatibilityError(
                "unsupported_message_content",
                "This message only supports text content blocks",
                param=f"{param}.{index}",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise ProtocolCompatibilityError(
                "invalid_message_content",
                "Text content must be a string",
                param=f"{param}.{index}.text",
            )
        parts.append(text)
    return "".join(parts)


def _openai_user_content(value: Any, param: str) -> tuple[str, list[ImageInput]]:
    if isinstance(value, str):
        return value, []
    if not isinstance(value, list):
        raise ProtocolCompatibilityError(
            "invalid_message_content",
            "OpenAI user content must be a string or an array",
            param=param,
        )
    text_parts: list[str] = []
    images: list[ImageInput] = []
    for index, block in enumerate(value):
        block_param = f"{param}.{index}"
        if not isinstance(block, dict):
            raise ProtocolCompatibilityError(
                "invalid_content_block",
                "OpenAI content blocks must be objects",
                param=block_param,
            )
        block_type = block.get("type")
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise ProtocolCompatibilityError(
                    "invalid_message_content",
                    "Text content must be a string",
                    param=f"{block_param}.text",
                )
            text_parts.append(text)
            continue
        if block_type == "image_url":
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise ProtocolCompatibilityError(
                    "invalid_image_url",
                    "image_url.url must be a non-empty string",
                    param=f"{block_param}.image_url.url",
                )
            images.append(_image_from_url(image_url, block_param))
            continue
        raise ProtocolCompatibilityError(
            "unsupported_content_block",
            f"Unsupported OpenAI user content block: {block_type}",
            param=block_param,
        )
    return "".join(text_parts), images


def _image_from_url(value: str, param: str) -> ImageInput:
    if value.startswith("data:"):
        try:
            header, data = value.split(",", 1)
            media_type, encoding = header.removeprefix("data:").split(";", 1)
        except ValueError as exc:
            raise ProtocolCompatibilityError(
                "invalid_image_url", "Invalid image data URL", param=param
            ) from exc
        if encoding != "base64":
            raise ProtocolCompatibilityError(
                "invalid_image_url", "Image data URL must use base64", param=param
            )
        return ImageInput.model_validate({"media_type": media_type, "data": data})
    return ImageInput.model_validate({"url": value})


def _openai_tool_calls(value: Any, param: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProtocolCompatibilityError(
            "invalid_tool_calls", "tool_calls must be an array", param=param
        )
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(value):
        item_param = f"{param}.{index}"
        if not isinstance(call, dict) or call.get("type", "function") != "function":
            raise ProtocolCompatibilityError(
                "unsupported_tool_call", "Only function tool calls are supported", param=item_param
            )
        function = call.get("function")
        if not isinstance(function, dict):
            raise ProtocolCompatibilityError(
                "invalid_tool_call", "Tool call function must be an object", param=item_param
            )
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ProtocolCompatibilityError(
                    "invalid_tool_arguments",
                    "Tool arguments must be valid JSON",
                    param=f"{item_param}.function.arguments",
                ) from exc
        if not isinstance(arguments, dict):
            raise ProtocolCompatibilityError(
                "invalid_tool_arguments",
                "Tool arguments must be a JSON object",
                param=f"{item_param}.function.arguments",
            )
        calls.append(
            {
                "id": _required_string(call, "id", f"{item_param}.id"),
                "name": _required_string(function, "name", f"{item_param}.function.name"),
                "arguments": arguments,
            }
        )
    return calls


def _openai_tools(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for index, tool in enumerate(value):
        param = f"tools.{index}"
        if tool.get("type", "function") != "function" or not isinstance(
            tool.get("function"), dict
        ):
            raise ProtocolCompatibilityError(
                "unsupported_tool", "Only function tools are supported", param=param
            )
        function = tool["function"]
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise ProtocolCompatibilityError(
                "invalid_tool_schema", "Tool parameters must be an object", param=param
            )
        tools.append(
            {
                "name": _required_string(function, "name", f"{param}.function.name"),
                "description": str(function.get("description") or ""),
                "input_schema": parameters,
            }
        )
    return tools


def _openai_tool_choice(value: str | dict[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        mapping = {"auto": "auto", "required": "any", "none": "none"}
        if value not in mapping:
            raise ProtocolCompatibilityError(
                "invalid_tool_choice", f"Unsupported tool_choice: {value}", param="tool_choice"
            )
        return {"type": mapping[value], "name": ""}
    function = value.get("function")
    if value.get("type") != "function" or not isinstance(function, dict):
        raise ProtocolCompatibilityError(
            "invalid_tool_choice", "Named tool_choice must select a function", param="tool_choice"
        )
    return {
        "type": "tool",
        "name": _required_string(function, "name", "tool_choice.function.name"),
    }


def _openai_thinking(value: str | None) -> dict[str, Any]:
    if value is None:
        return {"type": "disabled"}
    budgets = {
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "xhigh": 24576,
        "max": 32768,
    }
    return {"type": "enabled", "budget_tokens": budgets[value]}


def _anthropic_system_parts(value: str | list[dict[str, Any]] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    parts: list[str] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ProtocolCompatibilityError(
                "unsupported_system_block",
                "Anthropic system only supports text blocks",
                param=f"system.{index}",
            )
        parts.append(_required_string(block, "text", f"system.{index}.text"))
    return parts


def _anthropic_image(block: Mapping[str, Any], param: str) -> ImageInput:
    source = block.get("source")
    if not isinstance(source, dict):
        raise ProtocolCompatibilityError(
            "invalid_image_source", "Image source must be an object", param=f"{param}.source"
        )
    source_type = source.get("type")
    if source_type == "base64":
        return ImageInput.model_validate(
            {
                "media_type": _required_string(source, "media_type", f"{param}.source.media_type"),
                "data": _required_string(source, "data", f"{param}.source.data"),
            }
        )
    if source_type == "url":
        return ImageInput.model_validate(
            {"url": _required_string(source, "url", f"{param}.source.url")}
        )
    raise ProtocolCompatibilityError(
        "unsupported_image_source",
        f"Unsupported image source: {source_type}",
        param=f"{param}.source.type",
    )


def _anthropic_tool_result_text(value: Any, param: str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parts: list[str] = []
    for index, block in enumerate(value):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(_required_string(block, "text", f"{param}.content.{index}.text"))
        else:
            parts.append(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
    return "".join(parts)


def _anthropic_tools(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for index, tool in enumerate(value):
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise ProtocolCompatibilityError(
                "invalid_tool_schema",
                "Tool input_schema must be an object",
                param=f"tools.{index}.input_schema",
            )
        tools.append(
            {
                "name": _required_string(tool, "name", f"tools.{index}.name"),
                "description": str(tool.get("description") or ""),
                "input_schema": schema,
            }
        )
    return tools


def _anthropic_tool_choice(value: dict[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    choice_type = _required_string(value, "type", "tool_choice.type")
    if choice_type not in {"auto", "any", "none", "tool"}:
        raise ProtocolCompatibilityError(
            "invalid_tool_choice", f"Unsupported tool_choice: {choice_type}", param="tool_choice"
        )
    return {
        "type": choice_type,
        "name": (
            _required_string(value, "name", "tool_choice.name")
            if choice_type == "tool"
            else ""
        ),
    }


def _anthropic_thinking(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None or value.get("type") == "disabled":
        return {"type": "disabled"}
    if value.get("type") != "enabled":
        raise ProtocolCompatibilityError(
            "invalid_thinking", "thinking.type must be enabled or disabled", param="thinking.type"
        )
    budget = value.get("budget_tokens")
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise ProtocolCompatibilityError(
            "invalid_thinking", "thinking.budget_tokens must be an integer", param="thinking.budget_tokens"
        )
    return {"type": "enabled", "budget_tokens": budget}


def _required_string(value: Mapping[str, Any], key: str, param: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ProtocolCompatibilityError(
            "invalid_request", f"{param} must be a non-empty string", param=param
        )
    return item


def _validate_compat_json(value: Any) -> None:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must contain JSON-compatible data") from exc
    if len(serialized) > _MAX_COMPAT_JSON_CHARS:
        raise ValueError("value is too large")


def _agent_response(value: AgentChatResponse | Mapping[str, Any]) -> AgentChatResponse:
    return value if isinstance(value, AgentChatResponse) else AgentChatResponse.model_validate(value)


def _openai_assistant_message(response: AgentChatResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or None,
    }
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in response.tool_calls
        ]
    return message


def _openai_finish_reason(response: AgentChatResponse) -> str:
    if response.code == "PROVIDER_OUTPUT_TRUNCATED" or response.usage.get(
        "output_truncated"
    ):
        return "length"
    return "tool_calls" if response.tool_calls else "stop"


def _openai_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens") or prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _anthropic_response_blocks(response: AgentChatResponse) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if response.content:
        blocks.append({"type": "text", "text": response.content})
    blocks.extend(
        {
            "type": "tool_use",
            "id": call.id,
            "name": call.name,
            "input": call.arguments,
        }
        for call in response.tool_calls
    )
    return blocks


def _anthropic_stop_reason(response: AgentChatResponse) -> str:
    if response.code == "PROVIDER_OUTPUT_TRUNCATED" or response.usage.get(
        "output_truncated"
    ):
        return "max_tokens"
    return "tool_use" if response.tool_calls else "end_turn"


def _anthropic_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": _usage_int(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _usage_int(usage, "output_tokens", "completion_tokens"),
    }


def _usage_int(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _openai_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }


def _openai_sse(payload: Mapping[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _anthropic_sse(event: str, payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


async def _normalized_stream_events(
    events: AsyncIterable[dict[str, Any] | str | bytes],
) -> AsyncIterator[tuple[Literal["delta", "completed", "error"], dict[str, Any]]]:
    async for raw in events:
        for event in _decode_stream_item(raw):
            kind = event.get("kind")
            if kind == "delta":
                yield "delta", {"content": str(event.get("content") or "")}
                continue
            if kind == "completed":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    yield "completed", payload
                    continue
            event_type = event.get("type")
            payload = event.get("payload")
            if event_type == "model.delta" and isinstance(payload, dict):
                yield "delta", payload
            elif event_type == "model.completed" and isinstance(payload, dict):
                yield "completed", payload
            elif event_type in {"gateway.error", "error"}:
                yield "error", payload if isinstance(payload, dict) else event


def _decode_stream_item(raw: dict[str, Any] | str | bytes) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [raw]
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    events: list[dict[str, Any]] = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line.removeprefix("data:").strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProtocolCompatibilityError(
                "invalid_stream_event", "Gateway stream event is invalid JSON"
            ) from exc
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _error_fields(
    error: Mapping[str, Any] | Exception,
    status_code: int | None,
) -> tuple[str, str, str, int]:
    if isinstance(error, ProtocolCompatibilityError):
        return error.code, error.message, error.param, status_code or error.status_code
    if isinstance(error, ValidationError):
        first = error.errors(include_input=False, include_context=False)[0]
        location = ".".join(str(part) for part in first.get("loc") or ())
        return (
            "invalid_request",
            str(first.get("msg") or "Request validation failed"),
            location,
            status_code or 400,
        )
    detail = getattr(error, "detail", None)
    exception_status = getattr(error, "status_code", None)
    if isinstance(detail, Mapping):
        return _error_fields(
            detail,
            status_code=(
                status_code
                or (exception_status if isinstance(exception_status, int) else None)
            ),
        )
    if isinstance(error, Mapping):
        detail = error.get("detail")
        if isinstance(detail, dict):
            error = detail
        code = str(error.get("code") or "gateway_error")
        message = str(error.get("message") or "Gateway request failed")
        param = str(error.get("param") or error.get("field") or "")
        raw_status = error.get("status") or error.get("status_code")
        resolved_status = (
            status_code
            or (raw_status if isinstance(raw_status, int) else None)
            or 500
        )
        return code, message, param, resolved_status
    return (
        "gateway_error",
        str(error) or "Gateway request failed",
        "",
        status_code or 500,
    )


__all__ = [
    "AnthropicMessagesRequest",
    "OpenAIChatCompletionRequest",
    "OpenAIStreamOptions",
    "ProtocolCompatibilityError",
    "anthropic_error_response",
    "anthropic_message_response",
    "anthropic_stream_response",
    "anthropic_to_agent_request",
    "openai_chat_completion_response",
    "openai_error_response",
    "openai_models_response",
    "openai_stream_response",
    "openai_to_agent_request",
]
