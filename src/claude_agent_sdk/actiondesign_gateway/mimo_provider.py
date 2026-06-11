from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx  # noqa: F401 - keep mimo_provider.httpx monkeypatch compatibility
from fastapi import HTTPException

from .backend_tools import (
    BackendToolResult,
    backend_tool_loop_limit_error,
    clean_backend_tool_protocol_text,
    execute_backend_tool_calls,
    extract_backend_tool_calls,
    format_backend_tool_results,
    frontend_tool_misuse_result,
)
from .mimo_http import post_mimo, stream_post_mimo
from .mimo_protocol import (
    clean_malformed_frontend_tool_text,
    has_malformed_backend_tool_call,
    has_malformed_frontend_tool_call,
    malformed_backend_tool_result,
    visible_text_chunks,
)
from .mimo_stream import (
    event_usage,
    extract_stop_reason,
    merge_numeric_usage,
    raise_for_stream_error_event,
    stream_text_delta,
)
from .models import DESIGN_TOOLS, MIMO_IMAGE_MODELS
from .review_protocol import REVIEW_SYSTEM_PROMPT, parse_code_review_response
from .session_log import append_conversation_event, model_to_dict
from .tool_protocol import (
    clean_tool_protocol_text,
    extract_tool_calls,
    normalize_image_data,
)

_MIMO_INCOMPLETE_CODE = "MIMO_RESPONSE_INCOMPLETE"
_MIMO_BACKEND_TOOL_PROMPT = """Backend-only tools:
Read-before-write rule:
- If you are not certain about an ActionDesign element's inputParams,
  outputParams, events, methods, examples, constraints, or required parameter
  shape, do not guess and do not write canvas nodes yet.
- First call [BACKEND_TOOL_CALL] knowledge.search({"query":"..."}).
- If search snippets are not enough, call
  [BACKEND_TOOL_CALL] knowledge.read({"path":"...", "heading":"..."}).
- A response that contains knowledge.search or knowledge.read must not also
  contain frontend write tool calls such as create_action, create_node,
  insert_node, or delete_node. Wait for backend results, then continue in the
  next turn.
- Use knowledge.search/read for general ActionDesign element rules, component
  conventions, method/event contracts, examples, and constraints.
- Use frontend read-only tools for live page state: inspect_action for current
  canvas nodes, get_page_actions for existing page actions, get_element_detail
  for registered element details, and get_component_methods for actual page
  component methods.
- You may also use backend MCP or Skill tools with [BACKEND_TOOL_CALL] tool_name({...}).
- Available backend tool namespaces are knowledge.*, mcp.*, and skill.*.
- Backend tool calls are executed only by the backend and must never be shown to the frontend.
- Do not use [BACKEND_TOOL_CALL] for ActionDesign canvas operations.
- get_element_detail, list_elements, get_page_actions, propose_plan, create_node, insert_node, delete_node, and preview_code are frontend tools and must never be used with [BACKEND_TOOL_CALL].

ActionDesign frontend tools:
- Final answers should only use [TOOL_CALL] tool_name({...}) for ActionDesign frontend tools such as create_node, preview_code, and propose_plan.
- Frontend tool calls are executed by the frontend after the backend returns.

Examples:
- If you need to create submit-time form validation but are unsure about
  NullCondition or BeforeSubmit parameters, first call
  [BACKEND_TOOL_CALL] knowledge.search({"query":"NullCondition BeforeSubmit inputParams validation examples"}).
- If you need to call a page component method and the concrete methods are
  unknown, use [TOOL_CALL] get_component_methods({"componentId":"C:<componentId>"})
  before creating ComponentMethod nodes.
- If you need general BeforeSubmit binding constraints or examples, first call
  [BACKEND_TOOL_CALL] knowledge.search({"query":"BeforeSubmit binding constraints examples"})."""


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
    if has_malformed_frontend_tool_call(clean_backend_content, parsed_tool_calls):
        clean_content = clean_malformed_frontend_tool_text(clean_backend_content)
        parsed_tool_calls = []
    else:
        clean_content = _clean_final_content(content)
    success = bool(loop_result.get("success", True))
    payload = _response(
        content=clean_content,
        tool_calls=parsed_tool_calls if success else [],
        success=success,
        error=loop_result.get("error"),
        duration_ms=_duration_ms(started),
        usage=loop_result["usage"],
        model=model,
        provider="mimo",
    )
    if loop_result.get("code"):
        payload["code"] = loop_result["code"]
    return payload


async def call_mimo_review(req: Any, settings: Any) -> dict[str, Any]:
    """Call MiMo for a JSON-only ActionDesign code review."""
    started = time.time()
    model = _model(req, settings)
    _reject_unsupported_images(req, model)
    body = _messages_body(
        req,
        settings,
        model,
        stream=False,
        messages=_initial_messages(req),
        system_prompt=_review_system_prompt(req),
    )
    payload = await _post_mimo(body, settings)
    content = _extract_response_text(payload)
    return parse_code_review_response(
        content,
        provider="mimo",
        model=model,
        duration_ms=_duration_ms(started),
        usage=_payload_usage(payload),
    )


async def stream_mimo(req: Any, settings: Any) -> AsyncIterator[str]:
    """Stream MiMo results while hiding backend-only tool protocol markers."""
    started = time.time()
    model = _model(req, settings)
    usage: dict[str, Any] = {}
    try:
        _reject_unsupported_images(req, model)
        messages = _initial_messages(req)
        system_prompt = _system_prompt(req)
        max_turns = _max_backend_tool_turns(settings)
        max_calls_per_turn = _max_backend_tool_calls_per_turn(settings)
        allowed_tools = _allowed_tools(req)

        for _ in range(max_turns):
            body = _messages_body(
                req,
                settings,
                model,
                stream=True,
                messages=messages,
                system_prompt=system_prompt,
            )
            content_parts: list[str] = []
            text_chunks: list[str] = []
            turn_usage: dict[str, Any] = {}
            stop_reason = ""

            async for event in _stream_post_mimo(body, settings):
                raise_for_stream_error_event(event)
                turn_usage = merge_numeric_usage(turn_usage, event_usage(event))
                stop_reason = extract_stop_reason(event) or stop_reason
                text_delta = stream_text_delta(event)
                if not text_delta:
                    continue
                content_parts.append(text_delta)
                text_chunks.append(text_delta)

            usage = _merge_usage(usage, turn_usage)
            content = "".join(content_parts)
            backend_calls = extract_backend_tool_calls(content)
            malformed_backend_call = has_malformed_backend_tool_call(
                content,
                backend_calls,
            )
            _log_model_turn(
                req,
                settings,
                model,
                content,
                stop_reason,
                turn_usage,
                bool(backend_calls or malformed_backend_call),
            )
            if stop_reason == "max_tokens":
                yield _sse(
                    _message_complete_payload(
                        content=_clean_final_content(content),
                        tool_calls=[],
                        success=False,
                        duration_ms=_duration_ms(started),
                        usage=usage,
                        model=model,
                        code=_MIMO_INCOMPLETE_CODE,
                        error="MiMo stopped before completion because max_tokens was reached",
                    )
                )
                return

            if not backend_calls and not malformed_backend_call:
                clean_backend_content = clean_backend_tool_protocol_text(content)
                tool_calls = _filter_allowed(
                    extract_tool_calls(clean_backend_content),
                    allowed_tools,
                )
                if has_malformed_frontend_tool_call(
                    clean_backend_content,
                    tool_calls,
                ):
                    clean_content = clean_malformed_frontend_tool_text(
                        clean_backend_content
                    )
                    tool_calls = []
                else:
                    clean_content = _clean_final_content(content)
                for chunk in _stream_visible_chunks(text_chunks, clean_content):
                    yield _sse({"type": "text_delta", "content": chunk})
                yield _sse(
                    _message_complete_payload(
                        content=clean_content,
                        tool_calls=tool_calls,
                        success=True,
                        duration_ms=_duration_ms(started),
                        usage=usage,
                        model=model,
                    )
                )
                return

            messages.append({"role": "assistant", "content": content})
            if malformed_backend_call:
                results = [_malformed_backend_tool_result(content, backend_calls)]
                _log_backend_tool_results(req, settings, results)
            else:
                results = await _execute_backend_tool_calls_with_log(
                    req,
                    settings,
                    backend_calls,
                    max_calls_per_turn,
                )
            messages.append(
                {
                    "role": "user",
                    "content": format_backend_tool_results(results),
                }
            )

        raise backend_tool_loop_limit_error()
    except Exception as exc:
        code, error = _exception_code_and_message(exc)
        yield _sse(
            {
                "type": "message_complete",
                "content": "",
                "tool_calls": [],
                "success": False,
                "code": code,
                "error": error,
                "duration_ms": _duration_ms(started),
                "usage": usage,
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
        turn_usage = _payload_usage(payload)
        usage = _merge_usage(usage, turn_usage)

        content = _extract_response_text(payload)
        stop_reason = extract_stop_reason(payload)
        backend_calls = extract_backend_tool_calls(content)
        malformed_backend_call = has_malformed_backend_tool_call(
            content,
            backend_calls,
        )
        _log_model_turn(
            req,
            settings,
            model,
            content,
            stop_reason,
            turn_usage,
            bool(backend_calls or malformed_backend_call),
        )
        if stop_reason == "max_tokens":
            return {
                "content": content,
                "usage": usage,
                "success": False,
                "code": _MIMO_INCOMPLETE_CODE,
                "error": "MiMo stopped before completion because max_tokens was reached",
            }

        if not backend_calls and not malformed_backend_call:
            return {"content": content, "usage": usage, "success": True}

        messages.append({"role": "assistant", "content": content})
        if malformed_backend_call:
            results = [_malformed_backend_tool_result(content, backend_calls)]
            _log_backend_tool_results(req, settings, results)
        else:
            results = await _execute_backend_tool_calls_with_log(
                req,
                settings,
                backend_calls,
                max_calls_per_turn,
            )
        messages.append(
            {
                "role": "user",
                "content": format_backend_tool_results(results),
            }
        )

    raise backend_tool_loop_limit_error()


async def _post_mimo(body: dict[str, Any], settings: Any) -> dict[str, Any]:
    return await post_mimo(body, settings)


async def _stream_post_mimo(
    body: dict[str, Any],
    settings: Any,
) -> AsyncIterator[dict[str, Any]]:
    async for event in stream_post_mimo(body, settings):
        yield event


def _exception_code_and_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        code = str(exc.detail.get("code") or "MIMO_PROVIDER_ERROR")
        message = str(exc.detail.get("message") or exc.detail.get("error") or "")
        return code, message or code
    return "MIMO_PROVIDER_ERROR", str(exc)


def _message_complete_payload(
    *,
    content: str,
    tool_calls: list[dict[str, Any]],
    success: bool,
    duration_ms: int,
    usage: dict[str, Any],
    model: str,
    code: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "message_complete",
        "content": content,
        "tool_calls": tool_calls,
        "success": success,
        "duration_ms": duration_ms,
        "usage": usage,
        "model": model,
        "provider": "mimo",
    }
    if code:
        payload["code"] = code
    if error:
        payload["error"] = error
    return payload


def _payload_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    return dict(usage) if isinstance(usage, dict) else {}


def _merge_usage(
    accumulated: dict[str, Any],
    turn_usage: dict[str, Any],
) -> dict[str, Any]:
    if not turn_usage:
        return accumulated

    if accumulated and "turns" not in accumulated:
        turns = [dict(accumulated)]
    else:
        turns = list(accumulated.get("turns") or [])
    current = {key: value for key, value in accumulated.items() if key != "turns"}
    merged = merge_numeric_usage(current, turn_usage)
    turns.append(dict(turn_usage))
    if len(turns) > 1:
        merged["turns"] = turns
    return merged


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


def _review_system_prompt(req: Any) -> str:
    system_prompt = str(
        _request_value(req, "systemPrompt", "system_prompt", default="")
        or ""
    )
    if system_prompt:
        return f"{system_prompt}\n\n{REVIEW_SYSTEM_PROMPT}"
    return REVIEW_SYSTEM_PROMPT


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
    if _request_value(req, "images", default=[]) and model not in MIMO_IMAGE_MODELS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_DOES_NOT_SUPPORT_IMAGES",
                "fallbackModel": _fallback_image_model(),
            },
        )


def _fallback_image_model() -> str:
    default = "mimo-v2.5"
    if default in MIMO_IMAGE_MODELS:
        return default
    return sorted(MIMO_IMAGE_MODELS)[0] if MIMO_IMAGE_MODELS else ""


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


async def _execute_backend_tool_calls_with_log(
    req: Any,
    settings: Any,
    backend_calls: list[Any],
    max_calls_per_turn: int,
) -> list[BackendToolResult]:
    limited_calls = backend_calls[:max_calls_per_turn]
    _log_backend_tool_calls(req, settings, limited_calls)
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
    _log_backend_tool_results(req, settings, results)
    return results


def _log_model_turn(
    req: Any,
    settings: Any,
    model: str,
    content: str,
    stop_reason: str,
    usage: dict[str, Any],
    has_backend_tool_call: bool,
) -> None:
    conversation_id = _conversation_id(req)
    frontend_tool_calls = _frontend_tool_calls(content, req)
    append_conversation_event(
        settings,
        conversation_id,
        {
            "type": "model_turn",
            "conversationId": conversation_id,
            "runId": _run_id(req),
            "provider": "mimo",
            "model": model,
            "content": content,
            "stopReason": stop_reason,
            "usage": usage,
            "hasBackendToolCall": has_backend_tool_call,
            "frontendToolCallCount": len(frontend_tool_calls),
            "frontendToolCallNames": [
                str(call.get("name") or "") for call in frontend_tool_calls
            ],
            "contentDiagnostics": _content_diagnostics(
                content,
                frontend_tool_calls,
            ),
        },
    )


def _log_backend_tool_calls(
    req: Any,
    settings: Any,
    calls: list[Any],
) -> None:
    conversation_id = _conversation_id(req)
    for call in calls:
        append_conversation_event(
            settings,
            conversation_id,
            {
                "type": "backend_tool_call",
                "conversationId": conversation_id,
                "runId": _run_id(req),
                "provider": "mimo",
                "toolName": call.name,
                "arguments": model_to_dict(call.arguments),
            },
        )


def _log_backend_tool_results(
    req: Any,
    settings: Any,
    results: list[BackendToolResult],
) -> None:
    conversation_id = _conversation_id(req)
    for result in results:
        append_conversation_event(
            settings,
            conversation_id,
            {
                "type": "backend_tool_result",
                "conversationId": conversation_id,
                "runId": _run_id(req),
                "provider": "mimo",
                "toolName": result.name,
                "status": result.status,
                "code": result.code,
                "error": result.error,
                "result": model_to_dict(result.result),
            },
        )


def _conversation_id(req: Any) -> str:
    return str(_request_value(req, "conversationId", "conversation_id", default="") or "")


def _run_id(req: Any) -> str:
    return str(_request_value(req, "runId", "run_id", default="") or "")


def _clean_final_content(content: str) -> str:
    return clean_tool_protocol_text(
        clean_backend_tool_protocol_text(content)
    ).strip()


def _stream_visible_chunks(text_chunks: list[str], clean_content: str) -> list[str]:
    safe_chunks, safe_visible = visible_text_chunks(text_chunks)
    if safe_visible == clean_content:
        return safe_chunks
    return [clean_content] if clean_content else []


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(_response(**payload), ensure_ascii=False)}\n\n"


def _malformed_backend_tool_result(
    content: str,
    backend_calls: list[Any],
) -> BackendToolResult:
    return frontend_tool_misuse_result(content, backend_calls) or malformed_backend_tool_result()


def _frontend_tool_calls(content: str, req: Any) -> list[dict[str, Any]]:
    return _filter_allowed(extract_tool_calls(content), _allowed_tools(req))


def _content_diagnostics(
    content: str,
    frontend_tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "containsControlCharacters": _contains_control_characters(content),
        "containsMalformedFrontendToolCall": _contains_malformed_frontend_tool_call(
            content,
            frontend_tool_calls,
        ),
    }


def _contains_control_characters(content: str) -> bool:
    return any(
        ord(char) < 32 and char not in {"\t", "\n", "\r"} for char in content
    )


def _contains_malformed_frontend_tool_call(
    content: str,
    frontend_tool_calls: list[dict[str, Any]],
) -> bool:
    if has_malformed_frontend_tool_call(content, frontend_tool_calls):
        return True
    cleaned = clean_tool_protocol_text(content)
    return "[TOOL_CALL]" in cleaned or "[TOL_CALL]" in cleaned


def _duration_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
