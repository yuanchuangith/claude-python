"""Protocol-aware estimates over the same bodies sent to providers. No business state."""
from __future__ import annotations

import base64
import math
import struct
from typing import Any

from fastapi import HTTPException


def _image_tokens(value: dict[str, Any], protocol: str) -> int:
    """Image estimates depend on wire protocol and dimensions, never base64 length."""
    source = value.get("source") or value.get("image_url") or {}
    if isinstance(source, str):
        source = {"url": source}
    if source.get("detail", value.get("detail")) == "low" and protocol != "claude":
        return 85
    encoded = source.get("data") or str(source.get("url") or "").split(",", 1)[-1]
    try:
        raw = base64.b64decode(encoded[:65536], validate=True)
        width = height = 0
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
            width, height = struct.unpack(">II", raw[16:24])
        elif raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
            width, height = struct.unpack("<HH", raw[6:10])
        elif raw.startswith(b"\xff\xd8"):
            offset = 2
            while offset + 9 < len(raw):
                if raw[offset] != 255:
                    break
                marker = raw[offset + 1]
                length = int.from_bytes(raw[offset + 2:offset + 4], "big")
                if marker in {0xC0, 0xC1, 0xC2, 0xC3}:
                    height, width = struct.unpack(">HH", raw[offset + 5:offset + 9])
                    break
                offset += 2 + max(2, length)
        if not width or not height:
            return 4096
        if protocol == "claude":
            scale = min(1, 1568 / max(width, height), math.sqrt(1150000 / (width * height)))
            return math.ceil(width * height * scale * scale / 750)
        # Tile-based estimate with headroom for model-specific image accounting.
        scale = min(1, 2048 / max(width, height))
        width, height = width * scale, height * scale
        scale = 768 / min(width, height)
        return max(4096, 85 + 170 * math.ceil(width * scale / 512) * math.ceil(height * scale / 512))
    except (ValueError, TypeError, struct.error):
        return 4096


def _tokens(value: Any, protocol: str = "claude") -> int:
    """Ordinary data, including tool arguments and Schema examples, is fully counted."""
    if isinstance(value, str):
        ascii_count = sum(ord(c) < 128 for c in value)
        return math.ceil(ascii_count / 4 + (len(value) - ascii_count) / 1.6)
    if isinstance(value, list):
        return sum(_tokens(item, protocol) + 4 for item in value)
    if isinstance(value, dict):
        return sum(_tokens(k, protocol) + _tokens(v, protocol) + 4 for k, v in value.items())
    return 1


def _native_block_tokens(value: Any, protocol: str) -> int:
    """Special accounting only at a provider-native block boundary, never recursively."""
    if not isinstance(value, dict):
        return _tokens(value, protocol)
    opaque_types = {"redacted_thinking", "thinking"} if protocol == "claude" else {"reasoning"} if protocol == "openai-responses" else set()
    if value.get("type") in opaque_types:
        return sum(_tokens(k, protocol) + _tokens(v, protocol) + 4 for k, v in value.items()
                   if k not in {"data", "encrypted_content", "signature"})
    image_type = {"claude": "image", "openai": "image_url", "openai-responses": "input_image"}[protocol]
    if value.get("type") == image_type:
        return _image_tokens(value, protocol)
    return _tokens(value, protocol)


def _message_tokens(value: Any, protocol: str) -> int:
    if not isinstance(value, dict):
        return _tokens(value, protocol)
    return sum(_tokens(k, protocol) + (sum(_native_block_tokens(block, protocol) + 4 for block in v)
               if k == "content" and isinstance(v, list) else _tokens(v, protocol)) + 4
               for k, v in value.items())


def _provider_input_tokens(fields: dict[str, Any], protocol: str) -> int:
    total = 0
    for key, value in fields.items():
        if key in {"messages", "input"} and isinstance(value, list):
            count = sum((_native_block_tokens(item, protocol) if key == "input" and isinstance(item, dict)
                         and item.get("type") == "reasoning" else _message_tokens(item, protocol)) + 4 for item in value)
        else:
            count = _tokens(value, protocol)
        total += _tokens(key, protocol) + count + 4
    return total


def prepare_provider_body(req: Any, profile: Any, stream: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    from .provider_http import openai_request_body, request_body
    from .provider_responses import responses_request_body
    builder = {"claude": request_body, "openai": openai_request_body,
               "openai-responses": responses_request_body}[profile.protocol]
    body = builder(req, profile, stream)
    from .models import ApiProfileModel
    routed_model = body["model"]
    model = next((item for item in profile.models if item.id == routed_model), None)
    if model is None:
        model = ApiProfileModel(id=routed_model)
    output = min(req.max_tokens, model.max_output_tokens)
    output_field = "max_output_tokens" if profile.protocol == "openai-responses" else "max_tokens"
    body[output_field] = output
    if (profile.protocol == "claude" and body.get("thinking", {}).get("type") == "enabled"
            and body["thinking"]["budget_tokens"] >= output):
        raise HTTPException(400, detail={"code": "MODEL_OUTPUT_BUDGET_INVALID", "message": "Thinking budget exceeds routed output limit", "retryable": False})
    fields = {key: body[key] for key in ("system", "instructions", "messages", "input", "tools", "tool_choice") if key in body}
    estimated = _provider_input_tokens(fields, profile.protocol) + 64
    headroom = max(256, math.ceil(model.context_window_tokens * .03))
    available = max(0, model.context_window_tokens - output - headroom)
    budget = {"estimatedInputTokens": estimated, "availableInputTokens": available,
              "reservedOutputTokens": output, "contextWindowTokens": model.context_window_tokens,
              "isEstimate": True, "provider": profile.provider, "profileId": profile.id,
              "model": routed_model, "protocol": profile.protocol}
    if estimated >= available:
        raise HTTPException(413, detail={"code": "CONTEXT_BUDGET_EXCEEDED",
            "message": "Complete provider request exceeds the routed model input budget",
            "retryable": False, "budget": budget})
    return body, budget


def normalize_usage(usage: dict[str, Any], protocol: str) -> dict[str, int | None]:
    def number(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    inp = number(usage.get("prompt_tokens") if protocol == "openai" else usage.get("input_tokens"))
    out = number(usage.get("completion_tokens") if protocol == "openai" else usage.get("output_tokens"))
    cached = number((usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}).get("cached_tokens"))
    created = number(usage.get("cache_creation_input_tokens"))
    if protocol == "claude":
        cached = number(usage.get("cache_read_input_tokens"))
        if inp is not None:
            inp += (cached or 0) + (created or 0)
    reasoning = number((usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}).get("reasoning_tokens"))
    return {"inputTokens": inp, "outputTokens": out, "cachedInputTokens": cached,
            "cacheCreationTokens": created, "reasoningTokens": reasoning,
            "totalTokens": inp + out if inp is not None and out is not None else None}


def response_accounting(req: Any, profile: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if req.protocol_version != "1.1":
        return payload
    _, budget = prepare_provider_body(req, profile, req.stream)
    return {**payload, "normalizedUsage": normalize_usage(payload.get("usage") or {}, profile.protocol), "requestBudget": budget}
