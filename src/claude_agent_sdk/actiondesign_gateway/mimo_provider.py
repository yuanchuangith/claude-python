from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
from fastapi import HTTPException

from .backend_tools import (
    BackendToolResult,
    backend_tool_loop_limit_error,
    clean_backend_tool_protocol_text,
    execute_backend_tool_calls,
    extract_backend_tool_calls,
    format_backend_tool_results,
)
from .models import DESIGN_TOOLS
from .tool_protocol import (
    clean_tool_protocol_text,
    extract_tool_calls,
    normalize_image_data,
)

_IMAGE_MODELS = {"mimo-v2.5"}
_FALLBACK_IMAGE_MODEL = "mimo-v2.5"
_MIMO_BACKEND_TOOL_PROMPT = """Backend-only tools:
- When you need ActionDesign component parameters, events, usage, or examples,
  first call [BACKEND_TOOL_CALL] knowledge.search({"query":"..."}).
- If search snippets are not enough, call
  [BACKEND_TOOL_CALL] knowledge.read({"path":"...", "heading":"..."}).
- You may also use backend MCP or Skill tools with [BACKEND_TOOL_CALL] tool_name({...}).
- Available backend tool namespaces are knowledge.*, mcp.*, and skill.*.
- Backend tool calls are executed only by the backend and must never be shown to the frontend.
- Do not use [BACKEND_TOOL_CALL] for ActionDesign canvas operations.

ActionDesign frontend tools:
- Final answers should only use [TOOL_CALL] tool_name({...}) for ActionDesign frontend tools such as create_node, preview_code, and propose_plan.
- Frontend tool calls are executed by the frontend after the backend returns."""


async def call_mimo(req: Any, settings: Any) -> dict[str, Any]:
    """Call a MIMO-compatible Anthropic Messages endpoint."""
    started = time.time()
    model = _model(req, settings)
    _reject_unsupported_images(req, model)

    loop_result = await _run_mimo_agent_loop(req, settings, model)
    content = loop_result["content"]
    clean_backend_content = clean_backend_tool_protocol_text(content)
    parsed_tool_calls = _filter_allowed(
        extract_tool_calls(clean_backend_content),
        _allowed_tools(req),
    )
    clean_content = _clean_final_content(content)
    return _response(
        content=clean_content,
        tool_calls=parsed_tool_calls,
        success=True,
        duration_ms=_duration_ms(started),
        usage=loop_result["usage"],
        model=model,
        provider="mimo",
    )


async def stream_mimo(req: Any, settings: Any) -> AsyncIterator[str]:
    """Stream MiMo results while hiding backend-only tool protocol markers."""
    started = time.time()
    model = _model(req, settings)
    _reject_unsupported_images(req, model)

    try:
        loop_result = await _run_mimo_agent_loop(req, settings, model)
        full_text = loop_result["content"]
        clean_backend_content = clean_backend_tool_protocol_text(full_text)
        tool_calls = _filter_allowed(
            extract_tool_calls(clean_backend_content),
            _allowed_tools(req),
        )
        clean_content = _clean_final_content(full_text)
        if clean_content:
            yield _sse({"type": "text_delta", "content": clean_content})
        yield _sse(
            {
                "type": "message_complete",
                "content": clean_content,
                "tool_calls": tool_calls,
                "success": True,
                "duration_ms": _duration_ms(started),
                "usage": loop_result["usage"],
                "model": model,
                "provider": "mimo",
            }
        )
    except Exception as exc:
        yield _sse(
            {
                "type": "message_complete",
                "content": "",
                "tool_calls": [],
                "success": False,
                "error": str(exc),
                "duration_ms": _duration_ms(started),
                "usage": {},
                "model": model,
                "provider": "mimo",
            }
        )


async def _run_mimo_agent_loop(
    req: Any,
    settings: Any,
    model: str,
) -> dict[str, Any]:
    messages = _initial_messages(req)
    system_prompt = _system_prompt(req)
    max_turns = _max_backend_tool_turns(settings)
    max_calls_per_turn = _max_backend_tool_calls_per_turn(settings)
    usage: dict[str, Any] = {}

    for _ in range(max_turns):
        body = _messages_body(
            req,
            settings,
            model,
            stream=False,
            messages=messages,
            system_prompt=system_prompt,
        )
        payload = await _post_mimo(body, settings)
        if isinstance(payload.get("usage"), dict):
            usage = payload["usage"]

        content = _extract_response_text(payload)
        backend_calls = extract_backend_tool_calls(content)
        if not backend_calls:
            return {"content": content, "usage": usage}

        messages.append({"role": "assistant", "content": content})
        limited_calls = backend_calls[:max_calls_per_turn]
        results = await execute_backend_tool_calls(limited_calls, settings)
        if len(backend_calls) > max_calls_per_turn:
            results.append(
                BackendToolResult(
                    name="backend.call_limit",
                    status="failed",
                    error="Too many backend tool calls in one turn",
                    code="BACKEND_TOOL_CALL_LIMIT",
                )
            )
        messages.append(
            {
                "role": "user",
                "content": format_backend_tool_results(results),
            }
        )

    raise backend_tool_loop_limit_error()


async def _post_mimo(body: dict[str, Any], settings: Any) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
            response = await client.post(
                _base_url(settings),
                headers=_headers(settings),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MIMO_UPSTREAM_ERROR",
                "message": f"MiMo HTTP {exc.response.status_code}",
            },
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _headers(settings: Any) -> dict[str, str]:
    api_key = _setting(settings, "mimo_api_key", "api_key", default="")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MIMO_API_KEY_MISSING",
                "message": "Set GXP_MIMO_API_KEY on the backend",
            },
        )
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": _setting(
            settings,
            "mimo_anthropic_version",
            default="2023-06-01",
        ),
    }
    auth_mode = str(
        _setting(settings, "mimo_auth_mode", "mimo_auth_type", default="api-key")
    ).lower()
    if auth_mode in {"bearer", "authorization", "auth-bearer"}:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["api-key"] = api_key
    return headers


def _messages_body(
    req: Any,
    settings: Any,
    model: str,
    *,
    stream: bool,
    messages: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(
            _request_value(
                req,
                "maxTokens",
                "max_tokens",
                default=_setting(
                    settings,
                    "mimo_max_tokens",
                    "max_tokens",
                    default=8192,
                ),
            )
        ),
        "messages": messages or _initial_messages(req),
    }
    if stream:
        body["stream"] = True
    thinking = _request_value(req, "thinking", default=None)
    if thinking:
        body["thinking"] = thinking
    system_text = system_prompt if system_prompt is not None else _system_prompt(req)
    if system_text:
        body["system"] = system_text
    return body


def _initial_messages(req: Any) -> list[dict[str, Any]]:
    return [{"role": "user", "content": _message_content(req)}]


def _system_prompt(req: Any) -> str:
    system_prompt = str(
        _request_value(req, "systemPrompt", "system_prompt", default="")
        or ""
    )
    if system_prompt:
        return f"{system_prompt}\n\n{_MIMO_BACKEND_TOOL_PROMPT}"
    return _MIMO_BACKEND_TOOL_PROMPT


def _message_content(req: Any) -> str | list[dict[str, Any]]:
    prompt = str(_request_value(req, "prompt", default=""))
    images = list(_request_value(req, "images", default=[]) or [])
    if not images:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        media_type = str(
            _request_value(image, "media_type", "mediaType", default="image/png")
        )
        data = _request_value(image, "data", default="")
        url = _request_value(image, "url", default="")
        if data:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": normalize_image_data(str(data)),
                    },
                }
            )
        elif url:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "url", "url": str(url)},
                }
            )
    return content


def _reject_unsupported_images(req: Any, model: str) -> None:
    if _request_value(req, "images", default=[]) and model not in _IMAGE_MODELS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_DOES_NOT_SUPPORT_IMAGES",
                "fallbackModel": _FALLBACK_IMAGE_MODEL,
            },
        )


def _extract_response_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(payload.get("text") or payload.get("content") or "")


def _model(req: Any, settings: Any) -> str:
    return str(
        _request_value(
            req,
            "model",
            default=_setting(settings, "mimo_default_model", default="mimo-v2.5"),
        )
        or _setting(settings, "mimo_default_model", default="mimo-v2.5")
    )


def _base_url(settings: Any) -> str:
    return str(
        _setting(
            settings,
            "mimo_api_url",
            "mimo_base_url",
            "mimo_messages_url",
            default="https://api.xiaomimimo.com/anthropic/v1/messages",
        )
    )


def _timeout(settings: Any) -> float:
    return float(
        _setting(
            settings,
            "mimo_timeout_seconds",
            "mimo_timeout",
            "request_timeout",
            default=120.0,
        )
    )


def _max_backend_tool_turns(settings: Any) -> int:
    return int(_setting(settings, "mimo_max_backend_tool_turns", default=6))


def _max_backend_tool_calls_per_turn(settings: Any) -> int:
    return int(_setting(settings, "mimo_max_backend_tool_calls_per_turn", default=4))


def _allowed_tools(req: Any) -> set[str] | None:
    design_tools = set(DESIGN_TOOLS)
    tools = _request_value(req, "toolNames", "tool_names", default=None)
    if not tools:
        return design_tools
    return {str(tool) for tool in tools} & design_tools


def _filter_allowed(
    tool_calls: Iterable[dict[str, Any]],
    allowed_tools: set[str] | None,
) -> list[dict[str, Any]]:
    if allowed_tools is None:
        return list(tool_calls)
    return [call for call in tool_calls if call.get("name") in allowed_tools]


def _request_value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _setting(obj: Any, *names: str, default: Any = None) -> Any:
    return _request_value(obj, *names, default=default)


def _response(**payload: Any) -> dict[str, Any]:
    return payload


def _clean_final_content(content: str) -> str:
    return clean_tool_protocol_text(
        clean_backend_tool_protocol_text(content)
    ).strip()


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(_response(**payload), ensure_ascii=False)}\n\n"


def _duration_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
