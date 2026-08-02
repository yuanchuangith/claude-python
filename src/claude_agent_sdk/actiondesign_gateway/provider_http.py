from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator

from .models import AgentChatResponse, DiscoveredProviderModel
from .profile_store import ProviderProfile

_CLAUDE_MESSAGES_SUFFIX = "/v1/messages"
_OPENAI_CHAT_COMPLETIONS_SUFFIX = "/v1/chat/completions"
_MODEL_DISCOVERY_RESPONSE_MAX_BYTES = 1_048_576
_MODEL_DISCOVERY_MAX_MODELS = 256


def normalize_messages_url(value: str) -> str:
    return _normalize_fixed_provider_url(
        value,
        fixed_suffix=_CLAUDE_MESSAGES_SUFFIX,
        resource_suffix="/messages",
        provider_label="Claude-compatible",
    )


def normalize_chat_completions_url(value: str) -> str:
    return _normalize_fixed_provider_url(
        value,
        fixed_suffix=_OPENAI_CHAT_COMPLETIONS_SUFFIX,
        resource_suffix="/chat/completions",
        provider_label="OpenAI-compatible",
    )


def normalize_models_url(value: str, protocol: str) -> str:
    if protocol == "openai":
        endpoint = normalize_chat_completions_url(value)
        resource_suffix = "/chat/completions"
    else:
        endpoint = normalize_messages_url(value)
        resource_suffix = "/messages"
    parsed = urlsplit(endpoint)
    path = parsed.path
    if not path.endswith(resource_suffix):
        raise _upstream_error(
            502,
            "PROVIDER_ENDPOINT_INVALID",
            "Provider endpoint cannot be normalized for model discovery",
            retryable=False,
        )
    return urlunsplit(parsed._replace(path=f"{path[:-len(resource_suffix)]}/models"))


def _normalize_fixed_provider_url(
    value: str,
    *,
    fixed_suffix: str,
    resource_suffix: str,
    provider_label: str,
) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith(fixed_suffix):
        return urlunsplit(parsed._replace(path=path))
    if path.endswith(resource_suffix):
        raise _upstream_error(
            502,
            "PROVIDER_ENDPOINT_INVALID",
            f"{provider_label} endpoint must use the fixed {fixed_suffix} suffix",
            retryable=False,
        )
    if path.endswith("/v1"):
        endpoint_path = f"{path}{resource_suffix}"
    else:
        endpoint_path = f"{path}{fixed_suffix}" if path else fixed_suffix
    return urlunsplit(parsed._replace(path=endpoint_path))


def _provider_http_client(profile: ProviderProfile) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=profile.timeout_seconds,
        follow_redirects=False,
    )


def _raise_provider_status(response: httpx.Response) -> None:
    if bool(getattr(response, "is_redirect", False)):
        raise _upstream_error(
            502,
            "PROVIDER_REDIRECT_FORBIDDEN",
            "Provider redirects are not allowed",
            retryable=False,
        )
    response.raise_for_status()


def request_headers(profile: ProviderProfile) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if profile.auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {profile.api_key}"
    else:
        headers["x-api-key"] = profile.api_key
        headers["api-key"] = profile.api_key
    return headers


def openai_request_headers(profile: ProviderProfile) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if profile.auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {profile.api_key}"
    else:
        headers["x-api-key"] = profile.api_key
        headers["api-key"] = profile.api_key
    return headers


def request_body(req: Any, profile: ProviderProfile, stream: bool) -> dict[str, Any]:
    history = list(getattr(req, "messages", None) or [])
    if history:
        messages = _anthropic_history_messages(req, history)
        body: dict[str, Any] = {
            "model": req.model or profile.default_model,
            "max_tokens": req.max_tokens,
            "stream": stream,
            "messages": messages,
        }
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if system_prompt:
            body["system"] = system_prompt
        _apply_anthropic_tools(body, req)
        return body

    content: str | list[dict[str, Any]] = req.prompt
    if req.images:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": req.prompt}]
        for image in req.images:
            if image.data:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data}})
            elif image.url:
                blocks.append({"type": "image", "source": {"type": "url", "url": image.url}})
        content = blocks
    body: dict[str, Any] = {
        "model": req.model or profile.default_model,
        "max_tokens": req.max_tokens,
        "stream": stream,
        "messages": [{"role": "user", "content": content}],
    }
    system_prompt = str(getattr(req, "system_prompt", "") or "")
    if system_prompt:
        body["system"] = system_prompt
    _apply_anthropic_tools(body, req)
    return body


def openai_request_body(req: Any, profile: ProviderProfile, stream: bool) -> dict[str, Any]:
    history = list(getattr(req, "messages", None) or [])
    if history:
        messages: list[dict[str, Any]] = []
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(_openai_history_messages(req, history))
        body: dict[str, Any] = {
            "model": req.model or profile.default_model,
            "max_tokens": req.max_tokens,
            "stream": stream,
            "messages": messages,
        }
        _apply_openai_tools(body, req)
        return body

    user_content: str | list[dict[str, Any]] = req.prompt
    if req.images:
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": req.prompt}
        ]
        for image in req.images:
            image_url = image.url
            if image.data:
                image_url = f"data:{image.media_type};base64,{image.data}"
            content_blocks.append(
                {"type": "image_url", "image_url": {"url": image_url}}
            )
        user_content = content_blocks

    messages: list[dict[str, Any]] = []
    system_prompt = str(getattr(req, "system_prompt", "") or "")
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    body: dict[str, Any] = {
        "model": req.model or profile.default_model,
        "max_tokens": req.max_tokens,
        "stream": stream,
        "messages": messages,
    }
    _apply_openai_tools(body, req)
    return body


def _apply_anthropic_tools(body: dict[str, Any], req: Any) -> None:
    if req.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in req.tools
        ]
    tool_choice = getattr(req, "tool_choice", None)
    if tool_choice is not None:
        body["tool_choice"] = {
            "type": tool_choice.type,
            **({"name": tool_choice.name} if tool_choice.type == "tool" else {}),
        }
    thinking = getattr(req, "thinking", None)
    if thinking is not None and thinking.type == "enabled":
        body["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking.budget_tokens,
        }


def _apply_openai_tools(body: dict[str, Any], req: Any) -> None:
    if req.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in req.tools
        ]
    choice = getattr(req, "tool_choice", None)
    if choice is not None:
        if choice.type == "any":
            body["tool_choice"] = "required"
        elif choice.type == "tool":
            body["tool_choice"] = {
                "type": "function",
                "function": {"name": choice.name},
            }
        else:
            body["tool_choice"] = choice.type
    thinking = getattr(req, "thinking", None)
    if thinking is not None and thinking.type == "enabled":
        body["reasoning_effort"] = _openai_reasoning_effort(
            int(thinking.budget_tokens or 0)
        )


def _anthropic_history_messages(req: Any, history: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    last_user_index = max(
        (index for index, message in enumerate(history) if message.role == "user"),
        default=-1,
    )
    for index, message in enumerate(history):
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id or f"call_{index}_{call_index}",
                    "name": call.name,
                    "input": call.arguments,
                }
                for call_index, call in enumerate(message.tool_calls)
            )
            _append_anthropic_message(messages, "assistant", blocks)
            continue
        if message.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            _append_anthropic_message(messages, "user", [block])
            continue
        blocks = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        if index == last_user_index:
            blocks.extend(_anthropic_image_blocks(req))
        _append_anthropic_message(messages, "user", blocks)
    return messages


def _append_anthropic_message(
    messages: list[dict[str, Any]],
    role: str,
    blocks: list[dict[str, Any]],
) -> None:
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return
    messages.append({"role": role, "content": blocks})


def _anthropic_image_blocks(req: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for image in req.images:
        if image.data:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.data,
                    },
                }
            )
        elif image.url:
            blocks.append(
                {"type": "image", "source": {"type": "url", "url": image.url}}
            )
    return blocks


def _openai_history_messages(req: Any, history: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    last_user_index = max(
        (index for index, message in enumerate(history) if message.role == "user"),
        default=-1,
    )
    for index, message in enumerate(history):
        if message.role == "assistant":
            item: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or None,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id or f"call_{index}_{call_index}",
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
                    for call_index, call in enumerate(message.tool_calls)
                ]
            messages.append(item)
            continue
        if message.role == "tool":
            item = {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
            if message.name:
                item["name"] = message.name
            messages.append(item)
            continue
        content: str | list[dict[str, Any]] = message.content
        if index == last_user_index and req.images:
            content_blocks: list[dict[str, Any]] = []
            if message.content:
                content_blocks.append({"type": "text", "text": message.content})
            for image in req.images:
                image_url = image.url
                if image.data:
                    image_url = f"data:{image.media_type};base64,{image.data}"
                content_blocks.append(
                    {"type": "image_url", "image_url": {"url": image_url}}
                )
            content = content_blocks
        item = {"role": "user", "content": content}
        if message.name:
            item["name"] = message.name
        messages.append(item)
    return messages


def _openai_reasoning_effort(budget_tokens: int) -> str:
    if budget_tokens <= 4_096:
        return "low"
    if budget_tokens <= 8_192:
        return "medium"
    if budget_tokens <= 16_384:
        return "high"
    if budget_tokens <= 24_576:
        return "xhigh"
    return "max"


def parse_response(payload: dict[str, Any], provider: str, model: str, started: float) -> dict[str, Any]:
    content: list[str] = []
    calls: list[dict[str, Any]] = []
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", "Provider content must be an array")
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", f"Provider content[{index}] must be an object")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", f"Provider text block {index} is invalid")
            content.append(text)
        elif block.get("type") == "tool_use":
            arguments = block.get("input") or {}
            if not isinstance(arguments, dict):
                raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Provider tool_use input at index {index} must be an object")
            tool_name = str(block.get("name") or "").strip()
            if not tool_name:
                raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Provider tool_use name at index {index} is empty")
            calls.append({"id": str(block.get("id") or ""), "name": tool_name, "arguments": arguments})
    stop_reason = str(payload.get("stop_reason") or "")
    usage = dict(payload.get("usage") or {})
    if stop_reason:
        usage["stop_reason"] = stop_reason
    truncated = stop_reason in {"max_tokens", "length", "output_truncated"}
    if not "".join(content).strip() and not calls and not truncated:
        raise _upstream_error(502, "PROVIDER_RESPONSE_EMPTY", "Provider response contains neither text nor native tool calls")
    normalized = {
        "provider": provider,
        "model": str(payload.get("model") or model),
        "content": "".join(content),
        "tool_calls": calls,
        "success": not truncated,
        "error": "Provider output was truncated by max_tokens" if truncated else None,
        "code": "PROVIDER_OUTPUT_TRUNCATED" if truncated else None,
        "duration_ms": int((time.time() - started) * 1000),
        "usage": {**usage, **({"output_truncated": True} if truncated else {})},
    }
    validated = AgentChatResponse.model_validate(normalized)
    validate_response_against_request(req=None, response=validated)
    return validated.model_dump(mode="json")


async def call_messages(req: Any, profile: ProviderProfile) -> dict[str, Any]:
    started = time.time()
    try:
        async with _provider_http_client(profile) as client:
            response = await client.post(
                normalize_messages_url(profile.base_url),
                headers=request_headers(profile),
                json=request_body(req, profile, False),
            )
            _raise_provider_status(response)
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise _upstream_error(
            504,
            "PROVIDER_TIMEOUT",
            "Provider request timed out",
            retryable=True,
            failover_scope="model",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(
            502,
            "PROVIDER_NETWORK_ERROR",
            str(exc),
            retryable=True,
            failover_scope="model",
        ) from exc
    except ValueError as exc:
        raise _upstream_error(
            502,
            "PROVIDER_RESPONSE_ERROR",
            str(exc),
            retryable=True,
            failover_scope="model",
        ) from exc
    if not isinstance(payload, dict):
        raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", "Provider response is not an object")
    parsed = AgentChatResponse.model_validate(
        parse_response(payload, profile.provider, req.model or profile.default_model, started)
    )
    validate_response_against_request(req, parsed)
    return parsed.model_dump(mode="json")


async def stream_messages(req: Any, profile: ProviderProfile) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    text_parts: list[str] = []
    tool_blocks: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    model = req.model or profile.default_model
    try:
        async with _provider_http_client(profile) as client, client.stream(
            "POST",
            normalize_messages_url(profile.base_url),
            headers=request_headers(profile),
            json=request_body(req, profile, True),
        ) as response:
            if response.is_error:
                # Streaming responses are not buffered by httpx. Read only
                # the error body before mapping the status so the shared
                # error classifier can inspect it without ResponseNotRead.
                await response.aread()
            _raise_provider_status(response)
            event_name = ""
            data_lines: list[str] = []
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line:
                    if data_lines:
                        event = _parse_event(event_name, data_lines)
                        event_name, data_lines = "", []
                        if event is None:
                            continue
                        model, delta_text = _consume_stream_event(
                            event,
                            model,
                            text_parts,
                            tool_blocks,
                            usage,
                        )
                        if delta_text:
                            yield {"kind": "delta", "content": delta_text}
                    continue
                if line.startswith("event:"):
                    event_name = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").strip())
            if data_lines:
                event = _parse_event(event_name, data_lines)
                if event is not None:
                    model, delta_text = _consume_stream_event(
                        event,
                        model,
                        text_parts,
                        tool_blocks,
                        usage,
                    )
                    if delta_text:
                        yield {"kind": "delta", "content": delta_text}
    except httpx.TimeoutException as exc:
        raise _upstream_error(504, "PROVIDER_TIMEOUT", "Provider stream timed out", retryable=True, failover_scope="model") from exc
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(502, "PROVIDER_NETWORK_ERROR", str(exc), retryable=True, failover_scope="model") from exc

    calls = []
    for block in tool_blocks.values():
        arguments = block.get("input") or {}
        if block["json"]:
            try:
                arguments = json.loads(block["json"])
            except json.JSONDecodeError as exc:
                raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Tool call {block['name']} arguments are invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Tool call {block['name']} arguments must be an object")
        calls.append({"id": block["id"], "name": block["name"], "arguments": arguments})
    stop_reason = str(usage.get("stop_reason") or "")
    truncated = stop_reason in {"max_tokens", "length", "output_truncated"}
    if not "".join(text_parts).strip() and not calls and not truncated:
        raise _upstream_error(502, "PROVIDER_RESPONSE_EMPTY", "Provider stream contains neither text nor native tool calls")
    normalized = {
        "provider": profile.provider,
        "model": model,
        "content": "".join(text_parts),
        "tool_calls": calls,
        "success": not truncated,
        "error": "Provider output was truncated by max_tokens" if truncated else None,
        "code": "PROVIDER_OUTPUT_TRUNCATED" if truncated else None,
        "duration_ms": int((time.time() - started) * 1000),
        "usage": {**usage, **({"output_truncated": True} if truncated else {})},
    }
    validated = AgentChatResponse.model_validate(normalized)
    validate_response_against_request(req, validated)
    yield {
        "kind": "completed",
        "payload": validated.model_dump(mode="json"),
    }


def parse_openai_response(
    payload: dict[str, Any],
    provider: str,
    model: str,
    started: float,
) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _upstream_error(
            502,
            "PROVIDER_RESPONSE_INVALID",
            "OpenAI-compatible response choices must be a non-empty array",
        )
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _upstream_error(
            502,
            "PROVIDER_RESPONSE_INVALID",
            "OpenAI-compatible response message must be an object",
        )
    content = _openai_content_text(message.get("content"))
    calls = _parse_openai_tool_calls(message.get("tool_calls") or [])
    finish_reason = str(choice.get("finish_reason") or "")
    truncated = finish_reason in {"length", "max_tokens", "output_truncated"}
    if not content.strip() and not calls and not truncated:
        raise _upstream_error(
            502,
            "PROVIDER_RESPONSE_EMPTY",
            "OpenAI-compatible response contains neither text nor native tool calls",
        )
    usage = dict(payload.get("usage") or {})
    if finish_reason:
        usage["finish_reason"] = finish_reason
    normalized = {
        "provider": provider,
        "model": str(payload.get("model") or model),
        "content": content,
        "tool_calls": calls,
        "success": not truncated,
        "error": "Provider output was truncated by max_tokens" if truncated else None,
        "code": "PROVIDER_OUTPUT_TRUNCATED" if truncated else None,
        "duration_ms": int((time.time() - started) * 1000),
        "usage": {**usage, **({"output_truncated": True} if truncated else {})},
    }
    return AgentChatResponse.model_validate(normalized).model_dump(mode="json")


async def call_openai(req: Any, profile: ProviderProfile) -> dict[str, Any]:
    started = time.time()
    try:
        async with _provider_http_client(profile) as client:
            response = await client.post(
                normalize_chat_completions_url(profile.base_url),
                headers=openai_request_headers(profile),
                json=openai_request_body(req, profile, False),
            )
            _raise_provider_status(response)
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise _upstream_error(504, "PROVIDER_TIMEOUT", "Provider request timed out", retryable=True, failover_scope="model") from exc
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(502, "PROVIDER_NETWORK_ERROR", str(exc), retryable=True, failover_scope="model") from exc
    except ValueError as exc:
        raise _upstream_error(502, "PROVIDER_RESPONSE_ERROR", str(exc), retryable=True, failover_scope="model") from exc
    if not isinstance(payload, dict):
        raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", "Provider response is not an object")
    parsed = AgentChatResponse.model_validate(
        parse_openai_response(
            payload,
            profile.provider,
            req.model or profile.default_model,
            started,
        )
    )
    validate_response_against_request(req, parsed)
    return parsed.model_dump(mode="json")


async def stream_openai(req: Any, profile: ProviderProfile) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    usage: dict[str, Any] = {}
    finish_reason = ""
    model = req.model or profile.default_model
    try:
        async with _provider_http_client(profile) as client:
            async with client.stream(
                "POST",
                normalize_chat_completions_url(profile.base_url),
                headers=openai_request_headers(profile),
                json=openai_request_body(req, profile, True),
            ) as response:
                if response.is_error:
                    await response.aread()
                _raise_provider_status(response)
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith("event:") or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise _upstream_error(
                            502,
                            "PROVIDER_STREAM_INVALID",
                            "OpenAI-compatible stream event is invalid JSON",
                        ) from exc
                    if not isinstance(event, dict):
                        raise _upstream_error(502, "PROVIDER_STREAM_INVALID", "Provider stream event is not an object")
                    if event.get("error"):
                        error = event.get("error") or {}
                        message = error.get("message") if isinstance(error, dict) else error
                        raise _upstream_error(502, "PROVIDER_STREAM_ERROR", str(message or "Provider stream error"))
                    model = str(event.get("model") or model)
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"])
                    choices = event.get("choices") or []
                    if not isinstance(choices, list):
                        raise _upstream_error(502, "PROVIDER_STREAM_INVALID", "Provider stream choices must be an array")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        delta_text = _openai_content_text(delta.get("content"))
                        if delta_text:
                            text_parts.append(delta_text)
                            yield {"kind": "delta", "content": delta_text}
                        _consume_openai_tool_call_deltas(delta.get("tool_calls") or [], tool_calls)
    except httpx.TimeoutException as exc:
        raise _upstream_error(504, "PROVIDER_TIMEOUT", "Provider stream timed out", retryable=True, failover_scope="model") from exc
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(502, "PROVIDER_NETWORK_ERROR", str(exc), retryable=True, failover_scope="model") from exc

    calls = _finalize_openai_tool_calls(tool_calls)
    truncated = finish_reason in {"length", "max_tokens", "output_truncated"}
    if not "".join(text_parts).strip() and not calls and not truncated:
        raise _upstream_error(502, "PROVIDER_RESPONSE_EMPTY", "Provider stream contains neither text nor native tool calls")
    if finish_reason:
        usage["finish_reason"] = finish_reason
    validated = AgentChatResponse.model_validate(
        {
            "provider": profile.provider,
            "model": model,
            "content": "".join(text_parts),
            "tool_calls": calls,
            "success": not truncated,
            "error": "Provider output was truncated by max_tokens" if truncated else None,
            "code": "PROVIDER_OUTPUT_TRUNCATED" if truncated else None,
            "duration_ms": int((time.time() - started) * 1000),
            "usage": {**usage, **({"output_truncated": True} if truncated else {})},
        }
    )
    validate_response_against_request(req, validated)
    yield {"kind": "completed", "payload": validated.model_dump(mode="json")}


async def call_provider(req: Any, profile: ProviderProfile) -> dict[str, Any]:
    if profile.protocol == "openai":
        return await call_openai(req, profile)
    return await call_messages(req, profile)


async def probe_provider(profile: ProviderProfile, model: str) -> None:
    if model not in {item.id for item in profile.models}:
        raise _upstream_error(
            400,
            "PROFILE_MODEL_UNAVAILABLE",
            "The requested model is not configured for this Profile",
            retryable=False,
        )
    if profile.protocol == "openai":
        url = normalize_chat_completions_url(profile.base_url)
        headers = openai_request_headers(profile)
        body = {
            "model": model,
            "max_tokens": 1,
            "stream": False,
            "messages": [{"role": "user", "content": "ping"}],
        }
    else:
        url = normalize_messages_url(profile.base_url)
        headers = request_headers(profile)
        body = {
            "model": model,
            "max_tokens": 1,
            "stream": False,
            "messages": [{"role": "user", "content": "ping"}],
        }
    try:
        async with _provider_http_client(profile) as client:
            response = await client.post(url, headers=headers, json=body)
            _raise_provider_status(response)
    except httpx.TimeoutException as exc:
        raise _upstream_error(
            504,
            "PROVIDER_TIMEOUT",
            "Provider connection test timed out",
            retryable=True,
            failover_scope="profile",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(
            502,
            "PROVIDER_NETWORK_ERROR",
            "Provider connection test failed",
            retryable=True,
            failover_scope="profile",
        ) from exc


async def discover_provider_models(profile: ProviderProfile) -> dict[str, Any]:
    url = normalize_models_url(profile.base_url, profile.protocol)
    headers = (
        openai_request_headers(profile)
        if profile.protocol == "openai"
        else request_headers(profile)
    )
    try:
        async with _provider_http_client(profile) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if bool(getattr(response, "is_redirect", False)):
                    raise _upstream_error(
                        502,
                        "PROVIDER_REDIRECT_FORBIDDEN",
                        "Provider redirects are not allowed",
                        retryable=False,
                    )
                if response.status_code in {404, 405}:
                    raise _upstream_error(
                        502,
                        "PROVIDER_MODEL_DISCOVERY_UNSUPPORTED",
                        "Provider does not expose the fixed /v1/models endpoint",
                        retryable=False,
                    )
                if response.status_code >= 400:
                    synthetic = httpx.Response(
                        response.status_code,
                        request=response.request,
                        content=b"",
                    )
                    try:
                        synthetic.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise _provider_http_error(exc) from exc
                content_length = response.headers.get("content-length", "").strip()
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = 0
                    if declared_length > _MODEL_DISCOVERY_RESPONSE_MAX_BYTES:
                        raise _upstream_error(
                            502,
                            "PROVIDER_MODELS_RESPONSE_TOO_LARGE",
                            "Provider model discovery response is too large",
                            retryable=False,
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MODEL_DISCOVERY_RESPONSE_MAX_BYTES:
                        raise _upstream_error(
                            502,
                            "PROVIDER_MODELS_RESPONSE_TOO_LARGE",
                            "Provider model discovery response is too large",
                            retryable=False,
                        )
    except httpx.TimeoutException as exc:
        raise _upstream_error(
            504,
            "PROVIDER_TIMEOUT",
            "Provider model discovery timed out",
            retryable=False,
        ) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(
            502,
            "PROVIDER_NETWORK_ERROR",
            "Provider model discovery failed",
            retryable=False,
        ) from exc
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _upstream_error(
            502,
            "PROVIDER_MODELS_RESPONSE_INVALID",
            "Provider model discovery response is not valid JSON",
            retryable=False,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise _upstream_error(
            502,
            "PROVIDER_MODELS_RESPONSE_INVALID",
            "Provider model discovery response must contain data[]",
            retryable=False,
        )
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    truncated = payload.get("has_more") is True
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise _upstream_error(
                502,
                "PROVIDER_MODELS_RESPONSE_INVALID",
                "Provider model discovery data[] contains an invalid model",
                retryable=False,
            )
        model_id = item["id"]
        raw_name = item.get("display_name", item.get("name", model_id))
        if not isinstance(raw_name, str):
            raise _upstream_error(
                502,
                "PROVIDER_MODELS_RESPONSE_INVALID",
                "Provider model discovery data[] contains an invalid model name",
                retryable=False,
            )
        try:
            model = DiscoveredProviderModel.model_validate(
                {"id": model_id, "name": raw_name or model_id}
            )
        except ValueError as exc:
            raise _upstream_error(
                502,
                "PROVIDER_MODELS_RESPONSE_INVALID",
                "Provider model discovery data[] contains an invalid model",
                retryable=False,
            ) from exc
        if model.id in seen:
            continue
        seen.add(model.id)
        if len(models) >= _MODEL_DISCOVERY_MAX_MODELS:
            truncated = True
            continue
        models.append(model.model_dump(mode="json"))
    return {"models": models, "truncated": truncated}


def stream_provider(req: Any, profile: ProviderProfile) -> AsyncIterator[dict[str, Any]]:
    if profile.protocol == "openai":
        return stream_openai(req, profile)
    return stream_messages(req, profile)


def _openai_content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    raise _upstream_error(502, "PROVIDER_RESPONSE_INVALID", "OpenAI-compatible message content is invalid")


def _parse_openai_tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", "OpenAI-compatible tool_calls must be an array")
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(value):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"OpenAI-compatible tool call {index} is invalid")
        function = call["function"]
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments") or "{}"
        if not name:
            raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"OpenAI-compatible tool call {index} has no name")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Tool call {name} arguments are invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", f"Tool call {name} arguments must be an object")
        calls.append({"id": str(call.get("id") or ""), "name": name, "arguments": arguments})
    return calls


def _consume_openai_tool_call_deltas(
    deltas: Any,
    tool_calls: dict[int, dict[str, str]],
) -> None:
    if not isinstance(deltas, list):
        raise _upstream_error(502, "PROVIDER_STREAM_INVALID", "OpenAI-compatible tool call delta must be an array")
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        index = int(delta.get("index") or 0)
        current = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        current["id"] += str(delta.get("id") or "")
        function = delta.get("function") or {}
        if isinstance(function, dict):
            current["name"] += str(function.get("name") or "")
            current["arguments"] += str(function.get("arguments") or "")


def _finalize_openai_tool_calls(tool_calls: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    raw = [
        {
            "id": value["id"],
            "function": {"name": value["name"], "arguments": value["arguments"] or "{}"},
        }
        for _, value in sorted(tool_calls.items())
    ]
    return _parse_openai_tool_calls(raw)


def validate_response_against_request(req: Any | None, response: AgentChatResponse) -> None:
    """Validate provider-native calls against the request-supplied tool schemas.

    This is generic transport validation only; it does not introduce a backend
    ActionDesign tool registry or execute any business tool.
    """
    if req is None:
        return
    definitions = {tool.name: tool for tool in req.tools}
    choice = getattr(req, "tool_choice", None)
    if choice is not None and choice.type == "none" and response.tool_calls:
        raise _upstream_error(502, "PROVIDER_TOOL_CALL_INVALID", "Provider returned tool calls when toolChoice=none")
    if choice is not None and choice.type == "tool":
        if len(response.tool_calls) != 1 or response.tool_calls[0].name != choice.name:
            raise _upstream_error(
                502,
                "PROVIDER_TOOL_CALL_INVALID",
                f"Provider must call exactly the required tool {choice.name}",
            )
    for index, call in enumerate(response.tool_calls):
        definition = definitions.get(call.name)
        if definition is None:
            raise _upstream_error(
                502,
                "PROVIDER_TOOL_CALL_INVALID",
                f"Provider tool call {index} references undeclared tool {call.name}",
            )
        errors = sorted(
            Draft202012Validator(definition.input_schema).iter_errors(call.arguments),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            first = errors[0]
            path = ".".join(str(part) for part in first.absolute_path) or "$"
            raise _upstream_error(
                502,
                "PROVIDER_TOOL_CALL_INVALID",
                f"Provider tool call {call.name} arguments failed schema validation at {path}: {first.message}",
            )


def _consume_stream_event(
    event: dict[str, Any],
    model: str,
    text_parts: list[str],
    tool_blocks: dict[int, dict[str, Any]],
    usage: dict[str, Any],
) -> tuple[str, str]:
    event_type = str(event.get("type") or "")
    if event_type == "message_start":
        message = event.get("message") or {}
        model = str(message.get("model") or model)
        usage.update(message.get("usage") or {})
    elif event_type == "content_block_start":
        index = int(event.get("index") or 0)
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            tool_blocks[index] = {
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "input": block.get("input") or {},
                "json": "",
            }
    elif event_type == "content_block_delta":
        index = int(event.get("index") or 0)
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = str(delta.get("text") or "")
            if text:
                text_parts.append(text)
                return model, text
        elif delta.get("type") == "input_json_delta" and index in tool_blocks:
            tool_blocks[index]["json"] += str(delta.get("partial_json") or "")
    elif event_type == "message_delta":
        delta = event.get("delta") or {}
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            usage["stop_reason"] = str(stop_reason)
        usage.update(event.get("usage") or {})
    return model, ""


def _parse_event(event_name: str, data_lines: list[str]) -> dict[str, Any] | None:
    raw = "\n".join(data_lines)
    if raw == "[DONE]":
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _upstream_error(502, "PROVIDER_STREAM_INVALID", "Provider stream event is invalid JSON") from exc
    if not isinstance(event, dict):
        raise _upstream_error(502, "PROVIDER_STREAM_INVALID", "Provider stream event is not an object")
    if event_name and "type" not in event:
        event["type"] = event_name
    if event.get("type") == "error":
        error = event.get("error") or {}
        raise _upstream_error(502, "PROVIDER_STREAM_ERROR", str(error.get("message") or error or "Provider stream error"))
    return event


def _upstream_error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
    failover_scope: str = "none",
) -> HTTPException:
    if retryable is None:
        retryable = code in {
            "PROVIDER_RESPONSE_EMPTY",
            "PROVIDER_RESPONSE_ERROR",
            "PROVIDER_RESPONSE_INVALID",
            "PROVIDER_STREAM_ERROR",
            "PROVIDER_STREAM_INVALID",
        }
        if retryable and failover_scope == "none":
            failover_scope = "model"
    return HTTPException(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "failoverScope": failover_scope,
        },
    )


def _provider_http_error(exc: httpx.HTTPStatusError) -> HTTPException:
    status = exc.response.status_code
    body = exc.response.text[:2000]
    normalized_body = body.lower()
    if status in {401, 403}:
        return _upstream_error(
            502,
            "PROVIDER_AUTH_FAILED",
            f"Provider authentication failed ({status})",
            retryable=True,
            failover_scope="profile",
        )
    if status == 429:
        return _upstream_error(
            503,
            "PROVIDER_RATE_LIMITED",
            "Provider rate limit exceeded",
            retryable=True,
            failover_scope="model",
        )
    if status in {400, 413} and any(
        marker in normalized_body
        for marker in ("context", "too long", "maximum context", "context_length")
    ):
        return _upstream_error(
            413,
            "PROVIDER_CONTEXT_TOO_LARGE",
            "Provider rejected the request because the context is too large",
            retryable=True,
            failover_scope="model",
        )
    if status in {400, 404, 422} and any(
        marker in normalized_body
        for marker in (
            "model not found",
            "model_not_found",
            "unknown model",
            "unsupported model",
            "model is not supported",
        )
    ):
        return _upstream_error(
            502,
            "PROVIDER_MODEL_UNAVAILABLE",
            "Provider does not support the requested model",
            retryable=True,
            failover_scope="model",
        )
    if status == 408 or status >= 500:
        return _upstream_error(
            502,
            "PROVIDER_HTTP_ERROR",
            f"Provider HTTP {status}",
            retryable=True,
            failover_scope="model",
        )
    return _upstream_error(502, "PROVIDER_HTTP_ERROR", f"Provider HTTP {status}")
