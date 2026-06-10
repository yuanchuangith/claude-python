from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    query,
)

from .backend_tools import (
    BackendToolResult,
    backend_tool_loop_limit_error,
    clean_backend_tool_protocol_text,
    execute_backend_tool_calls,
    extract_backend_tool_calls,
    format_backend_tool_results,
    frontend_tool_misuse_result,
)
from .mimo_protocol import has_malformed_backend_tool_call
from .models import DESIGN_TOOLS
from .session_log import append_conversation_event, model_to_dict
from .tool_protocol import clean_tool_protocol_text, extract_tool_calls

_CLAUDE_CODE_INTERNAL_TOOL_PROMPT = """Claude Code internal tools:
- You may use the Claude Code internal tools exposed by the backend only to read and analyze project files.
- Do not describe or emit Claude Code internal tools as [TOOL_CALL] output.
- Do not ask the frontend to execute Claude Code internal tools such as Read, Grep, Glob, LS, Bash, Edit, or Write.

ActionDesign frontend tools:
- Only ActionDesign frontend tools should be emitted with [TOOL_CALL] tool_name({...}).
- Backend filtering will only return ActionDesign frontend tools to the frontend executor."""

_CLAUDE_CODE_BACKEND_TOOL_PROMPT = """Backend-only knowledge tools:
- When you need ActionDesign component parameters, events, usage, examples, or constraints,
  first call [BACKEND_TOOL_CALL] knowledge.search({"query":"..."}).
- If search snippets are not enough, call
  [BACKEND_TOOL_CALL] knowledge.read({"path":"...", "heading":"..."}).
- Backend tool calls are executed only by the backend and must never be shown to the frontend.
- Do not use [BACKEND_TOOL_CALL] for ActionDesign canvas operations.
- get_element_detail, list_elements, get_page_actions, propose_plan, create_node, insert_node, delete_node, and preview_code are frontend tools and must never be used with [BACKEND_TOOL_CALL]."""


async def call_claude_code(req: Any, settings: Any) -> dict[str, Any]:
    started = time.time()
    allowed = _allowed_tools(req)
    loop_result = await _run_claude_code_agent_loop(req, settings)
    content = loop_result["content"]
    clean_backend_content = clean_backend_tool_protocol_text(content)
    parsed_tool_calls = _filter_allowed(
        extract_tool_calls(clean_backend_content), allowed
    )
    tool_calls = _merge_tool_calls(loop_result["native_tool_calls"], parsed_tool_calls)
    clean_content = clean_tool_protocol_text(content).strip() if tool_calls else content
    clean_content = clean_backend_tool_protocol_text(clean_content).strip()
    return _response(
        content=clean_content,
        tool_calls=tool_calls,
        success=True,
        error=None,
        duration_ms=_duration_ms(started),
        usage={"total_cost_usd": loop_result["total_cost_usd"]},
        model=_model(req, settings),
        provider="claude-code",
    )


async def stream_claude_code(req: Any, settings: Any) -> AsyncIterator[str]:
    started = time.time()
    allowed = _allowed_tools(req)

    try:
        loop_result = await _run_claude_code_agent_loop(req, settings)
        full_text = loop_result["content"]
        clean_backend_content = clean_backend_tool_protocol_text(full_text)
        tool_calls = _merge_tool_calls(
            loop_result["native_tool_calls"],
            _filter_allowed(extract_tool_calls(clean_backend_content), allowed),
        )
        clean_content = (
            clean_tool_protocol_text(full_text).strip() if tool_calls else full_text
        )
        clean_content = clean_backend_tool_protocol_text(clean_content).strip()
        if clean_content:
            yield _sse({"type": "text_delta", "content": clean_content})
        yield _sse(
            {
                "type": "message_complete",
                "content": clean_content,
                "tool_calls": tool_calls,
                "success": True,
                "duration_ms": _duration_ms(started),
                "usage": {"total_cost_usd": loop_result["total_cost_usd"]},
                "model": _model(req, settings),
                "provider": "claude-code",
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
                "model": _model(req, settings),
                "provider": "claude-code",
            }
        )


async def _run_claude_code_agent_loop(
    req: Any,
    settings: Any,
) -> dict[str, Any]:
    prompt = _prompt(req)
    max_turns = int(_setting(settings, "claude_code_max_backend_tool_turns", default=6))
    max_calls_per_turn = int(
        _setting(settings, "claude_code_max_backend_tool_calls_per_turn", default=4)
    )
    native_tool_calls: list[dict[str, Any]] = []
    total_cost = 0.0
    last_content = ""

    for _ in range(max_turns):
        response = await _collect_claude_code_response(
            req,
            settings,
            prompt,
            include_partial_messages=False,
        )
        last_content = response["content"]
        native_tool_calls.extend(response["native_tool_calls"])
        total_cost = response["total_cost_usd"] or total_cost

        backend_calls = extract_backend_tool_calls(last_content)
        malformed_backend_call = has_malformed_backend_tool_call(
            last_content,
            backend_calls,
        )
        _log_model_turn(
            req,
            settings,
            last_content,
            {"total_cost_usd": response["total_cost_usd"]},
            bool(backend_calls or malformed_backend_call),
        )
        if not backend_calls and not malformed_backend_call:
            return {
                "content": last_content,
                "native_tool_calls": native_tool_calls,
                "total_cost_usd": total_cost,
            }

        if malformed_backend_call:
            results = [_malformed_backend_tool_result(last_content, backend_calls)]
            _log_backend_tool_results(req, settings, results)
        else:
            results = await _execute_backend_tool_calls_with_log(
                req,
                settings,
                backend_calls,
                max_calls_per_turn,
            )
        prompt = _next_backend_tool_prompt(prompt, last_content, results)

    raise backend_tool_loop_limit_error()


async def _collect_claude_code_response(
    req: Any,
    settings: Any,
    prompt: str,
    *,
    include_partial_messages: bool,
) -> dict[str, Any]:
    options = _options(req, settings, include_partial_messages=include_partial_messages)
    allowed = _allowed_tools(req)
    content_parts: list[str] = []
    final_assistant_parts: list[str] = []
    native_tool_calls: list[dict[str, Any]] = []
    total_cost = 0.0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, StreamEvent):
            text = _stream_text_delta(message)
            if text:
                content_parts.append(text)
        elif isinstance(message, AssistantMessage):
            final_assistant_parts = _text_blocks(message)
            content_parts.extend(final_assistant_parts)
            native_tool_calls.extend(_native_tool_calls(message, allowed))
        elif isinstance(message, ResultMessage):
            total_cost = message.total_cost_usd or total_cost

    return {
        "content": "".join(final_assistant_parts or content_parts),
        "native_tool_calls": native_tool_calls,
        "total_cost_usd": total_cost,
    }


def _next_backend_tool_prompt(
    previous_prompt: str,
    assistant_content: str,
    results: list[BackendToolResult],
) -> str:
    return (
        f"{previous_prompt}\n\n"
        "Previous assistant response contained backend-only tool calls:\n"
        f"{assistant_content}\n\n"
        "Backend tool results:\n"
        f"{format_backend_tool_results(results)}\n\n"
        "Continue the ActionDesign answer. Do not repeat backend tool results."
    )


def _options(
    req: Any,
    settings: Any,
    *,
    include_partial_messages: bool,
) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = {
        "tools": _internal_tools(settings),
        "allowed_tools": _auto_allowed_internal_tools(settings),
        "include_partial_messages": include_partial_messages,
    }
    model = _model(req, settings)
    if model:
        kwargs["model"] = model

    max_turns = _request_value(req, "maxTurns", "max_turns", default=None)
    if max_turns is not None:
        kwargs["max_turns"] = int(max_turns)

    project_path = _request_value(req, "projectPath", "project_path", default="")
    if project_path:
        path = Path(str(project_path))
        if path.exists():
            kwargs["cwd"] = path

    cli_path = _setting(settings, "claude_code_cli_path", default=None)
    if cli_path:
        kwargs["cli_path"] = cli_path

    return ClaudeAgentOptions(**kwargs)


def _native_tool_calls(
    message: AssistantMessage,
    allowed_tools: set[str],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if not isinstance(block, ToolUseBlock):
            continue
        tool_name = block.name
        tool_args = block.input or {}
        if block.name == "Question":
            normalized = extract_tool_calls(
                f"[TOOL_CALL] Question({json.dumps(tool_args, ensure_ascii=False)})"
            )
            if normalized:
                tool_name = str(normalized[0]["name"])
                tool_args = normalized[0]["arguments"]
        if tool_name not in allowed_tools:
            continue
        calls.append(
            {
                "id": block.id,
                "name": tool_name,
                "arguments": tool_args,
            }
        )
    return calls


def _text_blocks(message: AssistantMessage) -> list[str]:
    return [block.text for block in message.content if isinstance(block, TextBlock)]


def _stream_text_delta(message: StreamEvent) -> str:
    event = message.event
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    return str(delta.get("text") or "")


def _merge_tool_calls(
    native_tool_calls: Iterable[dict[str, Any]],
    parsed_tool_calls: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(native_tool_calls)
    native_mirrors = {_call_key(call) for call in merged}
    for call in parsed_tool_calls:
        if _call_key(call) in native_mirrors:
            continue
        merged.append(call)
    return merged


def _call_key(call: dict[str, Any]) -> tuple[str, str]:
    return (
        str(call.get("name", "")),
        json.dumps(call.get("arguments", {}), sort_keys=True, ensure_ascii=False),
    )


def _filter_allowed(
    tool_calls: Iterable[dict[str, Any]],
    allowed_tools: set[str],
) -> list[dict[str, Any]]:
    return [call for call in tool_calls if call.get("name") in allowed_tools]


def _allowed_tools(req: Any) -> set[str]:
    tools = _request_value(req, "toolNames", "tool_names", default=[]) or []
    if not tools:
        return set(DESIGN_TOOLS)
    return {str(tool) for tool in tools} & set(DESIGN_TOOLS)


def _prompt(req: Any) -> str:
    prompt = str(_request_value(req, "prompt", default=""))
    return (
        f"{_CLAUDE_CODE_INTERNAL_TOOL_PROMPT}\n\n"
        f"{_CLAUDE_CODE_BACKEND_TOOL_PROMPT}\n\n{prompt}"
    )


def _internal_tools(settings: Any) -> list[str]:
    tools = _setting(settings, "claude_code_internal_tools", default=None)
    if tools is None:
        return ["Read", "Grep", "Glob", "LS"]
    return [str(tool) for tool in tools if str(tool).strip()]


def _auto_allowed_internal_tools(settings: Any) -> list[str]:
    if not bool(
        _setting(settings, "claude_code_auto_allow_internal_tools", default=True)
    ):
        return []
    return _internal_tools(settings)


def _model(req: Any, settings: Any) -> str:
    return str(
        _request_value(
            req,
            "model",
            default=_default_model(settings),
        )
        or _default_model(settings)
    )


def _default_model(settings: Any) -> str:
    configured = str(_setting(settings, "claude_code_default_model", default="") or "")
    if configured:
        return configured
    models = _setting(settings, "claude_code_models", default=[]) or []
    for model in models:
        model = str(model).strip()
        if model:
            return model
    return str(_setting(settings, "mimo_default_model", default="mimo-v2.5") or "mimo-v2.5")


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
    content: str,
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
            "provider": "claude-code",
            "model": _model(req, settings),
            "content": content,
            "stopReason": "",
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
                "provider": "claude-code",
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
                "provider": "claude-code",
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


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(_response(**payload), ensure_ascii=False)}\n\n"


def _malformed_backend_tool_result(
    content: str,
    backend_calls: list[Any],
) -> BackendToolResult:
    result = frontend_tool_misuse_result(content, backend_calls)
    if result is not None:
        return result
    return BackendToolResult(
        name="backend.tool_call",
        status="failed",
        error="Backend tool call is malformed or incomplete",
        code="BACKEND_TOOL_ARGUMENTS_INVALID",
    )


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
    has_marker = "[TOOL_CALL]" in content or "[TOL_CALL]" in content
    if has_marker and not frontend_tool_calls:
        return True
    cleaned = clean_tool_protocol_text(content)
    return "[TOOL_CALL]" in cleaned or "[TOL_CALL]" in cleaned


def _duration_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
