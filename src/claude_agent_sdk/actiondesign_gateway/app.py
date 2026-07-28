"""FastAPI application for the thin ActionDesign model gateway."""

from __future__ import annotations

import json
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .claude_code_provider import call_claude_code, stream_claude_code
from .langsmith_forwarder import forward_trace_batch
from .mimo_provider import call_mimo, stream_mimo
from .models import (
    AgentChatRequest,
    AgentChatResponse,
    AgentModelsResponse,
    ApiProfileResponse,
    ApiProfileWriteRequest,
    DeleteProfileResponse,
    GatewayEventEnvelope,
    HealthResponse,
    ReadonlyMcpToolCallRequest,
    ReadonlyMcpToolCallResponse,
    ReadonlyMcpToolListResponse,
    RequestLogEntry,
    RequestLogListResponse,
    ToolResultRequest,
    ToolResultResponse,
    TraceBatchRequest,
    TraceBatchResponse,
)
from .profile_store import ApiProfileStore, ProfileSecretStore, ProviderProfile
from .provider_http import call_provider, stream_provider
from .request_log import RequestLogStore
from .redaction import redact_value
from .session_log import (
    ToolResultStore,
    append_conversation_event,
    append_log,
    model_to_dict,
    require_run_id_for_full_log,
    resolve_conversation_id,
)
from .settings import Settings
from .readonly_mcp import ReadonlyMcpError, ReadonlyMcpGateway

PROTOCOL_VERSION = "1.0"
API_PREFIX = "/api/actiondesign-agent"
SERVICE_AUTH_HEADER = "X-ActionDesign-Service-Token"
ProfileIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
RequestIdPath = Annotated[
    str,
    Path(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$"),
]


def create_app(
    settings: Settings | None = None,
    *,
    profile_secret_store: ProfileSecretStore | None = None,
) -> FastAPI:
    gateway_settings = settings or Settings()
    profile_store = ApiProfileStore(gateway_settings, profile_secret_store)
    tool_results = ToolResultStore()
    readonly_mcp_gateway = ReadonlyMcpGateway(gateway_settings)
    request_logs = RequestLogStore(
        gateway_settings.request_log_path,
        max_bytes=gateway_settings.request_log_max_bytes,
        backup_count=gateway_settings.request_log_backup_count,
    )
    app = FastAPI(title="ActionDesign Thin Agent Gateway", version="2.1.0")
    app.state.settings = gateway_settings
    app.state.profile_store = profile_store
    app.state.tool_results = tool_results
    app.state.readonly_mcp_gateway = readonly_mcp_gateway
    app.state.request_logs = request_logs

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {
                key: value
                for key, value in error.items()
                if key not in {"input", "ctx"}
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=gateway_settings.allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get(f"{API_PREFIX}/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "runtime": "thin-gateway",
            "protocolVersion": PROTOCOL_VERSION,
            "langsmithEnabled": bool(
                gateway_settings.langsmith_enabled
                and gateway_settings.langsmith_api_key
            ),
            "readonlyMcpEnabled": readonly_mcp_gateway.enabled,
        }

    @app.get(f"{API_PREFIX}/models", response_model=AgentModelsResponse)
    async def models() -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for profile in profile_store.list_public():
            grouped.setdefault(profile["provider"], []).append(profile)
        providers = {
            provider: _provider_capability(
                provider,
                profiles,
                profile_store.default_profile_id(provider),
            )
            for provider, profiles in grouped.items()
        }
        default_capability = providers.get(gateway_settings.default_provider)
        return {
            "defaultProvider": gateway_settings.default_provider,
            "defaultModel": default_capability.get("defaultModel", "") if default_capability else "",
            "supportedProtocolVersions": [PROTOCOL_VERSION],
            "providers": providers,
        }

    @app.post(
        f"{API_PREFIX}/chat",
        response_model=AgentChatResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def generic_chat(
        req: AgentChatRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        return await _chat(
            req,
            request,
            None,
            gateway_settings,
            profile_store,
            response,
        )

    @app.post(
        f"{API_PREFIX}/chat/stream",
        dependencies=[Depends(_require_service_auth)],
    )
    async def generic_chat_stream(
        req: AgentChatRequest,
        request: Request,
    ) -> StreamingResponse:
        return _chat_stream(req, request, None, gateway_settings, profile_store)

    @app.post(
        f"{API_PREFIX}/mimo/chat",
        response_model=AgentChatResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def mimo_chat(req: AgentChatRequest, request: Request, response: Response) -> dict[str, Any]:
        return await _chat(req, request, "mimo", gateway_settings, profile_store, response)

    @app.post(
        f"{API_PREFIX}/claude-code/chat",
        response_model=AgentChatResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def claude_chat(req: AgentChatRequest, request: Request, response: Response) -> dict[str, Any]:
        return await _chat(req, request, "claude-code", gateway_settings, profile_store, response)

    @app.post(
        f"{API_PREFIX}/mimo/chat/stream",
        dependencies=[Depends(_require_service_auth)],
    )
    async def mimo_chat_stream(req: AgentChatRequest, request: Request) -> StreamingResponse:
        return _chat_stream(req, request, "mimo", gateway_settings, profile_store)

    @app.post(
        f"{API_PREFIX}/claude-code/chat/stream",
        dependencies=[Depends(_require_service_auth)],
    )
    async def claude_chat_stream(req: AgentChatRequest, request: Request) -> StreamingResponse:
        return _chat_stream(req, request, "claude-code", gateway_settings, profile_store)

    @app.post(
        f"{API_PREFIX}/tool-result",
        response_model=ToolResultResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def tool_result(req: ToolResultRequest, request: Request) -> dict[str, Any]:
        conversation_id = resolve_conversation_id(request, req)
        duplicate = tool_results.store(gateway_settings, conversation_id, req)
        append_conversation_event(
            gateway_settings,
            conversation_id,
            {"type": "tool_result", **model_to_dict(req), "duplicate": duplicate},
        )
        return {"success": True, "message": "recorded", "duplicate": duplicate}

    @app.get(
        f"{API_PREFIX}/mcp/gxp-lowcode-readonly/tools",
        response_model=ReadonlyMcpToolListResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def readonly_mcp_tools(request: Request) -> dict[str, Any]:
        _require_readonly_mcp_origin(request, gateway_settings)
        gateway: ReadonlyMcpGateway = request.app.state.readonly_mcp_gateway
        try:
            return await gateway.list_tools()
        except ReadonlyMcpError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.post(
        f"{API_PREFIX}/mcp/gxp-lowcode-readonly/call",
        response_model=ReadonlyMcpToolCallResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(_require_service_auth)],
    )
    async def readonly_mcp_call(
        req: ReadonlyMcpToolCallRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_readonly_mcp_origin(request, gateway_settings)
        gateway: ReadonlyMcpGateway = request.app.state.readonly_mcp_gateway
        try:
            response = await gateway.call_tool(req.tool_name, req.arguments)
        except ReadonlyMcpError as exc:
            _log_readonly_mcp_call(gateway_settings, req, None, exc)
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        _log_readonly_mcp_call(gateway_settings, req, response, None)
        return response

    @app.post(
        f"{API_PREFIX}/telemetry/langsmith/runs/batch",
        response_model=TraceBatchResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def langsmith_batch(req: TraceBatchRequest) -> dict[str, Any]:
        batch_id = f"trace_batch_{secrets.token_hex(8)}"
        append_log(gateway_settings, batch_id, "langsmith-trace-batches.jsonl", req)
        trace_events = [model_to_dict(event) for event in req.events]
        if not gateway_settings.langsmith_enabled or not gateway_settings.langsmith_api_key:
            response = {
                "exportBatchId": batch_id,
                "accepted": 0,
                "rejected": len(trace_events),
                "status": "disabled",
            }
            append_log(
                gateway_settings,
                batch_id,
                "langsmith-trace-batches.jsonl",
                {"type": "langsmith_trace_batch_result", **response},
            )
            return response
        accepted, rejected = await forward_trace_batch(gateway_settings, trace_events)
        response = {
            "exportBatchId": batch_id,
            "accepted": accepted,
            "rejected": rejected,
            "status": "accepted" if rejected == 0 else "partial",
        }
        append_log(
            gateway_settings,
            batch_id,
            "langsmith-trace-batches.jsonl",
            {"type": "langsmith_trace_batch_result", **response},
        )
        return response

    @app.get(
        f"{API_PREFIX}/admin/profiles",
        response_model=list[ApiProfileResponse],
    )
    async def list_profiles(request: Request) -> list[dict[str, Any]]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.list_public()

    @app.get(
        f"{API_PREFIX}/admin/request-logs",
        response_model=RequestLogListResponse,
    )
    async def list_request_logs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        provider: Annotated[str, Query(max_length=64)] = "",
        profile_id: Annotated[str, Query(alias="profileId", max_length=64)] = "",
        protocol: Annotated[str, Query(pattern=r"^(openai|claude)?$")] = "",
        status: Annotated[str, Query(pattern=r"^(success|error|cancelled)?$")] = "",
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        items, total = request_logs.list(
            limit=limit,
            offset=offset,
            provider=provider,
            profile_id=profile_id,
            protocol=protocol,
            status=status,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get(
        f"{API_PREFIX}/admin/request-logs/{{request_id}}",
        response_model=RequestLogEntry,
    )
    async def get_request_log(
        request_id: RequestIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        entry = request_logs.get(request_id)
        if entry is None:
            raise HTTPException(status_code=404, detail={"code": "REQUEST_LOG_NOT_FOUND"})
        return entry

    @app.put(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}",
        response_model=ApiProfileResponse,
    )
    async def put_profile(
        profile_id: ProfileIdPath,
        req: ApiProfileWriteRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        if profile_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "PROFILE_ID_MISMATCH"})
        response = profile_store.upsert(req)
        append_log(
            gateway_settings,
            "profile-admin",
            "profile-admin-audit.jsonl",
            {
                "type": "profile_upsert",
                "profileId": response["id"],
                "provider": response["provider"],
                "displayName": response["displayName"],
                "protocol": response["protocol"],
                "baseUrl": response["baseUrl"],
                "defaultModel": response["defaultModel"],
                "modelCount": len(response["models"]),
                "authMode": response["authMode"],
                "enabled": response["enabled"],
                "keyUpdated": bool(req.api_key),
            },
        )
        return response

    @app.delete(f"{API_PREFIX}/admin/profiles/{{profile_id}}", response_model=DeleteProfileResponse)
    async def delete_profile(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        success = profile_store.delete(profile_id)
        append_log(
            gateway_settings,
            "profile-admin",
            "profile-admin-audit.jsonl",
            {"type": "profile_delete", "profileId": profile_id, "success": success},
        )
        return {"success": success}

    return app


async def _chat(
    req: AgentChatRequest,
    request: Request,
    provider: str | None,
    settings: Settings,
    profiles: ApiProfileStore,
    response: Response,
) -> dict[str, Any]:
    conversation_id = resolve_conversation_id(request, req)
    require_run_id_for_full_log(settings, req)
    concrete_provider = provider or (
        settings.default_provider if req.provider == "auto" else req.provider
    )
    req = AgentChatRequest.model_validate({
        **req.model_dump(mode="python"),
        "provider": concrete_provider,
        "conversation_id": conversation_id,
    })
    profile = profiles.resolve(concrete_provider, req.profile_id)
    _validate_profile_request(req, profile)
    request_id = f"req_{secrets.token_hex(12)}"
    response.headers["X-Request-Id"] = request_id
    started = time.monotonic()
    request_logs: RequestLogStore = request.app.state.request_logs
    _log_request(settings, conversation_id, req, profile)
    try:
        response = (
            await call_mimo(req, profile)
            if concrete_provider == "mimo"
            else (
                await call_claude_code(req, profile)
                if concrete_provider == "claude-code"
                else await call_provider(req, profile)
            )
        )
    except HTTPException as exc:
        detail = _error_detail(exc)
        _log_error(settings, conversation_id, req, detail)
        request_logs.record(
            request_id=request_id,
            request=req,
            profile=profile,
            started=started,
            status="error",
            error=detail,
            http_status=exc.status_code,
            stream=False,
        )
        raise
    _log_response(settings, conversation_id, req, response)
    request_logs.record(
        request_id=request_id,
        request=req,
        profile=profile,
        started=started,
        status="success" if response.get("success", True) else "error",
        response=response,
        error={
            "code": response.get("code") or "",
            "message": response.get("error") or "",
        },
        stream=False,
    )
    return response


def _chat_stream(
    req: AgentChatRequest,
    request: Request,
    provider: str | None,
    settings: Settings,
    profiles: ApiProfileStore,
) -> StreamingResponse:
    conversation_id = resolve_conversation_id(request, req)
    require_run_id_for_full_log(settings, req)
    concrete_provider = provider or (
        settings.default_provider if req.provider == "auto" else req.provider
    )
    req = AgentChatRequest.model_validate({
        **req.model_dump(mode="python"),
        "provider": concrete_provider,
        "conversation_id": conversation_id,
    })
    profile = profiles.resolve(concrete_provider, req.profile_id)
    _validate_profile_request(req, profile)
    header_trace_id = request.headers.get("X-Agent-Trace-Id", "").strip()
    if header_trace_id and not req.trace_id:
        try:
            req = AgentChatRequest.model_validate({
                **req.model_dump(mode="python"),
                "trace_id": header_trace_id,
            })
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_TRACE_ID", "field": "trace_id"},
            ) from exc
    trace_id = req.trace_id or f"trace_{secrets.token_hex(8)}"
    request_id = f"req_{secrets.token_hex(12)}"
    started = time.monotonic()
    request_logs: RequestLogStore = request.app.state.request_logs
    _log_request(settings, conversation_id, req, profile)

    async def generate() -> AsyncIterator[str]:
        sequence = 0
        finalized = False
        iterator: AsyncIterator[dict[str, Any]] = (
            stream_mimo(req, profile)
            if concrete_provider == "mimo"
            else (
                stream_claude_code(req, profile)
                if concrete_provider == "claude-code"
                else stream_provider(req, profile)
            )
        )
        try:
            async for event in iterator:
                sequence += 1
                if event.get("kind") == "delta":
                    yield _sse_envelope(req, trace_id, sequence, "model.delta", {"content": event.get("content", "")})
                    continue
                payload = event.get("payload") or {}
                _log_response(settings, conversation_id, req, payload)
                request_logs.record(
                    request_id=request_id,
                    request=req,
                    profile=profile,
                    started=started,
                    status="success" if payload.get("success", True) else "error",
                    response=payload,
                    error={
                        "code": payload.get("code") or "",
                        "message": payload.get("error") or "",
                    },
                    stream=True,
                )
                finalized = True
                yield _sse_envelope(req, trace_id, sequence, "model.completed", payload)
        except Exception as exc:  # stream already started; emit an error envelope
            sequence += 1
            detail = _error_detail(exc)
            _log_error(settings, conversation_id, req, detail)
            request_logs.record(
                request_id=request_id,
                request=req,
                profile=profile,
                started=started,
                status="error",
                error=detail,
                http_status=exc.status_code if isinstance(exc, HTTPException) else 500,
                stream=True,
            )
            finalized = True
            yield _sse_envelope(req, trace_id, sequence, "gateway.error", detail)
        finally:
            if not finalized:
                request_logs.record(
                    request_id=request_id,
                    request=req,
                    profile=profile,
                    started=started,
                    status="cancelled",
                    error={
                        "code": "CLIENT_STREAM_CANCELLED",
                        "message": "Client disconnected before the provider stream completed",
                    },
                    http_status=499,
                    stream=True,
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Request-Id": request_id},
    )


def _provider_capability(
    provider: str,
    profiles: list[dict[str, Any]],
    default_profile_id: str,
) -> dict[str, Any]:
    ordered = sorted(
        profiles,
        key=lambda item: (not item["id"].startswith("default-"), item["id"]),
    )
    ready_profiles = [
        profile
        for profile in ordered
        if profile.get("enabled") and profile.get("keyConfigured")
    ]
    selected = next(
        (
            profile
            for profile in ordered
            if profile.get("id") == default_profile_id
        ),
        ready_profiles[0] if ready_profiles else ordered[0],
    )
    models: dict[str, dict[str, Any]] = {}
    for profile in ready_profiles:
        for model in profile.get("models") or []:
            models.setdefault(str(model.get("id") or ""), model)
    ready = bool(ready_profiles)
    return {
        "displayName": selected.get("displayName") or provider,
        "protocol": selected.get("protocol") or "claude",
        "defaultProfileId": selected.get("id") or "",
        "status": "ready" if ready else "unavailable",
        "defaultModel": selected.get("defaultModel") or "",
        "models": [
            {
                "id": model_id,
                "name": metadata.get("name") or model_id,
                "provider": provider,
                "supportsImages": bool(metadata.get("supportsImages", True)),
                "contextWindowTokens": int(metadata.get("contextWindowTokens", 100_000)),
                "maxOutputTokens": int(metadata.get("maxOutputTokens", 8_192)),
                "supportsNativeTools": bool(metadata.get("supportsNativeTools", True)),
                "supportsThinking": bool(metadata.get("supportsThinking", False)),
            }
            for model_id, metadata in models.items()
            if model_id
        ] if ready else [],
        **({"error": "API profile/key is not configured"} if not ready else {}),
    }


def _validate_profile_request(
    req: AgentChatRequest,
    profile: ProviderProfile,
) -> None:
    model_id = req.model or profile.default_model
    model = next((item for item in profile.models if item.id == model_id), None)
    if model is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PROFILE_MODEL_UNAVAILABLE",
                "model": model_id,
                "profileId": profile.id,
            },
        )
    if req.images and not model.supports_images:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_DOES_NOT_SUPPORT_IMAGES",
                "model": model_id,
            },
        )
    if req.tools and not model.supports_native_tools:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_DOES_NOT_SUPPORT_NATIVE_TOOLS",
                "model": model_id,
            },
        )
    if req.max_tokens > model.max_output_tokens:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_MAX_OUTPUT_TOKENS_EXCEEDED",
                "model": model_id,
                "maxOutputTokens": model.max_output_tokens,
            },
        )
    if req.thinking.type == "enabled" and not model.supports_thinking:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MODEL_DOES_NOT_SUPPORT_THINKING",
                "model": model_id,
            },
        )


def _sse_envelope(
    req: AgentChatRequest,
    trace_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    envelope = GatewayEventEnvelope.model_validate({
        "protocolVersion": PROTOCOL_VERSION,
        "sequence": sequence,
        "conversationId": req.conversation_id,
        "runId": req.run_id,
        "traceId": trace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "payload": payload,
    }).model_dump(mode="json", by_alias=True)
    return f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"


def _log_request(
    settings: Settings,
    conversation_id: str,
    req: AgentChatRequest,
    profile: ProviderProfile,
) -> None:
    request_data = model_to_dict(req)
    system_prompt = str(request_data.pop("systemPrompt", ""))
    prompt = str(request_data.pop("prompt", ""))
    images = request_data.get("images") or []
    request_data["images"] = [
        {
            "media_type": image.get("media_type", ""),
            "source": "base64" if image.get("data") else "url",
            "encodedChars": len(str(image.get("data") or "")),
        }
        for image in images
    ]
    request_data["promptMetadata"] = {
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "characters": len(prompt),
    }
    request_data["systemPromptMetadata"] = {
        "sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "characters": len(system_prompt),
    }
    append_conversation_event(
        settings,
        conversation_id,
        {
            "type": "gateway_request",
            "provider": profile.provider,
            "profileId": profile.id,
            "request": redact_value(request_data),
        },
    )


def _log_response(
    settings: Settings,
    conversation_id: str,
    req: AgentChatRequest,
    response: dict[str, Any],
) -> None:
    response_data = model_to_dict(response)
    content = str(response_data.pop("content", ""))
    response_data["contentMetadata"] = {
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "characters": len(content),
    }
    append_conversation_event(
        settings,
        conversation_id,
        {"type": "gateway_response", "runId": req.run_id, "response": redact_value(response_data)},
    )


def _log_error(
    settings: Settings,
    conversation_id: str,
    req: AgentChatRequest,
    error: Any,
) -> None:
    append_conversation_event(
        settings,
        conversation_id,
        {"type": "gateway_error", "runId": req.run_id, "error": redact_value(error)},
    )


def _error_detail(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        message = redact_value(str(detail.get("message", str(exc.detail))))
        return {
            "code": str(detail.get("code", "GATEWAY_ERROR")),
            "message": str(message),
        }
    return {
        "code": "GATEWAY_ERROR",
        "message": str(redact_value(str(exc))),
    }


def _require_profile_admin(request: Request, settings: Settings) -> None:
    if not settings.profile_admin_token:
        raise HTTPException(status_code=503, detail={"code": "PROFILE_ADMIN_DISABLED"})
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.profile_admin_token}"
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail={"code": "PROFILE_ADMIN_AUTH_REQUIRED"})


def _require_service_auth(
    request: Request,
    service_token: Annotated[
        str | None,
        Header(alias=SERVICE_AUTH_HEADER, max_length=8192),
    ] = None,
) -> None:
    settings: Settings = request.app.state.settings
    if settings.service_auth_mode == "disabled":
        return
    if not service_token or not secrets.compare_digest(
        service_token,
        settings.service_auth_token,
    ):
        raise HTTPException(
            status_code=401,
            detail={"code": "GATEWAY_SERVICE_AUTH_REQUIRED"},
        )


def _require_readonly_mcp_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed = {item.rstrip("/") for item in settings.allow_origins if item != "*"}
    if not origin or origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "READONLY_MCP_ORIGIN_NOT_ALLOWED"},
        )


def _log_readonly_mcp_call(
    settings: Settings,
    req: ReadonlyMcpToolCallRequest,
    response: dict[str, Any] | None,
    error: ReadonlyMcpError | None,
) -> None:
    arguments_text = json.dumps(redact_value(req.arguments), ensure_ascii=False, default=str)
    result_value = response.get("result") if response else None
    result_text = json.dumps(redact_value(result_value), ensure_ascii=False, default=str)
    append_conversation_event(
        settings,
        req.conversation_id,
        {
            "type": "readonly_mcp_call",
            "runId": req.run_id,
            "topicId": req.topic_id,
            "checkpointId": req.checkpoint_id,
            "traceId": req.trace_id,
            "skillId": req.skill_id,
            "skillVersion": req.skill_version,
            "serverId": "gxp-lowcode-readonly",
            "toolName": req.tool_name,
            "argumentKeys": sorted(req.arguments),
            "argumentsMetadata": {
                "sha256": hashlib.sha256(arguments_text.encode("utf-8")).hexdigest(),
                "characters": len(arguments_text),
            },
            "resultMetadata": {
                "sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
                "characters": len(result_text),
                "success": bool(response and response.get("success")),
                "durationMs": int(response.get("durationMs", 0)) if response else 0,
            },
            **(
                {"error": {"code": error.code, "message": str(error)}}
                if error
                else {}
            ),
        },
    )


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("claude_agent_sdk.actiondesign_gateway.app:app", host="0.0.0.0", port=8318)


if __name__ == "__main__":
    main()
