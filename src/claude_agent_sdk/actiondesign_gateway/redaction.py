from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException

_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GRAPH_USER_MESSAGE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])run_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}:user:[0-9]+(?![A-Za-z0-9_:.-])",
    re.IGNORECASE,
)
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
    "availableinputtokens",
    "budget_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cachecreationtokens",
    "cachedinputtokens",
    "cached_tokens",
    "completion_tokens",
    "completion_tokens_details",
    "contextwindowtokens",
    "context_window_tokens",
    "input_tokens",
    "input_tokens_details",
    "inputtokens",
    "estimatedinputtokens",
    "maxoutputtokens",
    "max_output_tokens",
    "maxtokens",
    "max_tokens",
    "output_tokens",
    "output_tokens_details",
    "outputtokens",
    "prompt_tokens",
    "prompt_tokens_details",
    "reasoning_tokens",
    "reasoningtokens",
    "reservedoutputtokens",
    "total_tokens",
    "totaltokens",
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
            key: _redact_native_reasoning(item) if key == "reasoning" and isinstance(item, Mapping) and item.get("schemaVersion") == 1
            else _REDACTED if _is_secret_key(key) else redact_value(item)
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


def _redact_native_reasoning(value: Mapping) -> Any:
    # Only strict, bounded protocol state may preserve opaque signatures/ciphertext.
    # Signed plaintext must remain exact; if it contains sensitive material, omit
    # the state instead of returning a modified block that cannot be replayed.
    from .reasoning import REASONING_ADAPTER
    try:
        state = REASONING_ADAPTER.validate_python(dict(value))
    except ValueError:
        return None
    native = state.model_dump(mode="json", by_alias=True, exclude_none=True)
    texts: list[str] = []
    if state.protocol == "claude":
        texts = [block.thinking for block in state.blocks if block.type == "thinking"]
    elif state.protocol == "openai":
        texts = [state.reasoning_content]
    else:
        texts = [summary.text for item in state.items for summary in item.summary]
    if any(_redact_string(text) != text for text in texts):
        return None
    return native


def redact_secret_values(value: Any, secret_values: Sequence[str]) -> Any:
    """Redact structured secret fields and exact configured Secret values."""

    filtered = tuple(
        sorted(
            {secret for secret in secret_values if secret},
            key=len,
            reverse=True,
        )
    )
    return _replace_secret_values(redact_value(value), filtered)


def _replace_secret_values(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: None if key == "reasoning" and isinstance(item, Mapping)
            and any(secret in str(item) for secret in secret_values)
            else item if key == "reasoning" and isinstance(item, Mapping) and item.get("schemaVersion") == 1
            else _replace_secret_values(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_replace_secret_values(item, secret_values) for item in value)
    if _is_non_string_sequence(value):
        return [_replace_secret_values(item, secret_values) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            result = result.replace(secret, _REDACTED)
        return result
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
    # Only canonical UUID/numeric Graph identities are protected. Their user:0
    # suffix is provenance, not a connection credential (including in JSON text).
    placeholder = "__GRAPH_MESSAGE_ID__"
    while placeholder in value:
        placeholder += "_"
    identities: list[str] = []
    def protect_identity(match: re.Match[str]) -> str:
        identities.append(match.group(0))
        return f"{placeholder}{len(identities) - 1}__"
    redacted = _GRAPH_USER_MESSAGE_ID_RE.sub(protect_identity, value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_PAIR_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _CONNECTION_PAIR_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _URL_USERINFO_RE.sub(r"\1[REDACTED]:[REDACTED]@", redacted)
    redacted = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = _UNIX_SENSITIVE_PATH_RE.sub("[REDACTED_PATH]", redacted)
    for index, identity in enumerate(identities):
        redacted = redacted.replace(f"{placeholder}{index}__", identity)
    return redacted


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
