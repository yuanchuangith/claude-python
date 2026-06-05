"""Pydantic models shared by the ActionDesign gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

try:
    from pydantic import ConfigDict
except ImportError:  # pragma: no cover - pydantic v1
    ConfigDict = None  # type: ignore[assignment]


DESIGN_TOOLS = frozenset(
    {
        "list_elements",
        "get_element_detail",
        "get_component_methods",
        "get_page_actions",
        "propose_plan",
        "ask_user",
        "enter_plan_mode",
        "exit_plan_mode",
        "create_action",
        "create_node",
        "insert_node",
        "delete_node",
        "preview_code",
    }
)

MIMO_MODELS: dict[str, dict[str, Any]] = {
    "mimo-v2.5": {
        "name": "MiMo v2.5",
        "provider": "mimo",
        "supportsImages": True,
    },
    "mimo-v2.5-pro": {
        "name": "MiMo v2.5 Pro",
        "provider": "mimo",
        "supportsImages": False,
    },
}

MIMO_IMAGE_MODELS = frozenset(
    model_name
    for model_name, metadata in MIMO_MODELS.items()
    if metadata.get("supportsImages")
)


class _GatewayBaseModel(BaseModel):
    if ConfigDict is not None:
        model_config = ConfigDict(populate_by_name=True)
    else:

        class Config:
            allow_population_by_field_name = True


class ImageInput(_GatewayBaseModel):
    """Image input supplied as base64 data or a URL."""

    media_type: str = "image/png"
    data: str = ""
    url: str = ""


class AgentChatRequest(_GatewayBaseModel):
    """Chat request accepted by the ActionDesign gateway."""

    provider: Literal["mimo", "claude-code", "auto"] = "mimo"
    model: str = ""
    conversation_id: str = Field(default="", alias="conversationId")
    prompt: str
    stream: bool = False
    tool_names: list[str] = Field(default_factory=list, alias="toolNames")
    images: list[ImageInput] = Field(default_factory=list)
    max_tokens: int = Field(default=8192, alias="maxTokens")
    thinking: dict[str, Any] = Field(
        default_factory=lambda: {"type": "disabled"}
    )
    project_path: str = Field(
        default="src/core/common/ActionDesign",
        alias="projectPath",
    )


class AgentChatResponse(_GatewayBaseModel):
    """Chat response returned by the ActionDesign gateway."""

    provider: Literal["mimo", "claude-code"]
    model: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    success: bool = True
    error: str | None = None
    code: str | None = None
    duration_ms: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)


class ToolResultRequest(_GatewayBaseModel):
    """Tool execution result posted back by the ActionDesign client."""

    conversation_id: str = Field(default="", alias="conversationId")
    run_id: str = Field(alias="runId")
    turn: int = 0
    tool_call_id: str = Field(alias="toolCallId")
    tool_name: str = Field(alias="toolName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "failed"] = "success"
    result: Any = None
    error: str = ""
    timestamp: str = ""


class ToolResultResponse(_GatewayBaseModel):
    """Acknowledgement for a posted tool result."""

    success: bool
    message: str
    duplicate: bool = False
