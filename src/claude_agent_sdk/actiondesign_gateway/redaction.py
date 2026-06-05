from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException

_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_KEYS = {
    "authorization",
    "api-key",
    "apikey",
    "api_key",
    "cookie",
    "gxp_mimo_api_key",
    "model_mimo_key",
}
_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+$"
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_REDACTED = "[REDACTED]"


def safe_conversation_id(value: str) -> str:
    if value == "":
        return f"unknown_{int(time.time() * 1000)}"
    if not _CONVERSATION_ID_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_CONVERSATION_ID"},
        )
    return value


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_secret_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if _is_non_string_sequence(value):
        return [redact_value(item) for item in value]
    if isinstance(value, str) and _is_base64_or_image_data(value):
        return _REDACTED
    return value


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower()
    return normalized in _SECRET_KEYS


def _is_non_string_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _is_base64_or_image_data(value: str) -> bool:
    stripped = value.strip()
    if _IMAGE_DATA_URL_RE.fullmatch(stripped):
        return True
    if len(stripped) < 128 or len(stripped) % 4 != 0:
        return False
    return _BASE64_RE.fullmatch(stripped) is not None
