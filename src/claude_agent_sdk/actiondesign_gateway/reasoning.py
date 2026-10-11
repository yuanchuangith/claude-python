"""Bounded native reasoning state; protocol payloads are never inferred from text."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

if TYPE_CHECKING:
    from .models import AgentChatMessage, AgentChatRequest
    from .profile_store import ProviderProfile

MAX_REASONING_BYTES = 524_288


class _NativeState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ThinkingBlock(_NativeState):
    type: Literal["thinking"]
    thinking: str = Field(max_length=MAX_REASONING_BYTES)
    signature: str | None = Field(default=None, max_length=MAX_REASONING_BYTES)


class RedactedThinkingBlock(_NativeState):
    type: Literal["redacted_thinking"]
    data: str = Field(min_length=1, max_length=MAX_REASONING_BYTES)


class ReasoningSummary(_NativeState):
    type: Literal["summary_text"]
    text: str = Field(max_length=MAX_REASONING_BYTES)


class ResponsesReasoningItem(_NativeState):
    type: Literal["reasoning"]
    id: str | None = Field(default=None, min_length=1, max_length=256)
    summary: list[ReasoningSummary] = Field(max_length=128)
    encrypted_content: str | None = Field(
        default=None, min_length=1, max_length=MAX_REASONING_BYTES
    )


class ReasoningState(_NativeState):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    profile_id: str = Field(alias="profileId", max_length=256)
    model: str = Field(
        min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
    )

    @model_validator(mode="after")
    def _bounded(self) -> ReasoningState:
        size = len(
            json.dumps(
                self.model_dump(by_alias=True, exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if size > MAX_REASONING_BYTES:
            raise ValueError("Native reasoning state exceeds the 512 KiB limit")
        return self


class ClaudeReasoningState(ReasoningState):
    protocol: Literal["claude"]
    blocks: list[
        Annotated[ThinkingBlock | RedactedThinkingBlock, Field(discriminator="type")]
    ] = Field(min_length=1, max_length=128)


class OpenAIReasoningState(ReasoningState):
    protocol: Literal["openai"]
    reasoning_content: str = Field(
        alias="reasoningContent", min_length=1, max_length=MAX_REASONING_BYTES
    )


class ResponsesReasoningState(ReasoningState):
    protocol: Literal["openai-responses"]
    items: list[ResponsesReasoningItem] = Field(min_length=1, max_length=128)


NativeReasoningState = Annotated[
    ClaudeReasoningState | OpenAIReasoningState | ResponsesReasoningState,
    Field(discriminator="protocol"),
]
REASONING_ADAPTER = TypeAdapter(NativeReasoningState)


def reasoning_payload(
    provider: str, profile_id: str, model: str, protocol: str, **native: object
) -> dict[str, Any]:
    return REASONING_ADAPTER.validate_python(
        {
            "schemaVersion": 1,
            "provider": provider,
            "profileId": profile_id,
            "model": model,
            "protocol": protocol,
            **native,
        }
    ).model_dump(mode="json", by_alias=True, exclude_none=True)


def compatible_reasoning(
    message: AgentChatMessage, req: AgentChatRequest, profile: ProviderProfile
) -> NativeReasoningState | None:
    state = message.reasoning
    if state is None:
        return None
    if (
        state.provider != profile.provider
        or state.profile_id != profile.id
        or state.model != (req.model or profile.default_model)
        or state.protocol != profile.protocol
    ):
        return None
    return state
