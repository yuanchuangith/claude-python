"""Canonical identifiers shared by ActionDesign gateway boundaries."""

from __future__ import annotations

import re
from uuid import UUID

CONVERSATION_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
RUN_ID_PATTERN = rf"^run_{CONVERSATION_ID_PATTERN.removeprefix('^').removesuffix('$')}$"
HISTORY_CONVERSATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
SAFE_REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
UUID4_ID_PATTERN = CONVERSATION_ID_PATTERN
_CONVERSATION_ID_RE = re.compile(CONVERSATION_ID_PATTERN)
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)
_HISTORY_CONVERSATION_ID_RE = re.compile(HISTORY_CONVERSATION_ID_PATTERN)
_SAFE_REQUEST_ID_RE = re.compile(SAFE_REQUEST_ID_PATTERN)


def validate_conversation_uuid4(value: str) -> str:
    """Return a canonical lowercase UUID v4 string or raise ``ValueError``."""

    if not isinstance(value, str) or not _CONVERSATION_ID_RE.fullmatch(value):
        raise ValueError("conversationId must be a canonical UUID v4")
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("conversationId must be a canonical UUID v4")
    return value


def validate_run_id(value: str) -> str:
    """Return a canonical ``run_<uuid-v4>`` identifier or raise ``ValueError``."""

    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise ValueError("runId must use the canonical run_<uuid-v4> format")
    validate_conversation_uuid4(value.removeprefix("run_"))
    return value


def validate_history_conversation_id(value: str) -> str:
    """Accept canonical runtime IDs and bounded path-safe legacy IDs."""

    try:
        return validate_conversation_uuid4(value)
    except ValueError:
        if not isinstance(value, str) or not _HISTORY_CONVERSATION_ID_RE.fullmatch(value):
            raise ValueError("conversationId must be canonical or a safe legacy identifier")
    return value


def validate_safe_request_id(value: str) -> str:
    """Return a bounded opaque HTTP request identifier or raise ``ValueError``."""

    if not isinstance(value, str) or not _SAFE_REQUEST_ID_RE.fullmatch(value):
        raise ValueError("requestId must be a bounded safe opaque identifier")
    return value


def validate_uuid4_id(value: str, *, label: str = "identifier") -> str:
    """Return a canonical lowercase UUID v4 identifier."""

    try:
        return validate_conversation_uuid4(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID v4") from exc


def validate_history_run_id(value: str) -> str:
    """Accept canonical runtime run IDs and bounded legacy request-log IDs."""

    try:
        return validate_run_id(value)
    except ValueError:
        return validate_safe_request_id(value)
