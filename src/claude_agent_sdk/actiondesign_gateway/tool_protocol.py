"""ActionDesign tool-call protocol parsing helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from html import unescape
from typing import Any

_RE_TOOL_CALL = re.compile(r"\[(?:TOOL_CALL|TOL_CALL)\]\s*(\w+)\s*\(", re.DOTALL)
_RE_XML_TOOL_CALL = re.compile(r"<tool_call\b[^>]*>.*?</tool_call\s*>", re.DOTALL)
_RE_XML_FUNCTION = re.compile(
    r"<function=([A-Za-z_][\w]*)\s*>(.*?)</function\s*>",
    re.DOTALL,
)
_RE_XML_PARAMETER = re.compile(
    r"<parameter=([A-Za-z_][\w]*)\s*>(.*?)</parameter\s*>",
    re.DOTALL,
)
_RE_CODE_BLOCK = re.compile(r"```(?:jsonc?|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)
_RE_DATA_URL = re.compile(r"^data:image/\w+;base64,", re.IGNORECASE)
_RE_TRAILING_COMMA = re.compile(r",\s*([\]}])")
_RE_SINGLE_QUOTED_KEY = re.compile(r"'([^']*)'\s*:")

_TEXT_MARKERS = ("[TOOL_CALL]", "[TOL_CALL]")
_XML_TOOL_CALL_MARKER = "<tool_call>"
_XML_TOOL_CALL_START = "<tool_call"
_MARKERS = (*_TEXT_MARKERS, _XML_TOOL_CALL_MARKER)


def normalize_image_data(data: str) -> str:
    """Remove a base64 image data URL prefix."""
    return _RE_DATA_URL.sub("", data)


def extract_tool_calls(content: str) -> list[dict[str, Any]]:
    """Extract ActionDesign tool calls from protocol text or JSON actions."""
    normalized = content.replace("[TOL_CALL]", "[TOOL_CALL]")
    indexed_calls: list[tuple[int, dict[str, Any]]] = []

    for match in _RE_TOOL_CALL.finditer(normalized):
        result = _extract_balanced(normalized, match.end() - 1, "(", ")")
        if result is None:
            continue
        args_text, _ = result
        args = _parse_jsonish(args_text.strip())
        if args is None:
            continue
        name, args = _normalize_tool_call(match.group(1), args)
        indexed_calls.append((match.start(), {"name": name, "arguments": args}))

    for match in _RE_XML_TOOL_CALL.finditer(normalized):
        call = _extract_xml_tool_call(match.group(0))
        if call is not None:
            indexed_calls.append((match.start(), call))

    tool_calls = [call for _, call in sorted(indexed_calls, key=lambda item: item[0])]

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

    for start, end in _tool_protocol_spans(normalized):
        pieces.append(normalized[cursor:start])
        cursor = end

    pieces.append(normalized[cursor:])
    return "".join(pieces)


def _tool_protocol_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []

    for match in _RE_TOOL_CALL.finditer(content):
        result = _extract_balanced(content, match.end() - 1, "(", ")")
        if result is None:
            continue
        spans.append((match.start(), result[1]))

    spans.extend(match.span() for match in _RE_XML_TOOL_CALL.finditer(content))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start >= merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


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
        marker_index = _next_marker_index(pending)
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
            end = (
                _find_xml_tool_call_end(pending)
                if _is_xml_marker(marker)
                else _find_tool_call_end(pending)
            )
            if end is None:
                break
            parsed = extract_tool_calls(pending[:end])
            tool_calls.extend(_filter_allowed(parsed, allowed_tools))
            pending = pending[end:]
            continue

        if _is_marker_prefix(pending):
            break

        next_marker = _next_marker_index(pending, start=1)
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

    all_parsed = extract_tool_calls(pending)
    parsed = _filter_allowed(all_parsed, allowed_tools)
    if parsed:
        return None, parsed
    if all_parsed or _contains_tool_protocol_start_or_prefix(pending):
        cleaned = clean_tool_protocol_text(pending)
        if cleaned == pending:
            cleaned = _strip_from_first_tool_protocol_start(pending)
        return (cleaned or None), []
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


def _is_xml_marker(marker: str) -> bool:
    return marker == _XML_TOOL_CALL_MARKER


def _next_marker_index(text: str, start: int = 0) -> int:
    indexes = [
        index
        for index in (text.find("[", start), text.find("<", start))
        if index != -1
    ]
    return min(indexes) if indexes else -1


def _find_tool_call_end(text: str) -> int | None:
    paren_start = text.find("(")
    if paren_start == -1:
        return None
    result = _extract_balanced(text, paren_start, "(", ")")
    if result is None:
        return None
    return result[1]


def _find_xml_tool_call_end(text: str) -> int | None:
    end_marker = "</tool_call>"
    end = text.find(end_marker)
    if end == -1:
        return None
    return end + len(end_marker)


def _contains_tool_protocol_start_or_prefix(text: str) -> bool:
    if any(marker in text for marker in (*_TEXT_MARKERS, _XML_TOOL_CALL_START)):
        return True
    return any(_is_marker_prefix(text[index:]) for index in range(len(text)))


def _strip_from_first_tool_protocol_start(text: str) -> str:
    indexes = [
        index
        for marker in (*_TEXT_MARKERS, _XML_TOOL_CALL_START)
        if (index := text.find(marker)) != -1
    ]
    indexes.extend(
        index
        for index in range(len(text))
        if any(marker.startswith(text[index:]) for marker in _MARKERS)
    )
    if not indexes:
        return text
    return text[: min(indexes)]


def _extract_xml_tool_call(block: str) -> dict[str, Any] | None:
    function_match = _RE_XML_FUNCTION.search(block)
    if function_match is None:
        return None

    args: dict[str, Any] = {}
    for parameter_match in _RE_XML_PARAMETER.finditer(function_match.group(2)):
        args[parameter_match.group(1)] = _parse_xml_parameter_value(
            parameter_match.group(2)
        )

    name, args = _normalize_tool_call(function_match.group(1), args)
    return {"name": name, "arguments": args}


def _parse_xml_parameter_value(raw_value: str) -> Any:
    value = unescape(raw_value.strip())
    parsed = _parse_jsonish(value)
    return value if parsed is None else parsed


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
