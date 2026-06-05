from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .actiondesign_backend_executor import warm_knowledge_index
from .claude_code_provider import call_claude_code, stream_claude_code
from .mimo_provider import call_mimo, stream_mimo
from .models import MIMO_MODELS, AgentChatRequest, AgentChatResponse, ToolResultRequest
from .qdrant_knowledge_store import qdrant_knowledge_store_from_settings
from .redaction import safe_conversation_id
from .session_log import ToolResultStore
from .settings import Settings


def _allow_origins(value: str | Sequence[str]) -> list[str]:
    if not isinstance(value, str):
        return list(value)
    if value == "*":
        return ["*"]
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def create_app(settings: Settings | None = None) -> FastAPI:
    gateway_settings = settings or Settings()
    tool_results = ToolResultStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            warm_knowledge_index(gateway_settings)
        except Exception as exc:
            app.state.knowledge_index_error = str(exc)
        yield

    app = FastAPI(
        title="ActionDesign Agent Gateway",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.actiondesign_settings = gateway_settings
    app.state.tool_results = tool_results
    app.state.knowledge_index_error = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allow_origins(gateway_settings.allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/actiondesign-agent/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/actiondesign-agent/models")
    async def get_models() -> dict:
        mimo_models = [
            {
                "id": model_id,
                "name": config["name"],
                "provider": "mimo",
                "supportsImages": config["supportsImages"],
                "supportsTextToolProtocol": True,
                "supportsNativeTools": False,
                "supportsThinking": True,
            }
            for model_id, config in MIMO_MODELS.items()
        ]
        mimo = {
            "status": "ready" if gateway_settings.mimo_api_key else "unavailable",
            "defaultModel": gateway_settings.mimo_default_model,
            "models": mimo_models,
        }
        if not gateway_settings.mimo_api_key:
            mimo["error"] = "Missing GXP_MIMO_API_KEY or MODEL_MIMO_KEY"

        claude_code = {
            "status": "ready"
            if gateway_settings.claude_code_enabled
            else "unavailable",
            "defaultModel": gateway_settings.claude_code_default_model,
            "models": [],
        }
        if not gateway_settings.claude_code_enabled:
            claude_code["error"] = "CLAUDE_CODE_PROVIDER_ENABLED=false"

        return {
            "defaultProvider": gateway_settings.default_provider,
            "defaultModel": gateway_settings.mimo_default_model,
            "providers": {
                "mimo": mimo,
                "claude-code": claude_code,
            },
        }

    @app.post("/api/actiondesign-agent/knowledge/upload")
    async def upload_knowledge(
        request: Request,
        files: Annotated[list[UploadFile], File(...)],
        paths: Annotated[list[str] | None, Form()] = None,
    ) -> dict:
        _require_knowledge_admin(request, gateway_settings)
        store = _knowledge_store_or_503(gateway_settings)
        documents = []
        seen_paths: set[str] = set()
        if paths is not None and len(paths) != len(files):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "KNOWLEDGE_PATH_COUNT_MISMATCH",
                    "message": "paths count must match files count",
                },
            )
        for index, uploaded in enumerate(files):
            path = _upload_path(uploaded, paths[index] if paths else None)
            if path in seen_paths:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "KNOWLEDGE_DUPLICATE_PATH",
                        "message": f"Duplicate knowledge path: {path}",
                    },
                )
            seen_paths.add(path)
            content = _decode_knowledge_content(await uploaded.read())
            if not content.strip():
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "KNOWLEDGE_EMPTY_FILE",
                        "message": f"Knowledge file is empty: {path}",
                    },
                )
            documents.append({"path": path, "content": content})
        result = store.replace_documents(documents)
        return {"success": True, **result}

    @app.post("/api/actiondesign-agent/knowledge/search")
    async def search_knowledge(payload: dict) -> dict:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "KNOWLEDGE_QUERY_REQUIRED",
                    "message": "knowledge search requires query",
                },
            )
        store = _knowledge_store_or_503(gateway_settings)
        results = store.search(
            query,
            limit=int(payload.get("limit") or gateway_settings.knowledge_max_results),
            max_chars_per_item=gateway_settings.knowledge_max_chars_per_item,
            max_context_chars=gateway_settings.knowledge_max_context_chars,
        )
        return {"query": query, "results": results}

    @app.post("/api/actiondesign-agent/knowledge/read")
    async def read_knowledge(payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "KNOWLEDGE_PATH_REQUIRED",
                    "message": "knowledge read requires path",
                },
            )
        store = _knowledge_store_or_503(gateway_settings)
        try:
            return store.read(
                path=path,
                heading=str(payload.get("heading") or "").strip(),
                max_chars=int(
                    payload.get("maxChars")
                    or payload.get("max_chars")
                    or gateway_settings.knowledge_max_chars_per_item
                ),
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "KNOWLEDGE_NOT_FOUND",
                    "message": f"Knowledge file not found: {path}",
                },
            ) from exc

    @app.post(
        "/api/actiondesign-agent/mimo/chat",
        response_model=AgentChatResponse,
    )
    async def mimo_chat(req: AgentChatRequest, request: Request) -> AgentChatResponse:
        return await _chat(req, request, "mimo")

    @app.post(
        "/api/actiondesign-agent/claude-code/chat",
        response_model=AgentChatResponse,
    )
    async def claude_code_chat(
        req: AgentChatRequest, request: Request
    ) -> AgentChatResponse:
        return await _chat(req, request, "claude-code")

    @app.post("/api/actiondesign-agent/mimo/chat/stream")
    async def mimo_chat_stream(
        req: AgentChatRequest, request: Request
    ) -> StreamingResponse:
        return _chat_stream(req, request, "mimo")

    @app.post("/api/actiondesign-agent/claude-code/chat/stream")
    async def claude_code_chat_stream(
        req: AgentChatRequest, request: Request
    ) -> StreamingResponse:
        return _chat_stream(req, request, "claude-code")

    @app.post("/api/actiondesign-agent/tool-result")
    async def receive_tool_result(req: ToolResultRequest, request: Request) -> dict:
        conversation_id = (
            request.headers.get("X-Conversation-Id") or req.conversation_id
        )
        safe_conversation_id(conversation_id)
        duplicate = tool_results.store(gateway_settings, conversation_id, req)
        return {"success": True, "message": "received", "duplicate": duplicate}

    @app.get("/api/actiondesign-agent/tool-results/{conversation_id}/{run_id}")
    async def get_tool_results(conversation_id: str, run_id: str) -> dict:
        safe_conversation_id(conversation_id)
        return {
            "conversationId": conversation_id,
            "runId": run_id,
            "results": tool_results.get(conversation_id, run_id),
        }

    async def _chat(
        req: AgentChatRequest,
        request: Request,
        provider: Literal["mimo", "claude-code"],
    ) -> AgentChatResponse:
        req.conversation_id = (
            request.headers.get("X-Conversation-Id") or req.conversation_id
        )
        safe_conversation_id(req.conversation_id)
        if provider == "mimo":
            return await call_mimo(req, gateway_settings)
        return await call_claude_code(req, gateway_settings)

    def _chat_stream(
        req: AgentChatRequest,
        request: Request,
        provider: Literal["mimo", "claude-code"],
    ) -> StreamingResponse:
        req.conversation_id = (
            request.headers.get("X-Conversation-Id") or req.conversation_id
        )
        safe_conversation_id(req.conversation_id)
        generator = (
            stream_mimo(req, gateway_settings)
            if provider == "mimo"
            else stream_claude_code(req, gateway_settings)
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _require_knowledge_admin(request: Request, settings: Settings) -> None:
    token = settings.knowledge_admin_token
    authorization = request.headers.get("Authorization", "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "KNOWLEDGE_ADMIN_TOKEN_NOT_CONFIGURED",
                "message": "Set ACTIONDESIGN_KNOWLEDGE_ADMIN_TOKEN",
            },
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "KNOWLEDGE_ADMIN_AUTH_REQUIRED",
                "message": "Knowledge upload requires bearer admin token",
            },
        )
    if authorization.removeprefix("Bearer ").strip() != token:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "KNOWLEDGE_ADMIN_AUTH_INVALID",
                "message": "Knowledge admin token is invalid",
            },
        )


def _knowledge_store_or_503(settings: Settings):
    store = qdrant_knowledge_store_from_settings(settings)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "KNOWLEDGE_STORE_NOT_CONFIGURED",
                "message": "Set ACTIONDESIGN_QDRANT_URL and embedding settings",
            },
        )
    return store


def _upload_path(uploaded: UploadFile, path_override: str | None = None) -> str:
    filename = (path_override or uploaded.filename or "").replace("\\", "/").lstrip("/")
    if not filename or filename.endswith("/") or not filename.lower().endswith(".md"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "KNOWLEDGE_FILE_INVALID",
                "message": "Knowledge upload only accepts Markdown files",
            },
        )
    if any(part in {"", ".", ".."} for part in filename.split("/")):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "KNOWLEDGE_PATH_FORBIDDEN",
                "message": "Knowledge upload path is not safe",
            },
        )
    return filename


def _decode_knowledge_content(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


app = create_app()
