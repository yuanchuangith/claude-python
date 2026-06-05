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
)
from .models import DESIGN_TOOLS
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
- Do not use [BACKEND_TOOL_CALL] for ActionDesign canvas operations."""


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
        if not backend_calls:
            return {
                "content": last_content,
                "native_tool_calls": native_tool_calls,
                "total_cost_usd": total_cost,
            }

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
            default=_setting(settings, "claude_code_default_model", default=""),
        )
        or _setting(settings, "claude_code_default_model", default="")
    )


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


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(_response(**payload), ensure_ascii=False)}\n\n"


def _duration_ms(started: float) -> int:
    return int((time.time() - started) * 1000)
