from __future__ import annotations

import re
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
_NON_SECRET_TOKEN_KEYS = {
    "budget_tokens",
    "cached_tokens",
    "completion_tokens",
    "contextwindowtokens",
    "context_window_tokens",
    "input_tokens",
    "maxoutputtokens",
    "max_output_tokens",
    "maxtokens",
    "max_tokens",
    "output_tokens",
    "prompt_tokens",
    "reasoning_tokens",
    "total_tokens",
}
_NON_SECRET_STATUS_KEYS = {
    "keyconfigured",
    "keyupdated",
    "masked",
}
_CONNECTION_KEYS = {
    "connection_string",
    "config_path",
    "database",
    "dsn",
    "host",
    "hostname",
    "port",
    "user",
    "username",
}
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|cookie|api[-_]?key|secret|password|passwd|token|credential)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_PAIR_RE = re.compile(
    r"\b(authorization|cookie|api[-_]?key|secret|password|passwd|token|credential)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"(https?://|mysql://)([^/@\s:]+):([^/@\s]+)@", re.IGNORECASE)
_CONNECTION_PAIR_RE = re.compile(
    r"\b(host|hostname|user|username|database|dsn|connection(?:_string)?|config(?:_path)?|port)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\r\n\t,;]+")
_UNIX_SENSITIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|var|etc|opt|tmp)/[^\r\n\t,;]+")
_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+$"
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_REDACTED = "[REDACTED]"


def safe_conversation_id(value: str) -> str:
    if not _CONVERSATION_ID_RE.fullmatch(value):
        raise HTTPException(
            status_code=422,
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
    if isinstance(value, str):
        if _is_base64_or_image_data(value):
            return _REDACTED
        return _redact_string(value)
    return value


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower()
    if normalized in _NON_SECRET_TOKEN_KEYS or normalized in _NON_SECRET_STATUS_KEYS:
        return False
    return (
        normalized in _SECRET_KEYS
        or normalized in _CONNECTION_KEYS
        or _SECRET_KEY_RE.search(normalized) is not None
    )


def _redact_string(value: str) -> str:
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", value)
    redacted = _SECRET_PAIR_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _CONNECTION_PAIR_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _URL_USERINFO_RE.sub(r"\1[REDACTED]:[REDACTED]@", redacted)
    redacted = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", redacted)
    return _UNIX_SENSITIVE_PATH_RE.sub("[REDACTED_PATH]", redacted)


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
