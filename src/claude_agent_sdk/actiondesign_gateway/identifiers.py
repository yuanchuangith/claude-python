"""Canonical identifiers shared by ActionDesign gateway boundaries."""

from __future__ import annotations

import re
from uuid import UUID


CONVERSATION_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
RUN_ID_PATTERN = rf"^run_{CONVERSATION_ID_PATTERN.removeprefix('^').removesuffix('$')}$"
_CONVERSATION_ID_RE = re.compile(CONVERSATION_ID_PATTERN)
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


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
