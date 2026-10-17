from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KnowledgeBase(Strict):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class Search(Strict):
    knowledge_base_id: UUID
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)


class Chat(Strict):
    knowledge_base_id: UUID
    conversation_id: UUID | None = None
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)


class AgentSearch(Strict):
    knowledge_base_id: UUID
    query: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=5, ge=1, le=20)
    max_snippet_chars: int = Field(default=400, ge=200, le=4000)
    result_version: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


class ModelSettings(Strict):
    chat_model: str = Field(min_length=1, max_length=100)
    vision_model: str = Field(min_length=1, max_length=100)
    context_length: int = Field(ge=4096, le=8192)
    ocr: bool = True
    caption: bool = True


class Rebuild(Strict):
    model: str = Field(min_length=1, max_length=100)
    confirm: Literal[True]


class Activate(Strict):
    version_id: UUID
    confirm: Literal[True]
