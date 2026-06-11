from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .actiondesign_backend_executor import warm_knowledge_index
from .backend_tools import BackendToolCall, BackendToolResult, execute_backend_tool_calls
from .claude_code_provider import (
    call_claude_code,
    call_claude_code_review,
    stream_claude_code,
)
from .mimo_provider import call_mimo, call_mimo_review, stream_mimo
from .models import (
    MIMO_MODELS,
    AgentChatRequest,
    AgentChatResponse,
    CodeReviewRequest,
    CodeReviewResponse,
    ToolResultRequest,
)
from .qdrant_knowledge_store import qdrant_knowledge_store_from_settings
from .redaction import safe_conversation_id
from .session_log import (
    ToolResultStore,
    append_conversation_event,
    model_to_dict,
    require_run_id_for_full_log,
    resolve_conversation_id,
)
from .settings import Settings


def _allow_origins(value: str | Sequence[str]) -> list[str]:
    if not isinstance(value, str):
        return list(value)
    if value == "*":
        return ["*"]
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _claude_code_models(settings: Settings) -> list[dict]:
    configured_model_ids = [
        str(model).strip()
        for model in (getattr(settings, "claude_code_models", None) or [])
        if str(model).strip()
    ]
    if configured_model_ids:
        model_ids = configured_model_ids
    else:
        model_ids = list(MIMO_MODELS)
    default_model = str(
        getattr(settings, "claude_code_default_model", "") or ""
    ).strip()
    if default_model and default_model not in model_ids:
        model_ids = [default_model, *model_ids]
    models: list[dict] = []
    seen: set[str] = set()
    for model_id in model_ids:
        model_id = str(model_id).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        metadata = MIMO_MODELS.get(
            model_id,
            {
                "name": model_id,
                "supportsImages": False,
            },
        )
        models.append(
            {
                "id": model_id,
                "name": metadata["name"],
                "provider": "claude-code",
                "supportsImages": bool(metadata.get("supportsImages", False)),
                "supportsTextToolProtocol": True,
                "supportsNativeTools": True,
                "supportsThinking": False,
            }
        )
    return models


def _first_model_id(models: list[dict]) -> str:
    if not models:
        return ""
    return str(models[0].get("id") or "")


def _resolved_request_model(
    req: AgentChatRequest,
    settings: Settings,
    provider: Literal["mimo", "claude-code"],
) -> str:
    if req.model:
        return req.model
    if provider == "mimo":
        return settings.mimo_default_model or "mimo-v2.5"
    return (
        settings.claude_code_default_model
        or _first_model_id(_claude_code_models(settings))
        or settings.mimo_default_model
        or "mimo-v2.5"
    )


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
            "chatPath": "/api/actiondesign-agent/mimo/chat",
            "streamPath": "/api/actiondesign-agent/mimo/chat/stream",
            "supportsGenericChat": False,
            "models": mimo_models,
        }
        if not gateway_settings.mimo_api_key:
            mimo["error"] = "Missing GXP_MIMO_API_KEY or MODEL_MIMO_KEY"

        claude_code_models = _claude_code_models(gateway_settings)
        claude_code_default_model = (
            gateway_settings.claude_code_default_model
            or _first_model_id(claude_code_models)
        )
        claude_code = {
            "status": "ready"
            if gateway_settings.claude_code_enabled
            else "unavailable",
            "defaultModel": claude_code_default_model,
            "chatPath": "/api/actiondesign-agent/claude-code/chat",
            "streamPath": "/api/actiondesign-agent/claude-code/chat/stream",
            "supportsGenericChat": False,
            "models": claude_code_models,
        }
        if not gateway_settings.claude_code_enabled:
            claude_code["error"] = "CLAUDE_CODE_PROVIDER_ENABLED=false"

        return {
            "defaultProvider": gateway_settings.default_provider,
            "defaultModel": gateway_settings.mimo_default_model,
            "supportsGenericChat": False,
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
        result = await _execute_public_knowledge_tool(
            gateway_settings,
            BackendToolCall(
                name="knowledge.search",
                arguments={
                    "query": query,
                    "limit": payload.get("limit")
                    or gateway_settings.knowledge_max_results,
                    "maxCharsPerItem": payload.get("maxCharsPerItem")
                    or payload.get("max_chars_per_item")
                    or gateway_settings.knowledge_max_chars_per_item,
                    "maxContextChars": payload.get("maxContextChars")
                    or payload.get("max_context_chars")
                    or gateway_settings.knowledge_max_context_chars,
                },
            ),
        )
        return _public_knowledge_result_or_raise(result)

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
        result = await _execute_public_knowledge_tool(
            gateway_settings,
            BackendToolCall(
                name="knowledge.read",
                arguments={
                    "path": path,
                    "heading": str(payload.get("heading") or "").strip(),
                    "maxChars": payload.get("maxChars")
                    or payload.get("max_chars")
                    or gateway_settings.knowledge_max_chars_per_item,
                },
            ),
        )
        return _public_knowledge_result_or_raise(result)

    @app.post(
        "/api/actiondesign-agent/mimo/chat",
        response_model=AgentChatResponse,
    )
    async def mimo_chat(req: AgentChatRequest, request: Request) -> JSONResponse:
        return await _chat(req, request, "mimo")

    @app.post(
        "/api/actiondesign-agent/claude-code/chat",
        response_model=AgentChatResponse,
    )
    async def claude_code_chat(
        req: AgentChatRequest, request: Request
    ) -> JSONResponse:
        return await _chat(req, request, "claude-code")

    @app.post(
        "/api/actiondesign-agent/mimo/review",
        response_model=CodeReviewResponse,
    )
    async def mimo_review(
        req: CodeReviewRequest, request: Request
    ) -> JSONResponse:
        return await _review(req, request, "mimo")

    @app.post(
        "/api/actiondesign-agent/claude-code/review",
        response_model=CodeReviewResponse,
    )
    async def claude_code_review(
        req: CodeReviewRequest, request: Request
    ) -> JSONResponse:
        return await _review(req, request, "claude-code")

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
        conversation_id = resolve_conversation_id(request, req)
        req.conversation_id = conversation_id
        duplicate = tool_results.store(gateway_settings, conversation_id, req)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            {
                "type": "tool_result",
                "conversationId": conversation_id,
                "runId": req.run_id,
                "toolCallId": req.tool_call_id,
                "toolName": req.tool_name,
                "arguments": req.arguments,
                "status": req.status,
                "result": req.result,
                "error": req.error,
                "duplicate": duplicate,
            },
        )
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
    ) -> JSONResponse:
        started = time.time()
        conversation_id = resolve_conversation_id(request, req)
        req.conversation_id = conversation_id
        req.provider = provider
        req.model = _resolved_request_model(req, gateway_settings, provider)
        run_id = require_run_id_for_full_log(gateway_settings, req)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            _request_event(req, provider, run_id),
        )

        try:
            raw_response = (
                await call_mimo(req, gateway_settings)
                if provider == "mimo"
                else await call_claude_code(req, gateway_settings)
            )
        except Exception as exc:
            append_conversation_event(
                gateway_settings,
                conversation_id,
                _response_event(
                    _exception_response_payload(exc, provider, req.model),
                    conversation_id,
                    run_id,
                    started,
                ),
            )
            raise
        content = _agent_response_payload(raw_response)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            _response_event(content, conversation_id, run_id, started),
        )
        return JSONResponse(
            content=content,
            headers=_conversation_headers(conversation_id, run_id),
        )

    async def _review(
        req: CodeReviewRequest,
        request: Request,
        provider: Literal["mimo", "claude-code"],
    ) -> JSONResponse:
        started = time.time()
        conversation_id = resolve_conversation_id(request, req)
        req.conversation_id = conversation_id
        req.provider = provider
        req.model = _resolved_request_model(req, gateway_settings, provider)
        run_id = require_run_id_for_full_log(gateway_settings, req)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            _review_request_event(req, provider, run_id),
        )

        try:
            raw_response = (
                await call_mimo_review(req, gateway_settings)
                if provider == "mimo"
                else await call_claude_code_review(req, gateway_settings)
            )
        except Exception as exc:
            append_conversation_event(
                gateway_settings,
                conversation_id,
                _review_response_event(
                    _review_exception_response_payload(exc, provider, req.model),
                    conversation_id,
                    run_id,
                    started,
                ),
            )
            raise
        content = _code_review_response_payload(raw_response)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            _review_response_event(content, conversation_id, run_id, started),
        )
        return JSONResponse(
            content=content,
            headers=_conversation_headers(conversation_id, run_id),
        )

    def _chat_stream(
        req: AgentChatRequest,
        request: Request,
        provider: Literal["mimo", "claude-code"],
    ) -> StreamingResponse:
        started = time.time()
        conversation_id = resolve_conversation_id(request, req)
        req.conversation_id = conversation_id
        req.provider = provider
        req.model = _resolved_request_model(req, gateway_settings, provider)
        run_id = require_run_id_for_full_log(gateway_settings, req)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            _request_event(req, provider, run_id),
        )
        generator = (
            stream_mimo(req, gateway_settings)
            if provider == "mimo"
            else stream_claude_code(req, gateway_settings)
        )
        return StreamingResponse(
            _log_stream_response(
                generator,
                gateway_settings,
                conversation_id,
                run_id,
                started,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **_conversation_headers(conversation_id, run_id),
            },
        )

    return app


def _request_event(
    req: AgentChatRequest,
    provider: Literal["mimo", "claude-code"],
    run_id: str,
) -> dict:
    request_body = model_to_dict(req)
    if isinstance(request_body, dict):
        request_body["provider"] = provider
        request_body["conversationId"] = req.conversation_id
        request_body["runId"] = run_id
    return {
        "type": "request",
        "conversationId": req.conversation_id,
        "runId": run_id,
        "provider": provider,
        "model": req.model,
        "userPrompt": req.prompt,
        "requestBody": request_body,
    }


def _agent_response_payload(raw_response: dict) -> dict:
    response = AgentChatResponse(**raw_response)
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return response.dict()


def _code_review_response_payload(raw_response: dict) -> dict:
    response = CodeReviewResponse(**raw_response)
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True)
    return response.dict(by_alias=True)


def _response_event(
    response: dict,
    conversation_id: str,
    run_id: str,
    started: float,
) -> dict:
    duration = response.get("duration_ms")
    if duration is None:
        duration = int((time.time() - started) * 1000)
    return {
        "type": "response",
        "conversationId": conversation_id,
        "runId": run_id,
        "provider": response.get("provider"),
        "model": response.get("model"),
        "content": response.get("content", ""),
        "toolCalls": response.get("tool_calls", []),
        "success": bool(response.get("success", False)),
        "error": response.get("error"),
        "code": response.get("code"),
        "usage": response.get("usage", {}),
        "duration": duration,
    }


def _review_request_event(
    req: CodeReviewRequest,
    provider: Literal["mimo", "claude-code"],
    run_id: str,
) -> dict:
    request_body = model_to_dict(req)
    if isinstance(request_body, dict):
        request_body["provider"] = provider
        request_body["conversationId"] = req.conversation_id
        request_body["runId"] = run_id
    return {
        "type": "review_request",
        "conversationId": req.conversation_id,
        "runId": run_id,
        "provider": provider,
        "model": req.model,
        "userPrompt": req.prompt,
        "requestBody": request_body,
    }


def _review_response_event(
    response: dict,
    conversation_id: str,
    run_id: str,
    started: float,
) -> dict:
    duration = response.get("duration_ms")
    if duration is None:
        duration = int((time.time() - started) * 1000)
    return {
        "type": "review_response",
        "conversationId": conversation_id,
        "runId": run_id,
        "provider": response.get("provider"),
        "model": response.get("model"),
        "pass": bool(response.get("pass", False)),
        "summary": response.get("summary", ""),
        "issues": response.get("issues", []),
        "success": bool(response.get("success", False)),
        "error": response.get("error"),
        "code": response.get("code"),
        "usage": response.get("usage", {}),
        "duration": duration,
    }


def _exception_response_payload(
    exc: Exception,
    provider: Literal["mimo", "claude-code"],
    model: str,
) -> dict:
    code = "PROVIDER_ERROR"
    error = str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or code)
            error = str(
                detail.get("message")
                or detail.get("error")
                or detail.get("code")
                or error
            )
        else:
            error = str(detail or error)
    return {
        "provider": provider,
        "model": model,
        "content": "",
        "tool_calls": [],
        "success": False,
        "error": error,
        "code": code,
        "usage": {},
    }


def _review_exception_response_payload(
    exc: Exception,
    provider: Literal["mimo", "claude-code"],
    model: str,
) -> dict:
    code = "PROVIDER_ERROR"
    error = str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or code)
            error = str(
                detail.get("message")
                or detail.get("error")
                or detail.get("code")
                or error
            )
        else:
            error = str(detail or error)
    return {
        "provider": provider,
        "model": model,
        "pass": False,
        "summary": "Review provider failed.",
        "issues": [
            {
                "severity": "error",
                "code": code,
                "message": error,
            }
        ],
        "success": False,
        "error": error,
        "code": code,
        "usage": {},
        "raw": "",
    }


def _conversation_headers(conversation_id: str, run_id: str) -> dict[str, str]:
    return {
        "X-Conversation-Id": conversation_id,
        "X-Run-Id": run_id,
    }


async def _log_stream_response(
    generator: AsyncIterator[str],
    settings: Settings,
    conversation_id: str,
    run_id: str,
    started: float,
) -> AsyncIterator[str]:
    final_payload: dict | None = None
    async for chunk in generator:
        for payload in _sse_payloads(chunk):
            if payload.get("type") == "message_complete":
                final_payload = payload
        yield chunk

    if final_payload is None:
        final_payload = {
            "provider": None,
            "model": None,
            "content": "",
            "tool_calls": [],
            "success": False,
            "error": "Stream ended without message_complete",
            "code": "STREAM_RESPONSE_INCOMPLETE",
            "usage": {},
        }
    append_conversation_event(
        settings,
        conversation_id,
        _response_event(final_payload, conversation_id, run_id, started),
    )


def _sse_payloads(chunk: str) -> list[dict]:
    payloads: list[dict] = []
    for line in str(chunk).splitlines():
        if not line.startswith("data:"):
            continue
        raw = line.removeprefix("data:").strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


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


async def _execute_public_knowledge_tool(
    settings: Settings,
    call: BackendToolCall,
) -> BackendToolResult:
    results = await execute_backend_tool_calls([call], settings)
    return results[0]


def _public_knowledge_result_or_raise(result: BackendToolResult) -> dict:
    if result.status == "success":
        return result.result if isinstance(result.result, dict) else {"result": result.result}

    code = result.code or "KNOWLEDGE_TOOL_FAILED"
    if code in {
        "KNOWLEDGE_QUERY_REQUIRED",
        "KNOWLEDGE_PATH_REQUIRED",
        "KNOWLEDGE_PATH_FORBIDDEN",
        "BACKEND_TOOL_ARGUMENTS_INVALID",
    }:
        status_code = 400
    elif code == "KNOWLEDGE_NOT_FOUND":
        status_code = 404
    elif code == "KNOWLEDGE_ROOT_NOT_CONFIGURED":
        status_code = 503
    else:
        status_code = 502
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": result.error or f"{result.name} failed",
        },
    )


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
