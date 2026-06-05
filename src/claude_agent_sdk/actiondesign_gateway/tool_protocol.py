"""ActionDesign tool-call protocol parsing helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

_RE_TOOL_CALL = re.compile(r"\[(?:TOOL_CALL|TOL_CALL)\]\s*(\w+)\s*\(", re.DOTALL)
_RE_TOOL_CALL_MARKER = re.compile(r"\[(?:TOOL_CALL|TOL_CALL)\]")
_RE_CODE_BLOCK = re.compile(r"```(?:jsonc?|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)
_RE_DATA_URL = re.compile(r"^data:image/\w+;base64,", re.IGNORECASE)
_RE_TRAILING_COMMA = re.compile(r",\s*([\]}])")
_RE_SINGLE_QUOTED_KEY = re.compile(r"'([^']*)'\s*:")

_MARKERS = ("[TOOL_CALL]", "[TOL_CALL]")


def normalize_image_data(data: str) -> str:
    """Remove a base64 image data URL prefix."""
    return _RE_DATA_URL.sub("", data)


def extract_tool_calls(content: str) -> list[dict[str, Any]]:
    """Extract ActionDesign tool calls from protocol text or JSON actions."""
    normalized = content.replace("[TOL_CALL]", "[TOOL_CALL]")
    tool_calls: list[dict[str, Any]] = []

    for match in _RE_TOOL_CALL.finditer(normalized):
        result = _extract_balanced(normalized, match.end() - 1, "(", ")")
        if result is None:
            continue
        args_text, _ = result
        args = _parse_jsonish(args_text.strip())
        if args is None:
            continue
        name, args = _normalize_tool_call(match.group(1), args)
        tool_calls.append({"name": name, "arguments": args})

    for raw in _iter_json_action_sources(normalized):
        data = _parse_jsonish(raw)
        if data is not None:
            tool_calls.extend(_extract_actions_from_json(data))

    return tool_calls


def clean_tool_protocol_text(content: str) -> str:
    """Remove embedded tool protocol calls while preserving surrounding text."""
    normalized = content.replace("[TOL_CALL]", "[TOOL_CALL]")
    pieces: list[str] = []
    cursor = 0

    for match in _RE_TOOL_CALL.finditer(normalized):
        result = _extract_balanced(normalized, match.end() - 1, "(", ")")
        if result is None:
            continue
        _, end = result
        pieces.append(normalized[cursor : match.start()])
        cursor = end

    pieces.append(normalized[cursor:])
    return "".join(pieces)


def emit_safe_text(
    pending: str,
    chunk: str,
    allowed_tools: Iterable[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], str]:
    """Emit safe text chunks while suppressing split tool-call protocol."""
    pending += chunk
    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    while pending:
        marker_index = pending.find("[")
        if marker_index == -1:
            text_chunks.append(pending)
            pending = ""
            break

        if marker_index > 0:
            text_chunks.append(pending[:marker_index])
            pending = pending[marker_index:]
            continue

        marker = _matching_marker(pending)
        if marker is not None:
            end = _find_tool_call_end(pending)
            if end is None:
                break
            parsed = extract_tool_calls(pending[:end])
            tool_calls.extend(_filter_allowed(parsed, allowed_tools))
            pending = pending[end:]
            continue

        if _is_marker_prefix(pending):
            break

        next_marker = pending.find("[", 1)
        if next_marker == -1:
            text_chunks.append(pending)
            pending = ""
        else:
            text_chunks.append(pending[:next_marker])
            pending = pending[next_marker:]

    return text_chunks, tool_calls, pending


def flush_pending(
    pending: str,
    allowed_tools: Iterable[str] | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Flush buffered streaming text at end-of-stream."""
    if not pending:
        return None, []

    parsed = _filter_allowed(extract_tool_calls(pending), allowed_tools)
    if parsed:
        return None, parsed
    return pending, []


def _filter_allowed(
    tool_calls: Iterable[dict[str, Any]],
    allowed_tools: Iterable[str] | None,
) -> list[dict[str, Any]]:
    if allowed_tools is None:
        return list(tool_calls)
    allowed = set(allowed_tools)
    return [call for call in tool_calls if call.get("name") in allowed]


def _matching_marker(text: str) -> str | None:
    return next((marker for marker in _MARKERS if text.startswith(marker)), None)


def _is_marker_prefix(text: str) -> bool:
    return any(marker.startswith(text) for marker in _MARKERS)


def _find_tool_call_end(text: str) -> int | None:
    paren_start = text.find("(")
    if paren_start == -1:
        return None
    result = _extract_balanced(text, paren_start, "(", ")")
    if result is None:
        return None
    return result[1]


def _extract_balanced(
    text: str,
    start: int,
    opener: str,
    closer: str,
) -> tuple[str, int] | None:
    if start >= len(text) or text[start] != opener:
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

        if char in ("'", '"'):
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1

    return None


def _parse_jsonish(text: str) -> Any | None:
    if not text:
        return None

    candidates = [text]
    repaired_keys = re.sub(r"(?<=[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r' "\1":', text)
    if repaired_keys != text:
        candidates.append(repaired_keys)

    generally_repaired = _repair_json(text)
    if generally_repaired is not None:
        candidates.append(generally_repaired)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _repair_json(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None

    repaired = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    repaired = _RE_SINGLE_QUOTED_KEY.sub(r'"\1":', repaired)
    repaired = _RE_TRAILING_COMMA.sub(r"\1", repaired)

    for candidate in (repaired, repaired + "}", repaired + "]", repaired + "]}", repaired + "}}"):
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    return None


def _iter_json_action_sources(content: str) -> Iterable[str]:
    code_spans: list[tuple[int, int]] = []
    for match in _RE_CODE_BLOCK.finditer(content):
        code_spans.append(match.span())
        raw = match.group(1).strip()
        if '"actions"' in raw or "'actions'" in raw:
            yield raw

    raw_content = _replace_spans(content, code_spans)
    for raw in _iter_balanced_json_objects(raw_content):
        if '"actions"' in raw or "'actions'" in raw:
            yield raw


def _replace_spans(content: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return content
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(content[cursor:start])
        pieces.append(" " * (end - start))
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces)


def _iter_balanced_json_objects(content: str) -> Iterable[str]:
    index = 0
    while index < len(content):
        start = content.find("{", index)
        if start == -1:
            break
        result = _extract_balanced(content, start, "{", "}")
        if result is None:
            break
        _, end = result
        yield content[start:end]
        index = end


def _extract_actions_from_json(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("actions"), dict):
        return []

    results: list[dict[str, Any]] = []
    for action_key, nodes in data["actions"].items():
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            tool_call = _convert_json_node_to_tool_call(node, str(action_key))
            if tool_call is not None:
                results.append(tool_call)
    return results


def _convert_json_node_to_tool_call(
    node: Any,
    action_key: str,
) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None

    element_key = node.get("elementKey") or node.get("element")
    if not element_key:
        return None

    common_args = {
        "element": element_key,
        "params": node.get("paramsValue", {}),
        "actionKey": action_key,
        "title": node.get("title", ""),
    }
    if node.get("insertAfterKey") or node.get("anchorKey"):
        return {
            "name": "insert_node",
            "arguments": {
                **common_args,
                "insertAfterKey": node.get("insertAfterKey") or node.get("anchorKey"),
                "position": node.get("position", "after"),
            },
        }

    return {
        "name": "create_node",
        "arguments": {
            **common_args,
            "key": node.get("key"),
        },
    }


def _normalize_tool_call(tool_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if tool_name == "Question":
        return "ask_user", _transform_question_args(args)
    return tool_name, args


def _transform_question_args(args: dict[str, Any]) -> dict[str, Any]:
    questions = args.get("questions")
    if not isinstance(questions, list) or not questions or not isinstance(questions[0], dict):
        return args

    question = questions[0]
    normalized: dict[str, Any] = {
        "question": question.get("question", ""),
        "context": question.get("header", ""),
    }

    options = question.get("options", [])
    if isinstance(options, list) and options:
        normalized["suggestedOptions"] = [_normalize_question_option(option) for option in options]

    return normalized


def _normalize_question_option(option: Any) -> str:
    if not isinstance(option, dict):
        return str(option)

    label = str(option.get("label", ""))
    description = str(option.get("description", ""))
    if not description:
        return label
    return f"{label}\uff08{description}\uff09"
