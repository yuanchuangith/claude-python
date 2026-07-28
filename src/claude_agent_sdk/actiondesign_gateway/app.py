"""FastAPI application for the thin ActionDesign model gateway."""

from __future__ import annotations

import json
import hashlib
import ipaddress
import secrets
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .claude_code_provider import call_claude_code, stream_claude_code
from .identifiers import CONVERSATION_ID_PATTERN
from .langsmith_forwarder import forward_trace_batch
from .mimo_provider import call_mimo, stream_mimo
from .models import (
    AccessKeyRotateRequest,
    AccessKeyRotateResponse,
    AccessKeyStatusResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    AgentChatRequest,
    AgentChatResponse,
    AgentModelsResponse,
    ApiProfileResponse,
    ApiProfileWriteRequest,
    AuditLogListResponse,
    ConversationEventsResponse,
    ConversationListResponse,
    ConversationSealRequest,
    ConversationSealResponse,
    DeleteProfileResponse,
    GatewayEventEnvelope,
    HealthResponse,
    ReadonlyMcpToolCallRequest,
    ReadonlyMcpToolCallResponse,
    ReadonlyMcpToolListResponse,
    RequestLogDetailResponse,
    RequestLogEntry,
    RequestLogListResponse,
    ToolResultRequest,
    ToolResultResponse,
    TraceBatchRequest,
    TraceBatchResponse,
)
from .admin_security import AdminSessionStore, BrowserApiKeyStore
from .audit_log import AuditLogStore
from .profile_store import (
    ApiProfileStore,
    ProfileSecretStore,
    ProviderProfile,
    RouteCandidate,
)
from .provider_http import call_provider, stream_provider
from .request_log import RequestLogStore
from .redaction import redact_value
from .session_log import (
    ToolResultStore,
    append_conversation_event,
    append_log,
    model_to_dict,
    list_conversations,
    read_conversation,
    resolve_request_identity,
    seal_conversation,
)
from .settings import Settings
from .readonly_mcp import ReadonlyMcpError, ReadonlyMcpGateway

PROTOCOL_VERSION = "1.0"
API_PREFIX = "/api/actiondesign-agent"
SERVICE_AUTH_HEADER = "X-ActionDesign-Service-Token"
BROWSER_API_KEY_HEADER = "X-ActionDesign-Api-Key"
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
ConversationIdPath = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
RuntimeConversationIdPath = Annotated[
    str,
    Path(min_length=36, max_length=36, pattern=CONVERSATION_ID_PATTERN),
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
    admin_sessions = AdminSessionStore(
        gateway_settings.admin_password,
        ttl_seconds=gateway_settings.admin_session_ttl_seconds,
    )
    browser_api_keys = BrowserApiKeyStore(
        gateway_settings.browser_api_key_store_path
    )
    audit_root = FilePath(
        gateway_settings.log_root or gateway_settings.profile_store_path.parent
    )
    audit_logs = AuditLogStore(audit_root / "system" / "admin-audit.jsonl")
    app = FastAPI(title="ActionDesign Thin Agent Gateway", version="2.2.0")
    app.state.settings = gateway_settings
    app.state.profile_store = profile_store
    app.state.tool_results = tool_results
    app.state.readonly_mcp_gateway = readonly_mcp_gateway
    app.state.request_logs = request_logs
    app.state.admin_sessions = admin_sessions
    app.state.browser_api_keys = browser_api_keys
    app.state.audit_logs = audit_logs

    @app.middleware("http")
    async def admin_ui_security_headers(
        request: Request,
        call_next: Any,
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    admin_static_root = FilePath(__file__).with_name("static") / "admin"
    app.mount(
        "/admin",
        StaticFiles(directory=str(admin_static_root), html=True),
        name="actiondesign-admin",
    )

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
            "fullConversationLogEnabled": gateway_settings.full_conversation_log_enabled,
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
        routing_order = profile_store.routing_order()
        default_route = routing_order[0] if routing_order else None
        return {
            "defaultProvider": (
                default_route.profile.provider
                if default_route is not None
                else gateway_settings.default_provider
            ),
            "defaultModel": default_route.model.id if default_route is not None else "",
            "supportedProtocolVersions": [PROTOCOL_VERSION],
            "providers": providers,
            "routingOrder": [
                {
                    "position": index,
                    "provider": candidate.profile.provider,
                    "profileId": candidate.profile.id,
                    "protocol": candidate.profile.protocol,
                    "model": candidate.model.id,
                    "profilePriority": candidate.profile.priority,
                    "modelPriority": candidate.model.priority,
                    "isDefaultProfile": (
                        candidate.profile.id
                        == profile_store.default_profile_id(candidate.profile.provider)
                    ),
                    "isDefaultModel": candidate.model.id == candidate.profile.default_model,
                }
                for index, candidate in enumerate(routing_order, start=1)
            ],
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
    async def tool_result(
        req: ToolResultRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        conversation_id, _ = resolve_request_identity(request, req)
        request_id = f"req_{secrets.token_hex(12)}"
        response.headers["X-Request-Id"] = request_id
        duplicate = tool_results.store(
            gateway_settings,
            conversation_id,
            req,
            request_id=request_id,
        )
        append_conversation_event(
            gateway_settings,
            conversation_id,
            {
                "type": "tool_result",
                **model_to_dict(req),
                "requestId": request_id,
                "duplicate": duplicate,
            },
        )
        return {"success": True, "message": "recorded", "duplicate": duplicate}

    @app.post(
        f"{API_PREFIX}/conversations/{{conversation_id}}/seal",
        response_model=ConversationSealResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def service_seal_conversation(
        conversation_id: RuntimeConversationIdPath,
        req: ConversationSealRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        request_id = f"req_{secrets.token_hex(12)}"
        response.headers["X-Request-Id"] = request_id
        result = seal_conversation(gateway_settings, conversation_id, req.status)
        audit_logs.record(
            action="conversation_seal",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"conversationId": conversation_id, "status": req.status},
        )
        return result

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

    @app.post(
        f"{API_PREFIX}/admin/login",
        response_model=AdminLoginResponse,
    )
    async def admin_login(
        req: AdminLoginRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_admin_origin(request, gateway_settings)
        if not gateway_settings.admin_password:
            raise HTTPException(
                status_code=503,
                detail={"code": "ADMIN_LOGIN_DISABLED"},
            )
        actor = _audit_actor(request)
        request_id = f"admin_{secrets.token_hex(8)}"
        result, status = admin_sessions.login(req.password, actor=actor)
        audit_logs.record(
            action="admin_login",
            success=result is not None,
            actor=actor,
            request_id=request_id,
            details={"result": status},
        )
        if result is None:
            if status == "blocked":
                raise HTTPException(
                    status_code=429,
                    detail={"code": "ADMIN_LOGIN_RATE_LIMITED"},
                )
            raise HTTPException(
                status_code=401,
                detail={"code": "ADMIN_LOGIN_FAILED"},
            )
        return result

    @app.get(
        f"{API_PREFIX}/admin/access-key",
        response_model=AccessKeyStatusResponse,
    )
    async def get_access_key(request: Request) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return browser_api_keys.public()

    @app.post(
        f"{API_PREFIX}/admin/access-key/rotate",
        response_model=AccessKeyRotateResponse,
    )
    async def rotate_access_key(
        req: AccessKeyRotateRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        try:
            result = browser_api_keys.rotate(req.api_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_BROWSER_API_KEY"},
            ) from exc
        audit_logs.record(
            action="browser_api_key_rotate",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={"masked": result["masked"]},
        )
        return result

    @app.get(
        f"{API_PREFIX}/admin/audit-logs",
        response_model=AuditLogListResponse,
    )
    async def list_audit_logs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        items, total = audit_logs.list(limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get(
        f"{API_PREFIX}/admin/conversations",
        response_model=ConversationListResponse,
    )
    async def admin_list_conversations(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        items, total = list_conversations(
            gateway_settings,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get(
        f"{API_PREFIX}/admin/conversations/{{conversation_id}}",
        response_model=ConversationEventsResponse,
    )
    async def admin_get_conversation(
        conversation_id: ConversationIdPath,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        items, total = read_conversation(
            gateway_settings,
            conversation_id,
            limit=limit,
            offset=offset,
        )
        return {
            "conversationId": conversation_id,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.post(
        f"{API_PREFIX}/admin/conversations/{{conversation_id}}/seal",
        response_model=ConversationSealResponse,
    )
    async def admin_seal_conversation(
        conversation_id: ConversationIdPath,
        req: ConversationSealRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        result = seal_conversation(gateway_settings, conversation_id, req.status)
        audit_logs.record(
            action="conversation_seal",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={"conversationId": conversation_id, "status": req.status},
        )
        return result

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
        response_model=RequestLogDetailResponse,
    )
    async def get_request_log(
        request_id: RequestIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        attempts = request_logs.get_attempts(request_id)
        if not attempts:
            raise HTTPException(status_code=404, detail={"code": "REQUEST_LOG_NOT_FOUND"})
        return {
            **attempts[-1],
            "attemptCount": len(attempts),
            "attempts": attempts,
        }

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
        audit_logs.record(
            action="profile_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "profileId": response["id"],
                "provider": response["provider"],
                "protocol": response["protocol"],
                "priority": response["priority"],
                "defaultModel": response["defaultModel"],
                "modelPriorities": {
                    item["id"]: item["priority"] for item in response["models"]
                },
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
        audit_logs.record(
            action="profile_delete",
            success=success,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={"profileId": profile_id},
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
    conversation_id, _ = resolve_request_identity(request, req)
    routes = _routes_for_request(req, provider, profiles)
    request_id = f"req_{secrets.token_hex(12)}"
    response.headers["X-Request-Id"] = request_id
    request_logs: RequestLogStore = request.app.state.request_logs
    index = 0
    attempt = 0
    while index < len(routes):
        route = routes[index]
        attempt += 1
        routed_request = _request_for_route(req, route)
        started = time.monotonic()
        _log_request(
            settings,
            conversation_id,
            routed_request,
            route.profile,
            request_id=request_id,
            attempt=attempt,
        )
        try:
            result = await _call_route(routed_request, route.profile)
        except HTTPException as exc:
            detail = _error_detail(exc)
            next_index = _next_route_index(routes, index, route, exc)
            _log_error(
                settings,
                conversation_id,
                routed_request,
                detail,
                request_id=request_id,
                attempt=attempt,
                terminal=next_index is None,
            )
            request_logs.record(
                request_id=request_id,
                request=routed_request,
                profile=route.profile,
                started=started,
                status="error",
                error=detail,
                http_status=exc.status_code,
                stream=False,
                attempt=attempt,
            )
            if next_index is None:
                response.headers["X-Route-Attempts"] = str(attempt)
                raise
            index = next_index
            continue
        _log_response(
            settings,
            conversation_id,
            routed_request,
            result,
            request_id=request_id,
            attempt=attempt,
        )
        request_logs.record(
            request_id=request_id,
            request=routed_request,
            profile=route.profile,
            started=started,
            status="success" if result.get("success", True) else "error",
            response=result,
            error={
                "code": result.get("code") or "",
                "message": result.get("error") or "",
            },
            stream=False,
            attempt=attempt,
        )
        response.headers["X-Route-Attempts"] = str(attempt)
        response.headers["X-Actual-Profile"] = route.profile.id
        response.headers["X-Actual-Model"] = route.model.id
        return result
    raise HTTPException(status_code=503, detail={"code": "PROVIDER_ROUTE_EXHAUSTED"})


def _chat_stream(
    req: AgentChatRequest,
    request: Request,
    provider: str | None,
    settings: Settings,
    profiles: ApiProfileStore,
) -> StreamingResponse:
    conversation_id, _ = resolve_request_identity(request, req)
    routes = _routes_for_request(req, provider, profiles)
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
    request_logs: RequestLogStore = request.app.state.request_logs

    async def generate() -> AsyncIterator[str]:
        sequence = 0
        finalized = False
        emitted_output = False
        streamed_parts: list[str] = []
        index = 0
        attempt = 0
        try:
            while index < len(routes):
                route = routes[index]
                attempt += 1
                routed_request = _request_for_route(req, route)
                started = time.monotonic()
                _log_request(
                    settings,
                    conversation_id,
                    routed_request,
                    route.profile,
                    request_id=request_id,
                    attempt=attempt,
                )
                iterator = _stream_route(routed_request, route.profile)
                try:
                    async for event in iterator:
                        sequence += 1
                        if event.get("kind") == "delta":
                            content = str(event.get("content") or "")
                            if content:
                                emitted_output = True
                                streamed_parts.append(content)
                                yield _sse_envelope(
                                    routed_request,
                                    trace_id,
                                    sequence,
                                    "model.delta",
                                    {"content": content},
                                )
                            continue
                        payload = event.get("payload") or {}
                        _log_response(
                            settings,
                            conversation_id,
                            routed_request,
                            payload,
                            request_id=request_id,
                            attempt=attempt,
                        )
                        request_logs.record(
                            request_id=request_id,
                            request=routed_request,
                            profile=route.profile,
                            started=started,
                            status="success" if payload.get("success", True) else "error",
                            response=payload,
                            error={
                                "code": payload.get("code") or "",
                                "message": payload.get("error") or "",
                            },
                            stream=True,
                            attempt=attempt,
                        )
                        finalized = True
                        yield _sse_envelope(
                            routed_request,
                            trace_id,
                            sequence,
                            "model.completed",
                            payload,
                        )
                        return
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "PROVIDER_STREAM_INVALID",
                            "message": "Provider stream ended without completion",
                            "retryable": True,
                            "failoverScope": "model",
                        },
                    )
                except Exception as exc:
                    detail = _error_detail(exc)
                    next_index = None if emitted_output else _next_route_index(
                        routes,
                        index,
                        route,
                        exc,
                    )
                    _log_error(
                        settings,
                        conversation_id,
                        routed_request,
                        detail,
                        request_id=request_id,
                        attempt=attempt,
                        terminal=next_index is None,
                        partial_content="".join(streamed_parts),
                    )
                    request_logs.record(
                        request_id=request_id,
                        request=routed_request,
                        profile=route.profile,
                        started=started,
                        status="error",
                        error=detail,
                        http_status=exc.status_code if isinstance(exc, HTTPException) else 500,
                        stream=True,
                        attempt=attempt,
                    )
                    if next_index is not None:
                        index = next_index
                        continue
                    sequence += 1
                    finalized = True
                    yield _sse_envelope(
                        routed_request,
                        trace_id,
                        sequence,
                        "gateway.error",
                        detail,
                    )
                    return
        finally:
            if not finalized:
                route = routes[min(index, len(routes) - 1)]
                routed_request = _request_for_route(req, route)
                cancelled_error = {
                    "code": "CLIENT_STREAM_CANCELLED",
                    "message": "Client disconnected before the provider stream completed",
                }
                _log_error(
                    settings,
                    conversation_id,
                    routed_request,
                    cancelled_error,
                    request_id=request_id,
                    attempt=max(1, attempt),
                    terminal=True,
                    status="cancelled",
                    partial_content="".join(streamed_parts),
                )
                request_logs.record(
                    request_id=request_id,
                    request=routed_request,
                    profile=route.profile,
                    started=time.monotonic(),
                    status="cancelled",
                    error=cancelled_error,
                    http_status=499,
                    stream=True,
                    attempt=max(1, attempt),
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Request-Id": request_id},
    )


def _routes_for_request(
    req: AgentChatRequest,
    forced_provider: str | None,
    profiles: ApiProfileStore,
) -> list[RouteCandidate]:
    requested_provider = forced_provider or req.provider
    routes = profiles.route_candidates(
        provider=requested_provider,
        profile_id=req.profile_id,
        model=req.model,
    )
    first_request = _request_for_route(req, routes[0])
    _validate_profile_request(first_request, routes[0].profile)
    compatible = [routes[0]]
    for route in routes[1:]:
        routed_request = _request_for_route(req, route)
        try:
            _validate_profile_request(routed_request, route.profile)
        except HTTPException:
            continue
        compatible.append(route)
    return compatible


def _request_for_route(
    req: AgentChatRequest,
    route: RouteCandidate,
) -> AgentChatRequest:
    return AgentChatRequest.model_validate(
        {
            **req.model_dump(mode="python"),
            "provider": route.profile.provider,
            "profile_id": route.profile.id,
            "model": route.model.id,
        }
    )


async def _call_route(
    req: AgentChatRequest,
    profile: ProviderProfile,
) -> dict[str, Any]:
    if profile.provider == "mimo":
        return await call_mimo(req, profile)
    if profile.provider == "claude-code":
        return await call_claude_code(req, profile)
    return await call_provider(req, profile)


def _stream_route(
    req: AgentChatRequest,
    profile: ProviderProfile,
) -> AsyncIterator[dict[str, Any]]:
    if profile.provider == "mimo":
        return stream_mimo(req, profile)
    if profile.provider == "claude-code":
        return stream_claude_code(req, profile)
    return stream_provider(req, profile)


def _next_route_index(
    routes: list[RouteCandidate],
    current_index: int,
    current: RouteCandidate,
    exc: Exception,
) -> int | None:
    scope = _failover_scope(exc)
    if scope == "none":
        return None
    index = current_index + 1
    if scope == "profile":
        while index < len(routes) and routes[index].profile.id == current.profile.id:
            index += 1
    return index if index < len(routes) else None


def _failover_scope(exc: Exception) -> str:
    if not isinstance(exc, HTTPException) or not isinstance(exc.detail, dict):
        return "none"
    if exc.detail.get("retryable") is not True:
        return "none"
    scope = str(exc.detail.get("failoverScope") or "model")
    return scope if scope in {"model", "profile"} else "none"


def _provider_capability(
    provider: str,
    profiles: list[dict[str, Any]],
    default_profile_id: str,
) -> dict[str, Any]:
    ordered = sorted(
        profiles,
        key=lambda item: (-int(item.get("priority", 100)), item["id"]),
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
        "priority": int(selected.get("priority", 100)),
        "models": [
            {
                "id": model_id,
                "name": metadata.get("name") or model_id,
                "provider": provider,
                "priority": int(metadata.get("priority", 100)),
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
    *,
    request_id: str,
    attempt: int,
) -> None:
    request_data = model_to_dict(req)
    images = request_data.get("images") or []
    request_data["images"] = [
        {
            "media_type": image.get("media_type", ""),
            "source": "base64" if image.get("data") else "url",
            "encodedChars": len(str(image.get("data") or "")),
            "sha256": hashlib.sha256(
                str(image.get("data") or image.get("url") or "").encode("utf-8")
            ).hexdigest(),
        }
        for image in images
    ]
    append_conversation_event(
        settings,
        conversation_id,
        {
            "type": "gateway_request",
            "provider": profile.provider,
            "profileId": profile.id,
            "requestId": request_id,
            "runId": req.run_id,
            "attempt": attempt,
            "turnStatus": "running",
            "request": redact_value(request_data),
        },
    )


def _log_response(
    settings: Settings,
    conversation_id: str,
    req: AgentChatRequest,
    response: dict[str, Any],
    *,
    request_id: str,
    attempt: int,
) -> None:
    response_data = model_to_dict(response)
    append_conversation_event(
        settings,
        conversation_id,
        {
            "type": "gateway_response",
            "requestId": request_id,
            "runId": req.run_id,
            "attempt": attempt,
            "turnStatus": "completed",
            "response": redact_value(response_data),
        },
    )


def _log_error(
    settings: Settings,
    conversation_id: str,
    req: AgentChatRequest,
    error: Any,
    *,
    request_id: str,
    attempt: int,
    terminal: bool,
    status: str = "error",
    partial_content: str = "",
) -> None:
    append_conversation_event(
        settings,
        conversation_id,
        {
            "type": "gateway_error",
            "requestId": request_id,
            "runId": req.run_id,
            "attempt": attempt,
            "turnStatus": status if terminal else "fallback",
            "terminal": terminal,
            "error": redact_value(error),
            **(
                {"partialContent": redact_value(partial_content)}
                if partial_content
                else {}
            ),
        },
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
    _require_admin_origin(request, settings)
    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    sessions: AdminSessionStore = request.app.state.admin_sessions
    if token and sessions.verify(token):
        return
    if settings.profile_admin_token:
        expected = f"Bearer {settings.profile_admin_token}"
        if secrets.compare_digest(authorization, expected):
            return
    raise HTTPException(status_code=401, detail={"code": "PROFILE_ADMIN_AUTH_REQUIRED"})


def _require_service_auth(
    request: Request,
    service_token: Annotated[
        str | None,
        Header(alias=SERVICE_AUTH_HEADER, max_length=8192),
    ] = None,
    browser_api_key: Annotated[
        str | None,
        Header(alias=BROWSER_API_KEY_HEADER, max_length=8192),
    ] = None,
) -> None:
    settings: Settings = request.app.state.settings
    if service_token and settings.service_auth_mode == "required" and secrets.compare_digest(
        service_token,
        settings.service_auth_token,
    ):
        return
    if browser_api_key:
        sessions: AdminSessionStore = request.app.state.admin_sessions
        api_keys: BrowserApiKeyStore = request.app.state.browser_api_keys
        if sessions.verify(browser_api_key) or api_keys.verify(browser_api_key):
            return
    api_keys = request.app.state.browser_api_keys
    if settings.service_auth_mode == "disabled" and not api_keys.public()["configured"]:
        return
    raise HTTPException(
        status_code=401,
        detail={"code": "GATEWAY_SERVICE_AUTH_REQUIRED"},
    )


def _audit_actor(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _audit_request_id(request: Request) -> str:
    return request.headers.get("X-Request-Id", "").strip() or f"admin_{secrets.token_hex(8)}"


def _require_admin_origin(request: Request, settings: Settings) -> None:
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "localhost"
    if not settings.admin_allow_remote and not is_loopback:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_REMOTE_DISABLED"},
        )
    origin = request.headers.get("Origin", "").rstrip("/")
    if not origin:
        return
    if origin == str(request.base_url).rstrip("/"):
        return
    allowed = {item.rstrip("/") for item in settings.allow_origins if item != "*"}
    if origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_ORIGIN_NOT_ALLOWED"},
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
