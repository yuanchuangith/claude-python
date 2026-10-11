"""Responses wire adapter. All business tools still execute in the browser Graph."""

from __future__ import annotations
from .request_budget import prepare_provider_body

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .models import AgentChatResponse
from .profile_store import ProviderProfile
from .provider_http import (
    _normalize_fixed_provider_url,
    _openai_reasoning_effort,
    _provider_http_client,
    _provider_http_error,
    _raise_provider_status,
    _upstream_error,
    openai_request_headers,
    validate_response_against_request,
)
from .reasoning import compatible_reasoning, reasoning_payload


def normalize_responses_url(value: str) -> str:
    return _normalize_fixed_provider_url(
        value,
        fixed_suffix="/v1/responses",
        resource_suffix="/responses",
        provider_label="OpenAI Responses",
    )


def responses_request_body(
    req: Any, profile: ProviderProfile, stream: bool
) -> dict[str, Any]:
    history = list(req.messages or [])
    inputs: list[dict[str, Any]] = []
    last_user_index = max(
        (i for i, message in enumerate(history) if message.role == "user"), default=-1
    )
    for index, message in enumerate(history):
        if message.role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant":
            state = compatible_reasoning(message, req, profile)
            if state is not None and state.protocol == "openai-responses":
                inputs.extend(
                    item.model_dump(exclude_none=True)
                    for item in state.items
                    if item.encrypted_content
                )
            if message.content:
                inputs.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": message.content}],
                    }
                )
            inputs.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                }
                for call in message.tool_calls
            )
            continue
        content = [{"type": "input_text", "text": message.content}]
        if index == last_user_index:
            content.extend(_images(req))
        inputs.append({"role": "user", "content": content})
    if not history:
        inputs.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": req.prompt}, *_images(req)],
            }
        )
    body: dict[str, Any] = {
        "model": req.model or profile.default_model,
        "input": inputs,
        "max_output_tokens": req.max_tokens,
        "stream": stream,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if req.system_prompt:
        body["instructions"] = req.system_prompt
    if req.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in req.tools
        ]
    if req.tool_choice:
        choice = req.tool_choice
        body["tool_choice"] = (
            {"type": "function", "name": choice.name}
            if choice.type == "tool"
            else "required"
            if choice.type == "any"
            else choice.type
        )
    if req.thinking.type == "enabled":
        body["reasoning"] = {
            "effort": _openai_reasoning_effort(int(req.thinking.budget_tokens or 0)),
            "summary": "auto",
        }
    return body


def _images(req: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "input_image",
            "image_url": f"data:{image.media_type};base64,{image.data}"
            if image.data
            else image.url,
        }
        for image in req.images
    ]


def parse_responses_response(
    payload: dict[str, Any], profile: ProviderProfile, req: Any, started: float
) -> dict[str, Any]:
    outputs = payload.get("output")
    if not isinstance(outputs, list):
        raise _upstream_error(
            502, "PROVIDER_RESPONSE_INVALID", "Responses output must be an array"
        )
    texts, calls, reasoning = [], [], []
    for item in outputs:
        if not isinstance(item, dict):
            raise _upstream_error(
                502,
                "PROVIDER_RESPONSE_INVALID",
                "Responses output item must be an object",
            )
        kind = item.get("type")
        if kind == "message":
            contents = item.get("content")
            if not isinstance(contents, list) or any(
                not isinstance(content, dict) for content in contents
            ):
                raise _upstream_error(
                    502,
                    "PROVIDER_RESPONSE_INVALID",
                    "Responses message content must be an array of objects",
                )
            for content in contents:
                if content.get("type") == "output_text" and isinstance(
                    content.get("text"), str
                ):
                    texts.append(content["text"])
        elif kind == "function_call":
            try:
                arguments = json.loads(item.get("arguments", "{}"))
            except (ValueError, TypeError) as exc:
                raise _upstream_error(
                    502,
                    "PROVIDER_TOOL_CALL_INVALID",
                    "Responses tool arguments are invalid JSON",
                ) from exc
            if not isinstance(arguments, dict):
                raise _upstream_error(
                    502,
                    "PROVIDER_TOOL_CALL_INVALID",
                    "Responses tool arguments must be an object",
                )
            calls.append(
                {
                    "id": item.get("call_id"),
                    "name": item.get("name"),
                    "arguments": arguments,
                }
            )
        elif kind == "reasoning":
            reasoning.append(
                {
                    "type": "reasoning",
                    "summary": item.get("summary") or [],
                    **({"id": item["id"]} if item.get("id") else {}),
                    **(
                        {"encrypted_content": item["encrypted_content"]}
                        if item.get("encrypted_content")
                        else {}
                    ),
                }
            )
        else:
            raise _upstream_error(
                502,
                "PROVIDER_RESPONSE_INVALID",
                f"Unsupported Responses output item: {kind}",
            )
    status = payload.get("status")
    if status not in {
        "completed",
        "incomplete",
        "failed",
        "cancelled",
        "in_progress",
        "queued",
    }:
        raise _upstream_error(
            502, "PROVIDER_RESPONSE_INVALID", "Responses status is missing or invalid"
        )
    success = status == "completed"
    if success and not "".join(texts).strip() and not calls:
        raise _upstream_error(
            502,
            "PROVIDER_RESPONSE_EMPTY",
            "Responses output contains neither text nor native tool calls",
        )
    model = str(payload.get("model") or req.model or profile.default_model)
    normalized = {
        "provider": profile.provider,
        "model": model,
        "content": "".join(texts),
        "tool_calls": calls,
        "success": success,
        "error": None if success else "Responses generation did not complete",
        "code": None
        if success
        else "PROVIDER_OUTPUT_TRUNCATED"
        if status == "incomplete"
        else "PROVIDER_RESPONSE_ERROR",
        "duration_ms": int((time.time() - started) * 1000),
        "usage": payload.get("usage") or {},
        **(
            {
                "reasoning": reasoning_payload(
                    profile.provider,
                    profile.id,
                    req.model or profile.default_model,
                    "openai-responses",
                    items=reasoning,
                )
            }
            if reasoning and success
            else {}
        ),
    }
    parsed = AgentChatResponse.model_validate(normalized)
    validate_response_against_request(req, parsed)
    return parsed.model_dump(mode="json")


async def call_responses(req: Any, profile: ProviderProfile) -> dict[str, Any]:
    started = time.time()
    try:
        async with _provider_http_client(profile) as client:
            response = await client.post(
                normalize_responses_url(profile.base_url),
                headers=openai_request_headers(profile),
                json=prepare_provider_body(req, profile, False)[0],
            )
            _raise_provider_status(response)
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.TimeoutException as exc:
        raise _upstream_error(
            504,
            "PROVIDER_TIMEOUT",
            "Responses request timed out",
            retryable=True,
            failover_scope="model",
        ) from exc
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
            502, "PROVIDER_RESPONSE_INVALID", "Responses response is not JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise _upstream_error(
            502, "PROVIDER_RESPONSE_INVALID", "Responses response must be an object"
        )
    return parse_responses_response(payload, profile, req, started)


async def stream_responses(
    req: Any, profile: ProviderProfile
) -> AsyncIterator[dict[str, Any]]:
    started = time.time()
    completed: dict[str, Any] | None = None
    data: list[str] = []
    try:
        async with (
            _provider_http_client(profile) as client,
            client.stream(
                "POST",
                normalize_responses_url(profile.base_url),
                headers=openai_request_headers(profile),
                json=prepare_provider_body(req, profile, True)[0],
            ) as response,
        ):
            if response.is_error:
                await response.aread()
            _raise_provider_status(response)
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].strip())
                if line or not data:
                    continue
                raw, data = "\n".join(data), []
                if raw == "[DONE]":
                    break
                event = _parse_event(raw)
                if event.get("type") == "response.output_text.delta":
                    if not isinstance(event.get("delta"), str):
                        raise _upstream_error(
                            502,
                            "PROVIDER_STREAM_INVALID",
                            "Responses text delta must be text",
                        )
                    yield {"kind": "delta", "content": event["delta"]}
                elif event.get("type") in {
                    "response.completed",
                    "response.incomplete",
                }:
                    completed = event.get("response")
                elif event.get("type") in {"error", "response.failed"}:
                    raise _upstream_error(
                        502, "PROVIDER_STREAM_ERROR", "Responses stream failed"
                    )
            if data:
                event = _parse_event("\n".join(data))
                if event.get("type") in {"error", "response.failed"}:
                    raise _upstream_error(
                        502, "PROVIDER_STREAM_ERROR", "Responses stream failed"
                    )
                if event.get("type") in {
                    "response.completed",
                    "response.incomplete",
                }:
                    completed = event.get("response")
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc) from exc
    except httpx.TimeoutException as exc:
        raise _upstream_error(
            504,
            "PROVIDER_TIMEOUT",
            "Responses stream timed out",
            retryable=True,
            failover_scope="model",
        ) from exc
    except httpx.RequestError as exc:
        raise _upstream_error(
            502,
            "PROVIDER_NETWORK_ERROR",
            str(exc),
            retryable=True,
            failover_scope="model",
        ) from exc
    if not isinstance(completed, dict):
        raise _upstream_error(
            502,
            "PROVIDER_STREAM_INVALID",
            "Responses stream ended without a completed response",
        )
    yield {
        "kind": "completed",
        "payload": parse_responses_response(completed, profile, req, started),
    }


def _parse_event(raw: str) -> dict[str, Any]:
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise _upstream_error(
            502, "PROVIDER_STREAM_INVALID", "Responses stream event is invalid JSON"
        ) from exc
    if not isinstance(event, dict):
        raise _upstream_error(
            502, "PROVIDER_STREAM_INVALID", "Responses stream event must be an object"
        )
    return event
