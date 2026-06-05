from __future__ import annotations

from typing import Any

from .backend_tools import BackendToolResult
from .tool_protocol import clean_tool_protocol_text, extract_tool_calls

BACKEND_TOOL_CALL_MARKER = "[BACKEND_TOOL_CALL]"
BACKEND_TOOL_RESULT_MARKER = "[BACKEND_TOOL_RESULT]"
FRONTEND_TOOL_MARKERS = ("[TOOL_CALL]", "[TOL_CALL]")
PROTOCOL_CALL_MARKERS = (*FRONTEND_TOOL_MARKERS, BACKEND_TOOL_CALL_MARKER)
PROTOCOL_MARKERS = (*PROTOCOL_CALL_MARKERS, BACKEND_TOOL_RESULT_MARKER)


def has_malformed_backend_tool_call(
    content: str,
    backend_calls: list[Any],
) -> bool:
    return BACKEND_TOOL_CALL_MARKER in content and not backend_calls


def malformed_backend_tool_result() -> BackendToolResult:
    return BackendToolResult(
        name="backend.tool_call",
        status="failed",
        error="Backend tool call is malformed or incomplete",
        code="BACKEND_TOOL_ARGUMENTS_INVALID",
    )


def has_malformed_frontend_tool_call(
    content: str,
    tool_calls: list[dict[str, Any]],
) -> bool:
    return any(marker in content for marker in FRONTEND_TOOL_MARKERS) and not tool_calls


def clean_final_content(content: str) -> str:
    return clean_tool_protocol_text(content).strip()


def clean_malformed_frontend_tool_text(content: str) -> str:
    indexes = [
        content.find(marker)
        for marker in FRONTEND_TOOL_MARKERS
        if content.find(marker) != -1
    ]
    if not indexes:
        return content.strip()
    return content[: min(indexes)].strip()


def visible_text_chunks(chunks: list[str]) -> tuple[list[str], str]:
    pending = ""
    safe_chunks: list[str] = []
    for chunk in chunks:
        emitted, pending = emit_safe_text_after_turn(pending, chunk)
        safe_chunks.extend(emitted)
    flushed = flush_safe_text_after_turn(pending)
    if flushed:
        safe_chunks.append(flushed)
    visible = "".join(safe_chunks).strip()
    return safe_chunks, visible


def emit_safe_text_after_turn(
    pending: str,
    chunk: str,
) -> tuple[list[str], str]:
    pending += chunk
    chunks: list[str] = []
    while pending:
        marker_index = pending.find("[")
        if marker_index == -1:
            chunks.append(pending)
            pending = ""
            break

        if marker_index > 0:
            chunks.append(pending[:marker_index])
            pending = pending[marker_index:]
            continue

        marker = matching_protocol_marker(pending)
        if marker is None:
            if is_protocol_marker_prefix(pending):
                break
            next_marker = pending.find("[", 1)
            if next_marker == -1:
                chunks.append(pending)
                pending = ""
            else:
                chunks.append(pending[:next_marker])
                pending = pending[next_marker:]
            continue

        if marker == BACKEND_TOOL_RESULT_MARKER:
            pending = ""
            break

        end = find_protocol_call_end(pending)
        if end is None:
            pending = ""
            break
        pending = pending[end:]

    return chunks, pending


def flush_safe_text_after_turn(pending: str) -> str:
    if contains_protocol_marker(pending):
        return ""
    return pending


def matching_protocol_marker(text: str) -> str | None:
    return next(
        (marker for marker in PROTOCOL_MARKERS if text.startswith(marker)),
        None,
    )


def is_protocol_marker_prefix(text: str) -> bool:
    return any(marker.startswith(text) for marker in PROTOCOL_MARKERS)


def contains_protocol_marker(text: str) -> bool:
    return any(marker in text for marker in PROTOCOL_MARKERS)


def find_protocol_call_end(text: str) -> int | None:
    paren_start = text.find("(")
    if paren_start == -1:
        return None

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(paren_start, len(text)):
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
                return index + 1
    return None


def has_complete_frontend_tool_call(content: str) -> bool:
    return bool(extract_tool_calls(content))
