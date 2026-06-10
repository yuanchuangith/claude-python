from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from .models import DESIGN_TOOLS

BACKEND_TOOL_ALLOWED_NAMES = frozenset(
    {
        "mcp.list_resources",
        "mcp.read_resource",
        "mcp.search",
        "mcp.call_tool",
        "skill.search",
        "skill.load",
        "knowledge.search",
        "knowledge.read",
    }
)

_RE_BACKEND_TOOL_CALL = re.compile(
    r"\[BACKEND_TOOL_CALL\]\s*([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\s*\(",
    re.DOTALL,
)
_RE_ANY_BACKEND_TOOL_CALL = re.compile(
    r"\[BACKEND_TOOL_CALL\]\s*([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*\(",
    re.DOTALL,
)
_RE_BACKEND_CLEAN_MARKER = re.compile(r"\[BACKEND_TOOL_(?:CALL|RESULT)\]")


@dataclass
class BackendToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class BackendToolResult:
    name: str
    status: Literal["success", "failed"]
    result: Any = None
    error: str = ""
    code: str = ""


class BackendToolExecutor(Protocol):
    async def execute(self, call: BackendToolCall) -> BackendToolResult:
        """Execute a backend-only tool call."""


class NoopBackendToolExecutor:
    async def execute(self, call: BackendToolCall) -> BackendToolResult:
        return BackendToolResult(
            name=call.name,
            status="failed",
            error="Backend tool executor is not configured",
            code="BACKEND_TOOL_NOT_CONFIGURED",
        )


def extract_backend_tool_calls(content: str) -> list[BackendToolCall]:
    calls: list[BackendToolCall] = []
    for match in _RE_BACKEND_TOOL_CALL.finditer(content):
        result = _extract_balanced_args(content, match.end() - 1)
        if result is None:
            continue
        args_raw, _ = result
        try:
            arguments = json.loads(args_raw.strip() or "{}")
        except json.JSONDecodeError:
            calls.append(
                BackendToolCall(
                    name=match.group(1),
                    arguments={"_parse_error": args_raw.strip()},
                )
            )
            continue
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        calls.append(BackendToolCall(name=match.group(1), arguments=arguments))
    return calls


def clean_backend_tool_protocol_text(content: str) -> str:
    if "[BACKEND_TOOL_" not in content:
        return content
    cleaned = _remove_backend_tool_calls(content)
    return _remove_backend_tool_results(cleaned).strip()


def frontend_tool_misuse_result(
    content: str,
    backend_calls: list[BackendToolCall],
) -> BackendToolResult | None:
    if backend_calls or "[BACKEND_TOOL_CALL]" not in content:
        return None

    match = _RE_ANY_BACKEND_TOOL_CALL.search(content)
    if match is None:
        return None

    tool_name = match.group(1)
    if tool_name not in DESIGN_TOOLS:
        return None

    return BackendToolResult(
        name=tool_name,
        status="failed",
        error=(
            f"{tool_name} is an ActionDesign frontend tool. "
            f"Use [TOOL_CALL] {tool_name}(...) instead of [BACKEND_TOOL_CALL]."
        ),
        code="BACKEND_TOOL_FRONTEND_TOOL_MISUSED",
    )


async def execute_backend_tool_calls(
    calls: list[BackendToolCall],
    settings: Any,
) -> list[BackendToolResult]:
    executor = getattr(settings, "backend_tool_executor", None)
    if executor is None:
        executor = _default_backend_tool_executor(settings)

    results: list[BackendToolResult] = []
    for call in calls:
        if call.name not in BACKEND_TOOL_ALLOWED_NAMES:
            results.append(
                BackendToolResult(
                    name=call.name,
                    status="failed",
                    error=f"Backend tool is not allowed: {call.name}",
                    code="BACKEND_TOOL_NOT_ALLOWED",
                )
            )
            continue
        if call.name == "mcp.call_tool" and not _is_read_only_mcp_call_tool(
            call,
            settings,
        ):
            results.append(
                BackendToolResult(
                    name=call.name,
                    status="failed",
                    error="Nested MCP tool is not configured as read-only",
                    code="BACKEND_TOOL_NOT_ALLOWED",
                )
            )
            continue
        if "_parse_error" in call.arguments:
            results.append(
                BackendToolResult(
                    name=call.name,
                    status="failed",
                    error="Backend tool arguments are not valid JSON",
                    code="BACKEND_TOOL_ARGUMENTS_INVALID",
                )
            )
            continue
        try:
            results.append(await executor.execute(call))
        except Exception as exc:
            results.append(
                BackendToolResult(
                    name=call.name,
                    status="failed",
                    error=str(exc),
                    code="BACKEND_TOOL_FAILED",
                )
            )
    return results


def format_backend_tool_results(results: list[BackendToolResult]) -> str:
    blocks: list[str] = []
    for result in results:
        lines = [
            f"[BACKEND_TOOL_RESULT] {result.name}",
            f"status: {result.status}",
        ]
        if result.code:
            lines.append(f"code: {result.code}")
        if result.status == "success":
            lines.append("result:")
            lines.append(json.dumps(result.result, ensure_ascii=False, default=str))
        else:
            lines.append(f"error: {result.error}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def backend_tool_loop_limit_error() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "code": "BACKEND_TOOL_LOOP_LIMIT",
            "message": "MiMo backend tool loop limit exceeded",
        },
    )


def _default_backend_tool_executor(settings: Any) -> BackendToolExecutor:
    from .actiondesign_backend_executor import ActionDesignBackendToolExecutor

    return ActionDesignBackendToolExecutor(settings)


def _is_read_only_mcp_call_tool(call: BackendToolCall, settings: Any) -> bool:
    nested_tool = _nested_mcp_tool_name(call.arguments)
    if not nested_tool:
        return False
    allowed = set(_read_only_mcp_tool_names(settings))
    return nested_tool in allowed


def _read_only_mcp_tool_names(settings: Any) -> list[str]:
    raw = getattr(settings, "mcp_read_only_tool_names", [])
    values = raw.split(",") if isinstance(raw, str) else raw
    return [str(tool).strip() for tool in values if str(tool).strip()]


def _nested_mcp_tool_name(arguments: dict[str, Any]) -> str:
    for key in ("name", "tool", "toolName", "tool_name"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _remove_backend_tool_calls(content: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _RE_BACKEND_TOOL_CALL.finditer(content):
        result = _extract_balanced_args(content, match.end() - 1)
        if result is None:
            continue
        _, end = result
        pieces.append(content[cursor : match.start()])
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces)


def _remove_backend_tool_results(content: str) -> str:
    if "[BACKEND_TOOL_RESULT]" not in content:
        return content
    marker = _RE_BACKEND_CLEAN_MARKER.search(content)
    if marker is None:
        return content
    return content[: marker.start()]


def _extract_balanced_args(text: str, start: int) -> tuple[str, int] | None:
    if start >= len(text) or text[start] != "(":
        return None

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return None
