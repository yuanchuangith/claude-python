"""FastAPI application for the thin ActionDesign model gateway."""

from __future__ import annotations
from .request_budget import prepare_provider_body, response_accounting

import asyncio
import hashlib
import ipaddress
import json
import secrets
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Annotated, Any, overload

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .admin_security import AdminSessionStore, BrowserApiKeyStore
from .audit_log import AuditLogStore
from .client_api_key_store import (
    ClientApiKeyCreateRequest,
    ClientApiKeyStore,
    ClientApiKeyStoreError,
)
from .identifiers import (
    CONVERSATION_ID_PATTERN,
    HISTORY_CONVERSATION_ID_PATTERN,
    SAFE_REQUEST_ID_PATTERN,
    validate_safe_request_id,
)
from .langsmith_forwarder import forward_trace_batch
from .mcp_browser_config import (
    AdminMcpStdioServerWriteRequest,
    BrowserMcpConfigurationService,
    BrowserMcpDeleteRequest,
    BrowserMcpEnabledRequest,
    BrowserMcpServerWriteRequest,
)
from .mcp_deployment import (
    McpDeployerClient,
    McpDeployerUnavailableError,
    McpDeploymentBusyError,
    McpDeploymentError,
    McpDeploymentJob,
    McpDeploymentManifest,
    McpDeploymentRecord,
    McpDeploymentUpsertRequest,
    McpDeploymentVersionConflictError,
)
from .mcp_proxy import McpProxy, McpProxyError, McpSecretResolver, McpServerConfig
from .mcp_server_store import (
    McpRuntimeConfigResponse,
    McpServerCandidateStaleError,
    McpServerConfigVersionConflictError,
    McpServerConnectionTestResponse,
    McpServerDeleteResponse,
    McpServerResponse,
    McpServerStore,
    McpServerStoreError,
)
from .models import (
    AccessKeyRotateRequest,
    AccessKeyRotateResponse,
    AccessKeyStatusResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminRuntimePolicyResponse,
    AgentChatHttpResponse,
    AgentChatRequest,
    AgentChatResponse,
    AgentModelsResponse,
    ApiProfileModel,
    ApiProfileResponse,
    ApiProfileWriteRequest,
    AuditLogListResponse,
    ConversationEventsResponse,
    ConversationListResponse,
    ConversationSealRequest,
    ConversationSealResponse,
    ConversationTranscriptResponse,
    DefaultModelUpdateRequest,
    DeleteModelResponse,
    DeleteProfileResponse,
    GatewayEventEnvelope,
    HealthResponse,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolListResponse,
    ProfileSecretStatusResponse,
    ProfileSecretWriteRequest,
    ProviderConnectionTestRequest,
    ProviderHealthResponse,
    ProviderModelDiscoveryRequest,
    ProviderModelDiscoveryResponse,
    RequestCorrelationIdentity,
    RequestIdentity,
    RequestLogDetailResponse,
    RequestLogListResponse,
    ToolResultRequest,
    ToolResultResponse,
    TraceBatchRequest,
    TraceBatchResponse,
)
from .profile_store import (
    ApiProfileStore,
    ProfileSecretStore,
    ProviderProfile,
    RouteCandidate,
)
from .protocol_compat import (
    AnthropicMessagesRequest,
    OpenAIChatCompletionRequest,
    ProtocolCompatibilityError,
    anthropic_error_response,
    anthropic_message_response,
    anthropic_stream_response,
    anthropic_to_agent_request,
    openai_chat_completion_response,
    openai_error_response,
    openai_models_response,
    openai_stream_response,
    openai_to_agent_request,
)
from .provider_http import (
    call_provider,
    discover_provider_models,
    probe_provider,
    stream_provider,
)
from .redaction import redact_secret_values, redact_value
from .request_log import RequestLogStore
from .secret_store import (
    ManagedSecretStore,
    ManagedSecretStoreError,
    SecretExposure,
    SecretResponse,
    SecretRotateRequest,
    SecretUpsertRequest,
)
from .session_log import (
    ToolResultStore,
    append_conversation_event,
    append_log,
    list_conversations,
    model_to_dict,
    read_conversation,
    reconstruct_conversation_transcript,
    resolve_correlation_identity,
    resolve_request_identity,
    seal_conversation,
    store_conversation_artifact,
)
from .settings import Settings

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
    Path(min_length=1, max_length=256, pattern=SAFE_REQUEST_ID_PATTERN),
]
ConversationIdPath = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=HISTORY_CONVERSATION_ID_PATTERN),
]
RuntimeConversationIdPath = Annotated[
    str,
    Path(min_length=36, max_length=36, pattern=CONVERSATION_ID_PATTERN),
]
ModelIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
SecretIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
McpServerIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    ),
]
ClientApiKeyIdPath = Annotated[
    str,
    Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
]


def create_app(
    settings: Settings | None = None,
    *,
    profile_secret_store: ProfileSecretStore | None = None,
    mcp_deployer_client: McpDeployerClient | None = None,
) -> FastAPI:
    gateway_settings = settings or Settings()
    profile_store = ApiProfileStore(gateway_settings, profile_secret_store)
    tool_results = ToolResultStore()
    mcp_server_store = McpServerStore(
        gateway_settings.mcp_server_store_path,
        [
            _mcp_proxy_config(server, gateway_settings.mcp_timeout_seconds)
            for server in gateway_settings.mcp_servers
        ],
    )
    legacy_exposures: dict[str, SecretExposure] = {}
    for server in mcp_server_store.list_configs():
        bindings = (
            server.streamable_http.secret_bindings
            if server.transport == "streamable_http" and server.streamable_http
            else server.stdio.secret_bindings
            if server.stdio
            else ()
        )
        exposure: SecretExposure = (
            "browser_header"
            if server.transport == "streamable_http"
            else "stdio_env"
        )
        for binding in bindings:
            previous_exposure = legacy_exposures.get(binding.secret_id)
            if previous_exposure is not None and previous_exposure != exposure:
                raise ManagedSecretStoreError(
                    "one legacy Secret cannot be shared by HTTP and stdio MCP bindings"
                )
            legacy_exposures[binding.secret_id] = exposure
    managed_secrets = ManagedSecretStore(
        gateway_settings.managed_secret_store_path,
        profile_store.secret_backend,
        legacy_exposures=legacy_exposures,
    )
    browser_mcp_config = BrowserMcpConfigurationService(
        mcp_server_store,
        managed_secrets,
    )
    mcp_deployer = (
        mcp_deployer_client
        or McpDeployerClient(
            gateway_settings.mcp_deployer_socket_path,
            gateway_settings.mcp_deployer_token,
        )
        if gateway_settings.mcp_deployments_enabled
        else None
    )
    initial_mcp_configs = (
        [
            server
            for server in mcp_server_store.list_configs()
            if server.transport == "stdio"
        ]
        if gateway_settings.mcp_proxy_enabled
        else []
    )
    mcp_proxy = _build_mcp_proxy(
        initial_mcp_configs,
        _snapshot_mcp_secrets(managed_secrets, initial_mcp_configs),
        gateway_settings,
    )
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
    client_api_keys = ClientApiKeyStore(gateway_settings.client_api_key_store_path)
    audit_root = FilePath(
        gateway_settings.log_root or gateway_settings.profile_store_path.parent
    )
    audit_logs = AuditLogStore(audit_root / "system" / "admin-audit.jsonl")
    mcp_mutation_lock = asyncio.Lock()
    app = FastAPI(title="ActionDesign Thin Gateway", version="3.1.0")
    app.state.settings = gateway_settings
    app.state.profile_store = profile_store
    app.state.tool_results = tool_results
    app.state.managed_secrets = managed_secrets
    app.state.mcp_server_store = mcp_server_store
    app.state.browser_mcp_config = browser_mcp_config
    app.state.mcp_deployer = mcp_deployer
    app.state.mcp_proxy = mcp_proxy
    app.state.request_logs = request_logs
    app.state.admin_sessions = admin_sessions
    app.state.browser_api_keys = browser_api_keys
    app.state.client_api_keys = client_api_keys
    app.state.audit_logs = audit_logs
    app.state.mcp_mutation_lock = mcp_mutation_lock

    def _managed_mcp_runtime(
        configs: list[McpServerConfig],
        secret_overrides: dict[str, str] | None = None,
    ) -> McpProxy:
        stdio_configs = [server for server in configs if server.transport == "stdio"]
        return _build_mcp_proxy(
            stdio_configs,
            _snapshot_mcp_secrets(
                managed_secrets,
                stdio_configs,
                overrides=secret_overrides,
            ),
            gateway_settings,
        )

    def _publish_managed_mcp_runtime(runtime: McpProxy) -> McpProxy:
        previous: McpProxy = app.state.mcp_proxy
        app.state.mcp_proxy = runtime
        return previous

    async def _connection_test_mcp_candidate(
        candidate: Any,
        *,
        secret_overrides: dict[str, str] | None = None,
    ) -> McpProxy:
        runtime = _managed_mcp_runtime(
            list(candidate.configs),
            secret_overrides=secret_overrides,
        )
        for server_id in candidate.server_ids:
            server = candidate.config(server_id)
            if server is None or not server.enabled or server.transport != "stdio":
                continue
            await runtime.list_tools(server_id)
        return runtime

    @app.middleware("http")
    async def admin_ui_security_headers(
        request: Request,
        call_next: Any,
    ) -> Response:
        response = await call_next(request)
        _apply_correlation_headers(response, _validated_header_identity(request))
        _apply_runtime_headers(response, _validated_runtime_header_identity(request))
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self' https:; img-src 'self' data:; font-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(
            (
                f"{API_PREFIX}/mcp-browser/",
                f"{API_PREFIX}/admin/mcp-servers",
                f"{API_PREFIX}/mcp-config/servers",
                f"{API_PREFIX}/admin/mcp-deployment",
                f"{API_PREFIX}/admin/mcp-deployments",
            )
        ):
            # Apply this after routing so validation and exception responses are
            # non-cacheable too; route-local Response headers only cover success.
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.exception_handler(HTTPException)
    async def correlated_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        identity = _validated_header_identity(request)
        runtime_identity = _validated_runtime_header_identity(request)
        detail = redact_secret_values(
            exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)},
            _settings_secret_values(gateway_settings),
        )
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        if identity is not None:
            detail.setdefault("requestId", identity.request_id)
            detail.setdefault("correlationId", identity.correlation_id)
        if runtime_identity is not None:
            detail.setdefault("conversationId", runtime_identity.conversation_id)
            detail.setdefault("runId", runtime_identity.run_id)
            detail.setdefault("sequence", 1)
        response = JSONResponse(status_code=exc.status_code, content={"detail": detail})
        _apply_correlation_headers(response, identity)
        _apply_runtime_headers(response, runtime_identity)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation_error(
        request: Request,
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
        identity = _validated_header_identity(request)
        runtime_identity = _validated_runtime_header_identity(request)
        content: dict[str, Any] = {"detail": errors}
        if identity is not None:
            content["requestId"] = identity.request_id
            content["correlationId"] = identity.correlation_id
        if runtime_identity is not None:
            content["conversationId"] = runtime_identity.conversation_id
            content["runId"] = runtime_identity.run_id
            content["sequence"] = 1
        response = JSONResponse(status_code=422, content=content)
        _apply_correlation_headers(response, identity)
        _apply_runtime_headers(response, runtime_identity)
        return response

    @app.exception_handler(Exception)
    async def sanitized_unhandled_error(request: Request, _exc: Exception) -> JSONResponse:
        identity = _validated_header_identity(request)
        runtime_identity = _validated_runtime_header_identity(request)
        detail: dict[str, Any] = {
            "code": "GATEWAY_INTERNAL_ERROR",
            "message": "Gateway request failed",
        }
        if identity is not None:
            detail["requestId"] = identity.request_id
            detail["correlationId"] = identity.correlation_id
        if runtime_identity is not None:
            detail["conversationId"] = runtime_identity.conversation_id
            detail["runId"] = runtime_identity.run_id
            detail["sequence"] = 1
        response = JSONResponse(status_code=500, content={"detail": detail})
        _apply_correlation_headers(response, identity)
        _apply_runtime_headers(response, runtime_identity)
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=gateway_settings.allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        **({"allow_private_network": True} if gateway_settings.allow_private_network else {}),
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
            "mcpProxyEnabled": gateway_settings.mcp_proxy_enabled,
            "publicBrowserMcpConfigEnabled": gateway_settings.public_browser_mcp_config,
            "mcpDeploymentsEnabled": gateway_settings.mcp_deployments_enabled,
            "fullConversationLogEnabled": gateway_settings.full_conversation_log_enabled,
        }

    def models_payload() -> dict[str, Any]:
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
            "supportedProtocolVersions": [PROTOCOL_VERSION, "1.1"],
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

    @app.get(
        f"{API_PREFIX}/models",
        response_model=AgentModelsResponse,
        dependencies=[Depends(_require_models_access)],
    )
    async def models() -> dict[str, Any]:
        return models_payload()

    @app.get("/v1/models")
    async def compatible_models(request: Request) -> Response:
        try:
            _require_client_api_key(request)
        except HTTPException as exc:
            body, status_code = openai_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        return JSONResponse(
            content=openai_models_response(models_payload(), created=int(time.time()))
        )

    @app.post("/v1/chat/completions")
    async def openai_chat_completions(request: Request) -> Response:
        try:
            _require_client_api_key(request)
        except HTTPException as exc:
            body, status_code = openai_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        try:
            payload = OpenAIChatCompletionRequest.model_validate(await request.json())
            agent_request = openai_to_agent_request(payload)
        except (ProtocolCompatibilityError, ValidationError, ValueError) as exc:
            body, status_code = openai_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        internal_request = _compatible_internal_request(request, agent_request)
        if payload.stream:
            try:
                gateway_response = _chat_stream(
                    agent_request,
                    internal_request,
                    None,
                    gateway_settings,
                    profile_store,
                )
            except Exception as exc:
                body, status_code = openai_error_response(exc)
                return JSONResponse(status_code=status_code, content=body)
            routes = _routes_for_request(agent_request, None, profile_store)
            return StreamingResponse(
                openai_stream_response(
                    gateway_response.body_iterator,
                    model=routes[0].model.id,
                    include_usage=bool(
                        payload.stream_options and payload.stream_options.include_usage
                    ),
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store"},
            )
        gateway_headers = Response()
        try:
            result = await _chat(
                agent_request,
                internal_request,
                None,
                gateway_settings,
                profile_store,
                gateway_headers,
            )
        except Exception as exc:
            body, status_code = openai_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        return JSONResponse(
            content=openai_chat_completion_response(_agent_response_payload(result)),
            headers=_selected_response_headers(gateway_headers),
        )

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> Response:
        try:
            _require_client_api_key(request)
        except HTTPException as exc:
            body, status_code = anthropic_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        if request.headers.get("anthropic-version", "") != "2023-06-01":
            body, status_code = anthropic_error_response(
                ProtocolCompatibilityError(
                    "invalid_anthropic_version",
                    "anthropic-version must be 2023-06-01",
                    param="anthropic-version",
                )
            )
            return JSONResponse(status_code=status_code, content=body)
        try:
            payload = AnthropicMessagesRequest.model_validate(await request.json())
            agent_request = anthropic_to_agent_request(payload)
        except (ProtocolCompatibilityError, ValidationError, ValueError) as exc:
            body, status_code = anthropic_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        internal_request = _compatible_internal_request(request, agent_request)
        if payload.stream:
            try:
                gateway_response = _chat_stream(
                    agent_request,
                    internal_request,
                    None,
                    gateway_settings,
                    profile_store,
                )
            except Exception as exc:
                body, status_code = anthropic_error_response(exc)
                return JSONResponse(status_code=status_code, content=body)
            routes = _routes_for_request(agent_request, None, profile_store)
            return StreamingResponse(
                anthropic_stream_response(
                    gateway_response.body_iterator,
                    model=routes[0].model.id,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store"},
            )
        gateway_headers = Response()
        try:
            result = await _chat(
                agent_request,
                internal_request,
                None,
                gateway_settings,
                profile_store,
                gateway_headers,
            )
        except Exception as exc:
            body, status_code = anthropic_error_response(exc)
            return JSONResponse(status_code=status_code, content=body)
        return JSONResponse(
            content=anthropic_message_response(_agent_response_payload(result)),
            headers=_selected_response_headers(gateway_headers),
        )

    @app.post(
        f"{API_PREFIX}/chat",
        response_model=AgentChatHttpResponse,
        response_model_exclude_unset=True,
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
        response_model=AgentChatHttpResponse,
        response_model_exclude_unset=True,
        dependencies=[Depends(_require_service_auth)],
    )
    async def mimo_chat(req: AgentChatRequest, request: Request, response: Response) -> dict[str, Any]:
        return await _chat(req, request, "mimo", gateway_settings, profile_store, response)

    @app.post(
        f"{API_PREFIX}/claude-code/chat",
        response_model=AgentChatHttpResponse,
        response_model_exclude_unset=True,
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
        identity = resolve_correlation_identity(request, req)
        _apply_correlation_headers(response, identity)
        duplicate, tool_log = tool_results.store(
            gateway_settings,
            conversation_id,
            req,
            request_id=identity.request_id,
        )
        append_conversation_event(
            gateway_settings,
            conversation_id,
            {
                "type": "tool_result",
                **tool_log,
            },
        )
        return {
            "success": True,
            "message": "recorded",
            "duplicate": duplicate,
            "conversationId": conversation_id,
            "runId": req.run_id,
            "requestId": identity.request_id,
            "correlationId": identity.correlation_id,
            "sequence": 1,
        }

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
        request_id = _audit_request_id(request)
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
        f"{API_PREFIX}/mcp/{{server_id}}/tools",
        response_model=McpToolListResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def mcp_tools(
        server_id: McpServerIdPath,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        identity = resolve_correlation_identity(request, header_only=True)
        _apply_correlation_headers(response, identity)
        _require_mcp_origin(request, gateway_settings)
        _require_stdio_mcp_server(mcp_server_store, server_id)
        _require_mcp_enabled(gateway_settings)
        _require_mcp_config_version(request, mcp_server_store, server_id)
        proxy: McpProxy = request.app.state.mcp_proxy
        try:
            result = await proxy.list_tools(server_id)
            _log_mcp_tools(gateway_settings, server_id, identity, result, None)
            return result
        except McpProxyError as exc:
            _log_mcp_tools(gateway_settings, server_id, identity, None, exc)
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get(
        f"{API_PREFIX}/mcp/{{server_id}}/config",
        response_model=McpRuntimeConfigResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def mcp_runtime_config(
        server_id: McpServerIdPath,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        identity = resolve_correlation_identity(request, header_only=True)
        _apply_correlation_headers(response, identity)
        _require_mcp_origin(request, gateway_settings)
        public = mcp_server_store.get_public(
            server_id,
            managed_secrets,
            proxy_enabled=gateway_settings.mcp_proxy_enabled,
        )
        if public is None:
            raise HTTPException(
                status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"}
            )
        return {
            key: public[key]
            for key in (
                "id",
                "transport",
                "executionMode",
                "authMode",
                "browserDirectUrl",
                "enabled",
                "proxyEnabled",
                "configVersion",
                "activeVersion",
                "connectionStatus",
            )
        }

    @app.get(
        f"{API_PREFIX}/mcp/{{server_id}}/connection-config",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_service_auth)],
    )
    async def mcp_connection_config(
        server_id: McpServerIdPath,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        """Runtime-only projection using the existing session/client/service auth.

        Unlike the management projection, this omits plaintext Secret bindings
        and stdio commands. Final HTTP headers exist only for browser execution.
        """
        identity = resolve_correlation_identity(request, header_only=True)
        _apply_correlation_headers(response, identity)
        _require_mcp_origin(request, gateway_settings)
        projected = browser_mcp_config.get_server(server_id)
        if projected is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
        response.headers["Cache-Control"] = "no-store"
        return _browser_contract_projection(projected)

    @app.post(
        f"{API_PREFIX}/mcp/{{server_id}}/call",
        response_model=McpToolCallResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def mcp_call(
        server_id: McpServerIdPath,
        req: McpToolCallRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        resolve_request_identity(request, req)
        identity = resolve_correlation_identity(request, req)
        _apply_correlation_headers(response, identity)
        _require_mcp_origin(request, gateway_settings)
        _require_stdio_mcp_server(mcp_server_store, server_id)
        _require_mcp_enabled(gateway_settings)
        _require_mcp_config_version(request, mcp_server_store, server_id)
        proxy: McpProxy = request.app.state.mcp_proxy
        try:
            result = await proxy.call_tool(server_id, req.tool_name, req.arguments)
        except McpProxyError as exc:
            _log_mcp_call(
                gateway_settings,
                server_id,
                identity.request_id,
                req,
                None,
                exc,
            )
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        result.update(
            {
                "conversationId": req.conversation_id,
                "runId": req.run_id,
                "requestId": identity.request_id,
                "correlationId": identity.correlation_id,
                "sequence": 1,
            }
        )
        _log_mcp_call(
            gateway_settings,
            server_id,
            identity.request_id,
            req,
            result,
            None,
        )
        return result

    @app.post(
        f"{API_PREFIX}/telemetry/langsmith/runs/batch",
        response_model=TraceBatchResponse,
        dependencies=[Depends(_require_service_auth)],
    )
    async def langsmith_batch(
        req: TraceBatchRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        resolve_request_identity(request, req)
        identity = resolve_correlation_identity(request, req)
        _apply_correlation_headers(response, identity)
        conversation_id = req.conversation_id
        run_id = req.run_id
        batch_id = f"trace_batch_{secrets.token_hex(8)}"
        secret_values = _settings_secret_values(gateway_settings)
        append_log(
            gateway_settings,
            batch_id,
            "langsmith-trace-batches.jsonl",
            redact_secret_values(model_to_dict(req), secret_values),
        )
        trace_events = [
            redact_secret_values(model_to_dict(event), secret_values)
            for event in req.events
        ]
        for trace_event in trace_events:
            metadata = trace_event.get("metadata") if isinstance(trace_event, dict) else None
            conversation_id = (
                str(metadata.get("conversationId") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if conversation_id:
                append_conversation_event(
                    gateway_settings,
                    conversation_id,
                    {
                        "type": "trace",
                        "requestId": identity.request_id,
                        "correlationId": identity.correlation_id,
                        "runId": run_id,
                        "trace": redact_value(trace_event),
                    },
                )
        if not gateway_settings.langsmith_enabled or not gateway_settings.langsmith_api_key:
            response = {
                "exportBatchId": batch_id,
                "conversationId": conversation_id,
                "runId": run_id,
                "requestId": identity.request_id,
                "correlationId": identity.correlation_id,
                "sequence": req.sequence,
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
            "conversationId": conversation_id,
            "runId": run_id,
            "requestId": identity.request_id,
            "correlationId": identity.correlation_id,
            "sequence": req.sequence,
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

    @app.post(
        f"{API_PREFIX}/admin/local-session",
        response_model=AdminLoginResponse,
    )
    async def admin_local_session(request: Request) -> dict[str, Any]:
        """Issue a short admin session for the explicitly configured local CPM origin."""
        _require_local_default_origin(request, gateway_settings)
        if not gateway_settings.admin_password:
            raise HTTPException(status_code=503, detail={"code": "ADMIN_LOGIN_DISABLED"})
        actor = _audit_actor(request)
        result, status = admin_sessions.login(gateway_settings.admin_password, actor=actor)
        audit_logs.record(
            action="admin_local_session",
            success=result is not None,
            actor=actor,
            request_id=f"admin_local_{secrets.token_hex(8)}",
            details={"result": status},
        )
        if result is None:
            raise HTTPException(status_code=503, detail={"code": "ADMIN_LOCAL_SESSION_UNAVAILABLE"})
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
        request_id = _audit_request_id(request)
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
            request_id=request_id,
            details={"masked": result["masked"]},
        )
        return result

    @app.get(f"{API_PREFIX}/admin/client-api-keys")
    @app.get("/admin/client-api-keys")
    async def list_client_api_keys(request: Request) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return {"items": client_api_keys.list_public()}

    @app.post(f"{API_PREFIX}/admin/client-api-keys", status_code=201)
    @app.post("/admin/client-api-keys", status_code=201)
    async def create_client_api_key(
        req: ClientApiKeyCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        try:
            result = client_api_keys.create(req)
        except (ValueError, ClientApiKeyStoreError) as exc:
            raise HTTPException(
                status_code=503 if isinstance(exc, ClientApiKeyStoreError) else 422,
                detail={"code": "CLIENT_API_KEY_CREATE_FAILED"},
            ) from exc
        audit_logs.record(
            action="client_api_key_create",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "keyId": result["id"],
                "name": result["name"],
                "prefix": result["prefix"],
            },
        )
        return result

    @app.delete(f"{API_PREFIX}/admin/client-api-keys/{{key_id}}")
    @app.delete("/admin/client-api-keys/{key_id}")
    async def delete_client_api_key(
        key_id: ClientApiKeyIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        try:
            client_api_keys.delete(key_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "CLIENT_API_KEY_NOT_FOUND"},
            ) from exc
        except ClientApiKeyStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "CLIENT_API_KEY_STORE_UNAVAILABLE"},
            ) from exc
        audit_logs.record(
            action="client_api_key_delete",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"keyId": key_id},
        )
        return {"keyId": key_id, "deleted": True}

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
        conversation_id: Annotated[str, Query(alias="conversationId", max_length=128)] = "",
        request_id: Annotated[str, Query(alias="requestId", max_length=256)] = "",
        run_id: Annotated[str, Query(alias="runId", max_length=256)] = "",
        provider: Annotated[str, Query(max_length=64)] = "",
        model: Annotated[str, Query(max_length=256)] = "",
        status: Annotated[str, Query(pattern=r"^(open|completed|aborted)?$")] = "",
        incomplete_only: Annotated[bool, Query(alias="incompleteOnly")] = False,
        updated_from: Annotated[datetime | None, Query(alias="updatedFrom")] = None,
        updated_to: Annotated[datetime | None, Query(alias="updatedTo")] = None,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        updated_from, updated_to = _validated_time_range(updated_from, updated_to)
        items, total = list_conversations(
            gateway_settings,
            limit=limit,
            offset=offset,
            conversation_id=conversation_id,
            request_id=request_id,
            run_id=run_id,
            provider=provider,
            model=model,
            status=status,
            incomplete_only=incomplete_only,
            updated_from=updated_from,
            updated_to=updated_to,
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
        event_type: Annotated[str, Query(alias="type", max_length=128)] = "",
        request_id: Annotated[str, Query(alias="requestId", max_length=256)] = "",
        run_id: Annotated[str, Query(alias="runId", max_length=256)] = "",
        turn_status: Annotated[str, Query(alias="turnStatus", max_length=64)] = "",
        started_at: Annotated[datetime | None, Query(alias="startedAt")] = None,
        ended_at: Annotated[datetime | None, Query(alias="endedAt")] = None,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        started_at, ended_at = _validated_time_range(started_at, ended_at)
        items, total = read_conversation(
            gateway_settings,
            conversation_id,
            limit=limit,
            offset=offset,
            event_type=event_type,
            request_id=request_id,
            run_id=run_id,
            turn_status=turn_status,
            started_at=started_at,
            ended_at=ended_at,
        )
        return {
            "conversationId": conversation_id,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def _conversation_transcript_payload(
        conversation_id: str,
        request: Request,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        messages, total, incomplete_reason = reconstruct_conversation_transcript(
            gateway_settings,
            conversation_id,
            limit=limit,
            offset=offset,
        )
        return {
            "conversationId": conversation_id,
            "messages": messages,
            "total": total,
            "limit": limit,
            "offset": offset,
            "incompleteReason": incomplete_reason,
        }

    @app.get(
        f"{API_PREFIX}/admin/conversations/{{conversation_id}}/transcript",
        response_model=ConversationTranscriptResponse,
    )
    async def admin_get_conversation_transcript(
        conversation_id: ConversationIdPath,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _conversation_transcript_payload(
            conversation_id,
            request,
            limit,
            offset,
        )

    @app.get(
        f"{API_PREFIX}/admin/conversations/{{conversation_id}}/reconstruction",
        response_model=ConversationTranscriptResponse,
    )
    async def admin_get_conversation_reconstruction(
        conversation_id: ConversationIdPath,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await _conversation_transcript_payload(
            conversation_id,
            request,
            limit,
            offset,
        )

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
        request_id = _audit_request_id(request)
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
        f"{API_PREFIX}/admin/profiles",
        response_model=list[ApiProfileResponse],
    )
    async def list_profiles(request: Request) -> list[dict[str, Any]]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.list_public()

    @app.get(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}",
        response_model=ApiProfileResponse,
    )
    async def get_profile(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.get_public(profile_id)

    @app.get(
        f"{API_PREFIX}/admin/providers",
        response_model=list[ApiProfileResponse],
    )
    async def list_providers(request: Request) -> list[dict[str, Any]]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.list_public()

    @app.get(
        f"{API_PREFIX}/admin/providers/{{provider_id}}",
        response_model=ApiProfileResponse,
    )
    async def get_provider(
        provider_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.get_public(provider_id)

    @app.put(
        f"{API_PREFIX}/admin/providers/{{provider_id}}",
        response_model=ApiProfileResponse,
    )
    async def put_provider(
        provider_id: ProfileIdPath,
        req: ApiProfileWriteRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        if provider_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "PROVIDER_ID_MISMATCH"})
        result = profile_store.upsert(req)
        audit_logs.record(
            action="provider_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "providerId": result["id"],
                "provider": result["provider"],
                "protocol": result["protocol"],
                "providerPath": result["providerPath"],
                "keyUpdated": bool(req.api_key),
            },
        )
        return result

    @app.delete(
        f"{API_PREFIX}/admin/providers/{{provider_id}}",
        response_model=DeleteProfileResponse,
    )
    async def delete_provider(
        provider_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        success = profile_store.delete(provider_id)
        audit_logs.record(
            action="provider_delete",
            success=success,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"providerId": provider_id},
        )
        return {"success": success}

    @app.post(
        f"{API_PREFIX}/admin/providers/{{provider_id}}/models/discover",
        response_model=ProviderModelDiscoveryResponse,
    )
    async def discover_models_for_provider(
        provider_id: ProfileIdPath,
        req: ProviderModelDiscoveryRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        submitted_secret = req.api_key.get_secret_value()
        try:
            profile = profile_store.build_model_discovery_profile(provider_id, req)
            result = await discover_provider_models(profile)
        except Exception as exc:
            safe_exc = _sanitized_provider_exception(
                exc,
                (*_settings_secret_values(gateway_settings), submitted_secret),
            )
            detail = _error_detail(safe_exc)
            audit_logs.record(
                action="provider_models_discover",
                success=False,
                actor=_audit_actor(request),
                request_id=request_id,
                details={
                    "providerId": provider_id,
                    "protocol": req.protocol,
                    "errorCode": detail["code"],
                },
            )
            raise safe_exc
        audit_logs.record(
            action="provider_models_discover",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "providerId": provider_id,
                "protocol": req.protocol,
                "modelCount": len(result["models"]),
                "truncated": result["truncated"],
            },
        )
        return result

    @app.get(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/models",
        response_model=list[ApiProfileModel],
    )
    async def list_profile_models(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> list[dict[str, Any]]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.list_models(profile_id)

    @app.get(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/models/{{model_id:path}}",
        response_model=ApiProfileModel,
    )
    async def get_profile_model(
        profile_id: ProfileIdPath,
        model_id: ModelIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.get_model(profile_id, model_id)

    @app.post(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/models",
        response_model=ApiProfileModel,
    )
    async def create_profile_model(
        profile_id: ProfileIdPath,
        req: ApiProfileModel,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        result = profile_store.upsert_model(profile_id, req)
        audit_logs.record(
            action="model_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "modelId": req.id},
        )
        return result

    @app.put(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/models/{{model_id:path}}",
        response_model=ApiProfileModel,
    )
    async def put_profile_model(
        profile_id: ProfileIdPath,
        model_id: ModelIdPath,
        req: ApiProfileModel,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        if model_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "MODEL_ID_MISMATCH"})
        result = profile_store.upsert_model(profile_id, req)
        audit_logs.record(
            action="model_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "modelId": model_id},
        )
        return result

    @app.delete(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/models/{{model_id:path}}",
        response_model=DeleteModelResponse,
    )
    async def delete_profile_model(
        profile_id: ProfileIdPath,
        model_id: ModelIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        success = profile_store.delete_model(profile_id, model_id)
        audit_logs.record(
            action="model_delete",
            success=success,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "modelId": model_id},
        )
        return {"success": success}

    @app.put(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/default-model",
        response_model=ApiProfileResponse,
    )
    async def put_default_model(
        profile_id: ProfileIdPath,
        req: DefaultModelUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        result = profile_store.set_default_model(profile_id, req.model_id)
        audit_logs.record(
            action="default_model_update",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "modelId": req.model_id},
        )
        return result

    @app.get(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/secret",
        response_model=ProfileSecretStatusResponse,
    )
    async def get_profile_secret(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.secret_status(profile_id)

    @app.put(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/secret",
        response_model=ProfileSecretStatusResponse,
    )
    async def put_profile_secret(
        profile_id: ProfileIdPath,
        req: ProfileSecretWriteRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        result = profile_store.update_secret(profile_id, req.value)
        audit_logs.record(
            action="provider_secret_update",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "configured": result["configured"]},
        )
        return result

    @app.post(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/secret/rotate",
        response_model=ProfileSecretStatusResponse,
    )
    async def rotate_profile_secret(
        profile_id: ProfileIdPath,
        req: ProfileSecretWriteRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        result = profile_store.rotate_secret(profile_id, req.value)
        audit_logs.record(
            action="provider_secret_rotate",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "configured": result["configured"]},
        )
        return result

    @app.delete(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/secret",
        response_model=ProfileSecretStatusResponse,
    )
    async def revoke_profile_secret(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        result = profile_store.revoke_secret(profile_id)
        audit_logs.record(
            action="provider_secret_revoke",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "configured": False},
        )
        return result

    @app.post(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/connection-test",
        response_model=ProviderHealthResponse,
    )
    async def test_profile_connection(
        profile_id: ProfileIdPath,
        req: ProviderConnectionTestRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        request_id = _audit_request_id(request)
        profile = profile_store.get_profile(profile_id, require_ready=True)
        model = req.model or profile.default_model
        started = time.monotonic()
        try:
            await probe_provider(profile, model)
        except Exception as exc:
            detail = _error_detail(exc)
            result = profile_store.record_health(
                profile_id,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=detail["code"],
                message="Connection failed",
            )
            audit_logs.record(
                action="provider_connection_test",
                success=False,
                actor=_audit_actor(request),
                request_id=request_id,
                details={"profileId": profile_id, "model": model, "errorCode": detail["code"]},
            )
            return result
        result = profile_store.record_health(
            profile_id,
            success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            message="Connection succeeded",
        )
        audit_logs.record(
            action="provider_connection_test",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id, "model": model},
        )
        return result

    @app.get(
        f"{API_PREFIX}/admin/profiles/{{profile_id}}/health",
        response_model=ProviderHealthResponse,
    )
    async def get_profile_health(
        profile_id: ProfileIdPath,
        request: Request,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.health(profile_id)

    @app.get(
        f"{API_PREFIX}/admin/channels/health",
        response_model=list[ProviderHealthResponse],
    )
    async def list_channel_health(request: Request) -> list[dict[str, Any]]:
        _require_profile_admin(request, gateway_settings)
        return profile_store.list_health()

    def _require_mcp_deployer() -> McpDeployerClient:
        if not gateway_settings.mcp_deployments_enabled or mcp_deployer is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "MCP_DEPLOYMENTS_DISABLED"},
            )
        return mcp_deployer

    def _deployer_http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, McpDeploymentVersionConflictError):
            return HTTPException(
                status_code=409,
                detail={
                    "code": "MCP_DEPLOYMENT_VERSION_CONFLICT",
                    "id": exc.deployment_id,
                    "expected": exc.expected,
                    "current": exc.current,
                },
            )
        if isinstance(exc, McpDeploymentBusyError):
            return HTTPException(status_code=409, detail={"code": "MCP_DEPLOYMENT_BUSY"})
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
        if isinstance(exc, McpDeployerUnavailableError):
            return HTTPException(status_code=503, detail={"code": "MCP_DEPLOYER_UNAVAILABLE"})
        return HTTPException(status_code=400, detail={"code": "MCP_DEPLOYMENT_INVALID"})

    async def _deployment_records() -> dict[str, dict[str, Any]]:
        if not gateway_settings.mcp_deployments_enabled or mcp_deployer is None:
            return {}
        try:
            payload = await mcp_deployer.request("GET", "/v1/deployments")
        except McpDeploymentError:
            return {}
        assert isinstance(payload, list)
        return {
            str(item.get("id")): item
            for item in payload
            if isinstance(item, dict) and item.get("id")
        }

    async def _enrich_deployment_projection(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = await _deployment_records()
        for item in items:
            ref = item.get("deploymentRef")
            if not isinstance(ref, dict):
                continue
            record = records.get(str(ref.get("deploymentId") or item.get("id")))
            item["deploymentVersion"] = int(record.get("deploymentVersion", 0)) if record else 0
            item["deploymentStatus"] = str(record.get("status", "unavailable")) if record else "unavailable"
        return items

    def _legacy_mcp_config_projection(item: dict[str, Any]) -> dict[str, Any]:
        """Return the exact plaintext DTO consumed by the browser Agent."""

        fields = (
            "id",
            "displayName",
            "source",
            "editable",
            "transport",
            "executionMode",
            "browserSupported",
            "expectedServerName",
            "timeoutSeconds",
            "enabled",
            "configVersion",
            "configStatus",
            "errorCode",
            "streamableHttp",
            "managedBy",
            "deploymentRef",
        )
        result = {name: item[name] for name in fields}
        for name in ("deploymentVersion", "deploymentStatus"):
            if name in item:
                result[name] = item[name]
        transport = result.get("streamableHttp")
        if isinstance(transport, dict):
            result["streamableHttp"] = {
                name: transport[name]
                for name in (
                    "url",
                    "headers",
                    "staticHeaders",
                    "secretBindings",
                    "protocolVersion",
                    "sseReadTimeoutSeconds",
                )
            }
        return result

    def _browser_contract_projection(item: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "displayName",
            "source",
            "editable",
            "transport",
            "executionMode",
            "browserSupported",
            "expectedServerName",
            "timeoutSeconds",
            "enabled",
            "configVersion",
            "configStatus",
            "errorCode",
            "streamableHttp",
        )
        result = {name: item[name] for name in fields}
        transport = result.get("streamableHttp")
        if isinstance(transport, dict):
            result["streamableHttp"] = {
                name: transport[name]
                for name in (
                    "url",
                    "headers",
                    "protocolVersion",
                    "sseReadTimeoutSeconds",
                )
            }
        return result

    def _deployment_server_config(job: McpDeploymentJob) -> McpServerConfig:
        if job.manifest is None or not job.revision:
            raise ValueError("deployment job has no prepared runtime")
        registration = job.request.registration
        common: dict[str, Any] = {
            "id": job.deployment_id,
            "displayName": registration.display_name,
            "transport": job.manifest.transport,
            "expectedServerName": registration.expected_server_name,
            "timeoutSeconds": registration.timeout_seconds,
            "enabled": registration.enabled,
            "deploymentRef": {
                "deploymentId": job.deployment_id,
                "revision": job.revision,
            },
        }
        if job.manifest.transport == "streamable_http":
            common.update(
                {
                    "executionMode": "browser_direct",
                    "authMode": "backend_secret" if registration.secret_bindings else "none",
                    "streamableHttp": {
                        "url": (
                            gateway_settings.mcp_deployment_public_base_url.rstrip("/")
                            + f"/mcp-runtime/{job.deployment_id}/mcp"
                        ),
                        "headers": registration.static_headers,
                        "secretBindings": [
                            {
                                "secretId": binding.secret_id,
                                "target": "header",
                                "name": binding.name,
                                "valuePrefix": binding.value_prefix,
                            }
                            for binding in registration.secret_bindings
                        ],
                        "sseReadTimeoutSeconds": 300.0,
                    },
                }
            )
        else:
            common.update(
                {
                    "executionMode": "gateway_proxy",
                    "authMode": "none",
                    "stdio": {
                        "command": str(gateway_settings.mcp_stdio_bridge_path),
                        "args": [
                            "--deployment-id",
                            job.deployment_id,
                            "--revision",
                            job.revision,
                            "--job-id",
                            job.id,
                        ],
                        "cwd": str(gateway_settings.mcp_stdio_bridge_path.parent),
                        "secretBindings": [],
                    },
                }
            )
        return McpServerConfig.model_validate(common)

    def _deployment_record_server(record: McpDeploymentRecord) -> McpServerConfig:
        registration = record.registration
        common: dict[str, Any] = {
            "id": record.id,
            "displayName": registration.display_name,
            "transport": record.manifest.transport,
            "expectedServerName": registration.expected_server_name,
            "timeoutSeconds": registration.timeout_seconds,
            "enabled": registration.enabled,
            "deploymentRef": {
                "deploymentId": record.id,
                "revision": record.active_revision,
            },
        }
        if record.manifest.transport == "streamable_http":
            common.update(
                {
                    "executionMode": "browser_direct",
                    "authMode": "backend_secret" if registration.secret_bindings else "none",
                    "streamableHttp": {
                        "url": gateway_settings.mcp_deployment_public_base_url.rstrip("/")
                        + f"/mcp-runtime/{record.id}/mcp",
                        "headers": registration.static_headers,
                        "secretBindings": [
                            {
                                "secretId": binding.secret_id,
                                "target": "header",
                                "name": binding.name,
                                "valuePrefix": binding.value_prefix,
                            }
                            for binding in registration.secret_bindings
                        ],
                        "sseReadTimeoutSeconds": 300.0,
                    },
                }
            )
        else:
            common.update(
                {
                    "executionMode": "gateway_proxy",
                    "authMode": "none",
                    "stdio": {
                        "command": str(gateway_settings.mcp_stdio_bridge_path),
                        "args": [
                            "--deployment-id",
                            record.id,
                            "--revision",
                            record.active_revision,
                        ],
                        "cwd": str(gateway_settings.mcp_stdio_bridge_path.parent),
                        "secretBindings": [],
                    },
                }
            )
        return McpServerConfig.model_validate(common)

    def _preserve_deployment_managed_metadata(
        runtime_server: McpServerConfig,
        current_server: McpServerConfig,
    ) -> McpServerConfig:
        if runtime_server.transport != current_server.transport:
            return runtime_server
        updates: dict[str, Any] = {
            "display_name": current_server.display_name,
            "expected_server_name": current_server.expected_server_name,
            "timeout_seconds": current_server.timeout_seconds,
            "enabled": current_server.enabled,
        }
        if runtime_server.transport == "streamable_http":
            updates["streamable_http"] = current_server.streamable_http
            updates["auth_mode"] = current_server.auth_mode
        return runtime_server.model_copy(update=updates)

    @app.get(
        f"{API_PREFIX}/admin/mcp-deployments",
        response_model=list[dict[str, Any]],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def list_mcp_deployments() -> list[dict[str, Any]]:
        client = _require_mcp_deployer()
        try:
            payload = await client.request("GET", "/v1/deployments")
        except Exception as exc:
            raise _deployer_http_error(exc) from exc
        assert isinstance(payload, list)
        return payload

    @app.get(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def get_mcp_deployment(deployment_id: McpServerIdPath) -> dict[str, Any]:
        try:
            payload = await _require_mcp_deployer().request(
                "GET", f"/v1/deployments/{deployment_id}"
            )
        except Exception as exc:
            raise _deployer_http_error(exc) from exc
        assert isinstance(payload, dict)
        return payload

    @app.put(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}",
        status_code=202,
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def put_mcp_deployment(
        deployment_id: McpServerIdPath,
        req: McpDeploymentUpsertRequest,
        request: Request,
    ) -> dict[str, Any]:
        if deployment_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "MCP_DEPLOYMENT_ID_MISMATCH"})
        if mcp_server_store.source_for(deployment_id) == "environment":
            raise HTTPException(status_code=409, detail={"code": "MCP_ENVIRONMENT_SERVER_CONFLICT"})
        current = mcp_server_store.get_config(deployment_id)
        current_version = mcp_server_store.effective_version(deployment_id) if current else 0
        if req.expected_config_version != current_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MCP_CONFIG_VERSION_CONFLICT",
                    "expected": req.expected_config_version,
                    "current": current_version,
                },
            )
        try:
            if req.source.credential_secret_id:
                managed_secrets.require_exposure(
                    req.source.credential_secret_id,
                    {"deployment_only"},
                )
            for binding in req.registration.secret_bindings:
                managed_secrets.require_exposure(
                    binding.secret_id,
                    {"browser_header"},
                )
            if req.manifest is not None:
                required_scope = (
                    "stdio_env"
                    if req.manifest.transport == "stdio"
                    else "deployment_only"
                )
                for binding in req.manifest.environment:
                    managed_secrets.require_exposure(
                        binding.secret_id,
                        {required_scope},
                    )
        except Exception as exc:
            raise _managed_secret_http_error(exc) from exc
        try:
            payload = await _require_mcp_deployer().request(
                "PUT",
                f"/v1/deployments/{deployment_id}",
                json_body=req.model_dump(mode="json", by_alias=True),
            )
        except Exception as exc:
            raise _deployer_http_error(exc) from exc
        audit_logs.record(
            action="mcp_deployment_start",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": deployment_id,
                "sourceType": req.source.type,
                "sourceHash": hashlib.sha256(
                    json.dumps(
                        req.source.model_dump(mode="json", by_alias=True),
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "expectedDeploymentVersion": req.expected_deployment_version,
                "expectedConfigVersion": req.expected_config_version,
            },
        )
        assert isinstance(payload, dict)
        return payload

    @app.get(
        f"{API_PREFIX}/admin/mcp-deployment-jobs/{{job_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def get_mcp_deployment_job(job_id: str) -> dict[str, Any]:
        try:
            payload = await _require_mcp_deployer().request("GET", f"/v1/jobs/{job_id}")
        except Exception as exc:
            raise _deployer_http_error(exc) from exc
        assert isinstance(payload, dict)
        return payload

    @app.get(
        f"{API_PREFIX}/admin/mcp-deployment-jobs/{{job_id}}/candidate-config",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def get_mcp_deployment_candidate_config(
        job_id: str,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            raw = await _require_mcp_deployer().request("GET", f"/v1/jobs/{job_id}")
            assert isinstance(raw, dict)
            job = McpDeploymentJob.model_validate(raw)
            if job.status != "awaiting_browser_validation" or job.manifest is None:
                raise ValueError("deployment candidate is not ready")
            if job.manifest.transport == "stdio":
                return {
                    "id": job.deployment_id,
                    "transport": "stdio",
                    "browserSupported": False,
                    "executionMode": "gateway_proxy",
                    "revision": job.revision,
                }
            headers = dict(job.request.registration.static_headers)
            bindings: list[dict[str, str]] = []
            for binding in job.request.registration.secret_bindings:
                managed_secrets.require_exposure(binding.secret_id, {"browser_header"})
                value = managed_secrets.resolve_value(binding.secret_id)
                headers[binding.name] = f"{binding.value_prefix}{value}"
                bindings.append(
                    {
                        "secretId": binding.secret_id,
                        "name": binding.name,
                        "valuePrefix": binding.value_prefix,
                        "value": value,
                    }
                )
            return {
                "id": job.deployment_id,
                "transport": "streamable_http",
                "browserSupported": True,
                "executionMode": "browser_direct",
                "revision": job.revision,
                "streamableHttp": {
                    "url": gateway_settings.mcp_deployment_public_base_url.rstrip("/") + job.candidate_path,
                    "headers": headers,
                    "staticHeaders": job.request.registration.static_headers,
                    "secretBindings": bindings,
                    "protocolVersion": "2025-06-18",
                    "sseReadTimeoutSeconds": 300.0,
                },
            }
        except Exception as exc:
            raise _deployer_http_error(exc) from exc

    @app.post(
        f"{API_PREFIX}/admin/mcp-deployment-jobs/{{job_id}}/browser-validation",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def validate_mcp_deployment_candidate(
        job_id: str,
        req: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        client = _require_mcp_deployer()
        try:
            raw = await client.request("GET", f"/v1/jobs/{job_id}")
            assert isinstance(raw, dict)
            job = McpDeploymentJob.model_validate(raw)
            success = req.get("success") is True
            if success and job.manifest and job.manifest.transport == "stdio":
                candidate_server = _deployment_server_config(job)
                runtime = _build_mcp_proxy(
                    [candidate_server],
                    _snapshot_mcp_secrets(managed_secrets, []),
                    gateway_settings,
                )
                await runtime.list_tools(job.deployment_id)
            if not success:
                rejected = await client.request("POST", f"/v1/jobs/{job_id}/reject")
                assert isinstance(rejected, dict)
                return rejected
            await client.request("POST", f"/v1/jobs/{job_id}/promote")
            server = _deployment_server_config(job)
            previous = mcp_server_store.get_config(job.deployment_id)
            candidate = mcp_server_store.create_upsert_candidate(
                server,
                expected_config_version=job.request.expected_config_version,
            )
            if server.transport == "stdio":
                runtime = _managed_mcp_runtime(list(candidate.configs))
                browser_mcp_config.commit_runtime_candidate(
                    candidate,
                    runtime,
                    _publish_managed_mcp_runtime,
                )
            else:
                browser_mcp_config.commit_metadata_candidate(candidate)
            config_version = mcp_server_store.effective_version(job.deployment_id)
            try:
                committed = await client.request(
                    "POST",
                    f"/v1/jobs/{job_id}/commit?configVersion={config_version}",
                )
            except Exception:
                restore_candidate = (
                    mcp_server_store.create_upsert_candidate(
                        previous,
                        expected_config_version=config_version,
                    )
                    if previous is not None
                    else mcp_server_store.create_delete_candidate(
                        job.deployment_id,
                        expected_config_version=config_version,
                    )
                )
                if any(item.transport == "stdio" for item in restore_candidate.configs):
                    restore_runtime = _managed_mcp_runtime(list(restore_candidate.configs))
                    browser_mcp_config.commit_runtime_candidate(
                        restore_candidate,
                        restore_runtime,
                        _publish_managed_mcp_runtime,
                    )
                else:
                    browser_mcp_config.commit_metadata_candidate(restore_candidate)
                await client.request("POST", f"/v1/jobs/{job_id}/reject")
                raise
        except Exception as exc:
            try:
                await client.request("POST", f"/v1/jobs/{job_id}/reject")
            except Exception:
                pass
            raise _deployer_http_error(exc) from exc
        audit_logs.record(
            action="mcp_deployment_promote",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": job.deployment_id,
                "jobId": job.id,
                "revision": job.revision,
                "configVersion": config_version,
                "toolListHash": str(req.get("toolListHash") or "")[:128],
            },
        )
        assert isinstance(committed, dict)
        return committed

    @app.get(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}/logs",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def get_mcp_deployment_logs(
        deployment_id: McpServerIdPath,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            payload = await _require_mcp_deployer().request(
                "GET", f"/v1/deployments/{deployment_id}/logs"
            )
        except Exception as exc:
            raise _deployer_http_error(exc) from exc
        assert isinstance(payload, dict)
        return payload

    @app.put(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}/enabled",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def set_mcp_deployment_enabled(
        deployment_id: McpServerIdPath,
        req: BrowserMcpEnabledRequest,
        request: Request,
    ) -> dict[str, Any]:
        if req.expected_deployment_version is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "MCP_DEPLOYMENT_VERSION_REQUIRED"},
            )
        async with mcp_mutation_lock:
            current = mcp_server_store.get_config(deployment_id)
            if current is None or current.deployment_ref is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"},
                )
            current_config_version = mcp_server_store.effective_version(deployment_id)
            if req.expected_config_version != current_config_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MCP_CONFIG_VERSION_CONFLICT",
                        "expected": req.expected_config_version,
                        "current": current_config_version,
                    },
                )
            deployer = _require_mcp_deployer()
            try:
                deployed = await deployer.request(
                    "PUT",
                    f"/v1/deployments/{deployment_id}/enabled",
                    json_body={
                        "enabled": req.enabled,
                        "expectedDeploymentVersion": req.expected_deployment_version,
                        "nextConfigVersion": req.expected_config_version + 1,
                    },
                )
                assert isinstance(deployed, dict)
                candidate = browser_mcp_config.create_enabled_candidate(
                    deployment_id,
                    req.enabled,
                    expected_config_version=req.expected_config_version,
                )
                try:
                    if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                        runtime = _managed_mcp_runtime(list(candidate.configs))
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                        )
                    else:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                except Exception:
                    await deployer.request(
                        "PUT",
                        f"/v1/deployments/{deployment_id}/enabled",
                        json_body={
                            "enabled": current.enabled,
                            "expectedDeploymentVersion": int(
                                deployed["deploymentVersion"]
                            ),
                            "nextConfigVersion": req.expected_config_version,
                        },
                    )
                    raise
            except Exception as exc:
                if isinstance(exc, McpDeploymentError):
                    raise _deployer_http_error(exc) from exc
                raise _mcp_server_http_error(exc) from exc
            projected = browser_mcp_config.get_server(deployment_id)
            assert projected is not None
            result = (await _enrich_deployment_projection([projected]))[0]
        audit_logs.record(
            action="mcp_deployment_enabled",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": deployment_id,
                "enabled": req.enabled,
                "deploymentVersion": result["deploymentVersion"],
                "configVersion": result["configVersion"],
            },
        )
        return result

    @app.post(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}/rollback",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def rollback_mcp_deployment(
        deployment_id: McpServerIdPath,
        req: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        expected_deployment_version = int(req.get("expectedDeploymentVersion", 0))
        expected_config_version = int(req.get("expectedConfigVersion", 0))
        async with mcp_mutation_lock:
            current = mcp_server_store.get_config(deployment_id)
            if current is None or current.deployment_ref is None:
                raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
            current_config_version = mcp_server_store.effective_version(deployment_id)
            if expected_config_version != current_config_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MCP_CONFIG_VERSION_CONFLICT",
                        "expected": expected_config_version,
                        "current": current_config_version,
                    },
                )
            try:
                raw = await _require_mcp_deployer().request(
                    "POST",
                    f"/v1/deployments/{deployment_id}/rollback",
                    json_body={
                        "expectedDeploymentVersion": expected_deployment_version,
                        "nextConfigVersion": expected_config_version + 1,
                    },
                )
                assert isinstance(raw, dict)
                try:
                    deployment_record = McpDeploymentRecord.model_validate(raw)
                    updated = _deployment_record_server(deployment_record)
                    updated = _preserve_deployment_managed_metadata(updated, current)
                    candidate = mcp_server_store.create_upsert_candidate(
                        updated,
                        expected_config_version=expected_config_version,
                    )
                    if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                        runtime = _managed_mcp_runtime(list(candidate.configs))
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                        )
                    else:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                except Exception:
                    try:
                        await _require_mcp_deployer().request(
                            "POST",
                            f"/v1/deployments/{deployment_id}/rollback",
                            json_body={
                                "expectedDeploymentVersion": int(raw["deploymentVersion"]),
                                "nextConfigVersion": expected_config_version,
                            },
                        )
                    except Exception:
                        pass
                    raise
            except Exception as exc:
                raise _deployer_http_error(exc) if isinstance(exc, McpDeploymentError) else _mcp_server_http_error(exc)
        audit_logs.record(
            action="mcp_deployment_rollback",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": deployment_id,
                "deploymentVersion": raw["deploymentVersion"],
                "configVersion": mcp_server_store.effective_version(deployment_id),
                "revision": raw["activeRevision"],
            },
        )
        return raw

    @app.delete(
        f"{API_PREFIX}/admin/mcp-deployments/{{deployment_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    async def delete_mcp_deployment(
        deployment_id: McpServerIdPath,
        req: BrowserMcpDeleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        if req.expected_deployment_version is None:
            raise HTTPException(status_code=422, detail={"code": "MCP_DEPLOYMENT_VERSION_REQUIRED"})
        async with mcp_mutation_lock:
            configured = mcp_server_store.get_config(deployment_id)
            if configured is None or configured.deployment_ref is None:
                raise HTTPException(status_code=404, detail={"code": "MCP_DEPLOYMENT_NOT_FOUND"})
            current_config_version = mcp_server_store.effective_version(deployment_id)
            if req.expected_config_version != current_config_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MCP_CONFIG_VERSION_CONFLICT",
                        "expected": req.expected_config_version,
                        "current": current_config_version,
                    },
                )
            client = _require_mcp_deployer()
            try:
                prepared = await client.request(
                    "DELETE",
                    (
                        f"/v1/deployments/{deployment_id}"
                        f"?expectedDeploymentVersion={req.expected_deployment_version}"
                    ),
                )
                assert isinstance(prepared, dict)
                try:
                    candidate = mcp_server_store.create_delete_candidate(
                        deployment_id,
                        expected_config_version=req.expected_config_version,
                    )
                    if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                        runtime = _managed_mcp_runtime(list(candidate.configs))
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                        )
                    else:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                    await client.request(
                        "DELETE",
                        (
                            f"/v1/deployments/{deployment_id}"
                            f"?expectedDeploymentVersion={prepared['deploymentVersion']}&finalize=true"
                        ),
                    )
                except Exception:
                    if mcp_server_store.get_config(deployment_id) is None:
                        restore = mcp_server_store.create_upsert_candidate(
                            configured,
                            expected_config_version=0,
                        )
                        if browser_mcp_config.candidate_requires_stdio_runtime(restore):
                            runtime = _managed_mcp_runtime(list(restore.configs))
                            browser_mcp_config.commit_runtime_candidate(
                                restore,
                                runtime,
                                _publish_managed_mcp_runtime,
                            )
                        else:
                            browser_mcp_config.commit_metadata_candidate(restore)
                    try:
                        await client.request(
                            "PUT",
                            f"/v1/deployments/{deployment_id}/enabled",
                            json_body={
                                "enabled": configured.enabled,
                                "expectedDeploymentVersion": int(prepared["deploymentVersion"]),
                                "nextConfigVersion": req.expected_config_version,
                            },
                        )
                    except Exception:
                        pass
                    raise
            except Exception as exc:
                raise _deployer_http_error(exc) if isinstance(exc, McpDeploymentError) else _mcp_server_http_error(exc)
        audit_logs.record(
            action="mcp_deployment_delete",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": deployment_id,
                "deploymentVersion": req.expected_deployment_version,
                "configVersion": req.expected_config_version,
            },
        )
        return {"id": deployment_id, "deleted": True}

    @app.get(
        f"{API_PREFIX}/admin/runtime-policy",
        response_model=AdminRuntimePolicyResponse,
    )
    async def get_runtime_policy(request: Request) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        return {
            "profileAllowedHosts": sorted(set(gateway_settings.profile_allowed_hosts)),
            "providerPaths": {
                "openai": "/v1/chat/completions",
                "openai-responses": "/v1/responses",
                "claude": "/v1/messages",
            },
            "retryPolicy": {
                "mode": "retryable-failover",
                "attemptsPerRoute": 1,
                "retryableErrorsOnly": True,
                "partialStreamFailover": False,
            },
        }

    @app.get(
        f"{API_PREFIX}/mcp-browser/servers",
        response_model=list[dict[str, Any]],
    )
    async def list_browser_mcp_servers(
        request: Request,
        response: Response,
    ) -> list[dict[str, Any]]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        items = await _enrich_deployment_projection(browser_mcp_config.list_servers())
        return [_browser_contract_projection(item) for item in items]

    @app.get(
        f"{API_PREFIX}/mcp-browser/servers/{{server_id}}",
        response_model=dict[str, Any],
    )
    async def get_browser_mcp_server(
        server_id: McpServerIdPath,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        result = browser_mcp_config.get_server(server_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
        enriched = (await _enrich_deployment_projection([result]))[0]
        return _browser_contract_projection(enriched)

    @app.put(
        f"{API_PREFIX}/mcp-browser/servers/{{server_id}}",
        response_model=dict[str, Any],
    )
    async def put_browser_mcp_server(
        server_id: McpServerIdPath,
        req: BrowserMcpServerWriteRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        if server_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "MCP_SERVER_ID_MISMATCH"})
        async with mcp_mutation_lock:
            try:
                result = browser_mcp_config.upsert_server(req)
            except Exception as exc:
                raise _browser_mcp_config_http_error(exc) from exc
        audit_logs.record(
            action="browser_mcp_server_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": server_id,
                "source": "managed",
                "transport": "streamable_http",
                "configVersion": result["configVersion"],
                "entry": "mcp-browser",
                "configHash": browser_mcp_config.configuration_hash(server_id),
            },
        )
        return _browser_contract_projection(result)

    @app.put(
        f"{API_PREFIX}/mcp-browser/servers/{{server_id}}/enabled",
        response_model=dict[str, Any],
    )
    async def set_browser_mcp_server_enabled(
        server_id: McpServerIdPath,
        req: BrowserMcpEnabledRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        async with mcp_mutation_lock:
            try:
                current = mcp_server_store.get_config(server_id)
                if current is not None and current.deployment_ref is not None:
                    if req.expected_deployment_version is None:
                        raise ValueError("expectedDeploymentVersion is required")
                    current_config_version = mcp_server_store.effective_version(server_id)
                    if req.expected_config_version != current_config_version:
                        raise McpServerConfigVersionConflictError(
                            server_id,
                            req.expected_config_version,
                            current_config_version,
                        )
                    deployer = _require_mcp_deployer()
                    deployed = await deployer.request(
                        "PUT",
                        f"/v1/deployments/{server_id}/enabled",
                        json_body={
                            "enabled": req.enabled,
                            "expectedDeploymentVersion": req.expected_deployment_version,
                            "nextConfigVersion": req.expected_config_version + 1,
                        },
                    )
                    assert isinstance(deployed, dict)
                    candidate = browser_mcp_config.create_enabled_candidate(
                        server_id,
                        req.enabled,
                        expected_config_version=req.expected_config_version,
                    )
                    try:
                        if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                            runtime = _managed_mcp_runtime(list(candidate.configs))
                            browser_mcp_config.commit_runtime_candidate(
                                candidate,
                                runtime,
                                _publish_managed_mcp_runtime,
                            )
                        else:
                            browser_mcp_config.commit_metadata_candidate(candidate)
                    except Exception:
                        await deployer.request(
                            "PUT",
                            f"/v1/deployments/{server_id}/enabled",
                            json_body={
                                "enabled": current.enabled,
                                "expectedDeploymentVersion": int(deployed["deploymentVersion"]),
                                "nextConfigVersion": req.expected_config_version,
                            },
                        )
                        raise
                    projected = browser_mcp_config.get_server(server_id)
                    assert projected is not None
                    result = (await _enrich_deployment_projection([projected]))[0]
                else:
                    result = browser_mcp_config.set_enabled(server_id, req)
            except Exception as exc:
                if isinstance(exc, McpDeploymentError):
                    raise _deployer_http_error(exc) from exc
                raise _browser_mcp_config_http_error(exc) from exc
        audit_logs.record(
            action="browser_mcp_server_enabled",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": server_id,
                "enabled": req.enabled,
                "configVersion": result["configVersion"],
                "entry": "mcp-browser",
                "configHash": browser_mcp_config.configuration_hash(server_id),
            },
        )
        return _browser_contract_projection(result)

    @app.delete(
        f"{API_PREFIX}/mcp-browser/servers/{{server_id}}",
        response_model=McpServerDeleteResponse,
    )
    async def delete_browser_mcp_server(
        server_id: McpServerIdPath,
        req: BrowserMcpDeleteRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        previous_hash = ""
        async with mcp_mutation_lock:
            try:
                previous_hash = browser_mcp_config.configuration_hash(server_id)
                current = mcp_server_store.get_config(server_id)
                if current is not None and current.deployment_ref is not None:
                    if req.expected_deployment_version is None:
                        raise ValueError("expectedDeploymentVersion is required")
                    current_config_version = mcp_server_store.effective_version(server_id)
                    if req.expected_config_version != current_config_version:
                        raise McpServerConfigVersionConflictError(
                            server_id,
                            req.expected_config_version,
                            current_config_version,
                        )
                    deployer = _require_mcp_deployer()
                    prepared = await deployer.request(
                        "DELETE",
                        (
                            f"/v1/deployments/{server_id}"
                            f"?expectedDeploymentVersion={req.expected_deployment_version}"
                        ),
                    )
                    assert isinstance(prepared, dict)
                    candidate = browser_mcp_config.create_delete_candidate(
                        server_id,
                        expected_config_version=req.expected_config_version,
                    )
                    try:
                        if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                            runtime = _managed_mcp_runtime(list(candidate.configs))
                            browser_mcp_config.commit_runtime_candidate(
                                candidate,
                                runtime,
                                _publish_managed_mcp_runtime,
                            )
                        else:
                            browser_mcp_config.commit_metadata_candidate(candidate)
                        final_version = int(prepared["deploymentVersion"])
                        await deployer.request(
                            "DELETE",
                            (
                                f"/v1/deployments/{server_id}"
                                f"?expectedDeploymentVersion={final_version}&finalize=true"
                            ),
                        )
                    except Exception:
                        if mcp_server_store.get_config(server_id) is None:
                            restore = mcp_server_store.create_upsert_candidate(
                                current,
                                expected_config_version=0,
                            )
                            if browser_mcp_config.candidate_requires_stdio_runtime(restore):
                                runtime = _managed_mcp_runtime(list(restore.configs))
                                browser_mcp_config.commit_runtime_candidate(
                                    restore,
                                    runtime,
                                    _publish_managed_mcp_runtime,
                                )
                            else:
                                browser_mcp_config.commit_metadata_candidate(restore)
                        try:
                            await deployer.request(
                                "PUT",
                                f"/v1/deployments/{server_id}/enabled",
                                json_body={
                                    "enabled": current.enabled,
                                    "expectedDeploymentVersion": int(prepared["deploymentVersion"]),
                                    "nextConfigVersion": req.expected_config_version,
                                },
                            )
                        except Exception:
                            pass
                        raise
                    result = {"id": server_id, "deleted": True}
                else:
                    result = browser_mcp_config.delete_server(server_id, req)
            except Exception as exc:
                if isinstance(exc, McpDeploymentError):
                    raise _deployer_http_error(exc) from exc
                raise _browser_mcp_config_http_error(exc) from exc
        audit_logs.record(
            action="browser_mcp_server_delete",
            success=True,
            actor=_audit_actor(request),
            request_id=_audit_request_id(request),
            details={
                "serverId": server_id,
                "entry": "mcp-browser",
                "configVersion": req.expected_config_version,
                "configHash": previous_hash,
            },
        )
        return result

    @app.post(
        f"{API_PREFIX}/mcp-browser/servers/{{server_id}}/connection-test",
        response_model=dict[str, Any],
    )
    async def test_browser_mcp_server_configuration(
        server_id: McpServerIdPath,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _prepare_browser_mcp_request(request, response, gateway_settings)
        try:
            return browser_mcp_config.test_configuration(server_id)
        except Exception as exc:
            raise _browser_mcp_config_http_error(exc) from exc

    @app.get(
        f"{API_PREFIX}/admin/mcp-servers",
        response_model=list[dict[str, Any]],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.get(
        f"{API_PREFIX}/mcp-config/servers",
        response_model=list[dict[str, Any]],
        dependencies=[Depends(_require_client_api_key)],
    )
    async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
        items = await _enrich_deployment_projection(browser_mcp_config.list_servers())
        if request.url.path.startswith(f"{API_PREFIX}/mcp-config/"):
            return [_legacy_mcp_config_projection(item) for item in items]
        return items

    @app.get(
        f"{API_PREFIX}/admin/mcp-servers/{{server_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.get(
        f"{API_PREFIX}/mcp-config/servers/{{server_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_client_api_key)],
    )
    async def get_mcp_server(
        server_id: McpServerIdPath,
        request: Request,
    ) -> dict[str, Any]:
        result = browser_mcp_config.get_server(server_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
        enriched = (await _enrich_deployment_projection([result]))[0]
        if request.url.path.startswith(f"{API_PREFIX}/mcp-config/"):
            return _legacy_mcp_config_projection(enriched)
        return enriched

    @app.put(
        f"{API_PREFIX}/admin/mcp-servers/{{server_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.put(
        f"{API_PREFIX}/mcp-config/servers/{{server_id}}",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_client_api_key)],
    )
    async def put_mcp_server(
        server_id: McpServerIdPath,
        req: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        if server_id != req.get("id"):
            raise HTTPException(status_code=400, detail={"code": "MCP_SERVER_ID_MISMATCH"})
        await mcp_mutation_lock.acquire()
        try:
            if req.get("transport") == "streamable_http":
                payload = BrowserMcpServerWriteRequest.model_validate(req)
                result = browser_mcp_config.upsert_server(payload)
                saved = mcp_server_store.get_config(server_id)
                assert saved is not None
            else:
                payload = AdminMcpStdioServerWriteRequest.model_validate(req)
                candidate = browser_mcp_config.create_stdio_candidate(payload)
                runtime = await _connection_test_mcp_candidate(candidate)
                saved = candidate.config(server_id)
                assert saved is not None
                browser_mcp_config.commit_runtime_candidate(
                    candidate,
                    runtime,
                    _publish_managed_mcp_runtime,
                    connection_status=(
                        "healthy" if saved.enabled else "disconnected"
                    ),
                )
                result = browser_mcp_config.get_server(server_id)
                assert result is not None
        except Exception as exc:
            raise _mcp_server_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="mcp_server_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "serverId": saved.id,
                "source": "managed",
                "transport": saved.transport,
                "enabled": saved.enabled,
                "secretIds": _mcp_server_secret_ids(saved),
                "configVersion": result["configVersion"],
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "configHash": browser_mcp_config.configuration_hash(server_id),
            },
        )
        if request.url.path.startswith(f"{API_PREFIX}/mcp-config/"):
            return _legacy_mcp_config_projection(result)
        return result

    @app.put(
        f"{API_PREFIX}/admin/mcp-servers/{{server_id}}/enabled",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.put(
        f"{API_PREFIX}/mcp-config/servers/{{server_id}}/enabled",
        response_model=dict[str, Any],
        dependencies=[Depends(_require_client_api_key)],
    )
    async def set_mcp_server_enabled(
        server_id: McpServerIdPath,
        req: BrowserMcpEnabledRequest,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        await mcp_mutation_lock.acquire()
        try:
            configured = mcp_server_store.get_config(server_id)
            if configured is None:
                raise KeyError(server_id)
            if configured.deployment_ref is not None:
                raise PermissionError(
                    "deployment-managed lifecycle must use MCP deployment API"
                )
            if configured.transport == "streamable_http":
                result = browser_mcp_config.set_enabled(server_id, req)
                saved = mcp_server_store.get_config(server_id)
                assert saved is not None
            else:
                candidate = browser_mcp_config.create_enabled_candidate(
                    server_id,
                    req.enabled,
                    expected_config_version=req.expected_config_version,
                )
                runtime = await _connection_test_mcp_candidate(candidate)
                saved = candidate.config(server_id)
                assert saved is not None
                browser_mcp_config.commit_runtime_candidate(
                    candidate,
                    runtime,
                    _publish_managed_mcp_runtime,
                    connection_status=(
                        "healthy" if req.enabled else "disconnected"
                    ),
                )
                result = browser_mcp_config.get_server(server_id)
                assert result is not None
        except Exception as exc:
            raise _mcp_server_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="mcp_server_enabled",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "serverId": saved.id,
                "enabled": saved.enabled,
                "configVersion": result["configVersion"],
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "configHash": browser_mcp_config.configuration_hash(server_id),
            },
        )
        if request.url.path.startswith(f"{API_PREFIX}/mcp-config/"):
            return _legacy_mcp_config_projection(result)
        return result

    @app.delete(
        f"{API_PREFIX}/admin/mcp-servers/{{server_id}}",
        response_model=McpServerDeleteResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.delete(
        f"{API_PREFIX}/mcp-config/servers/{{server_id}}",
        response_model=McpServerDeleteResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def delete_mcp_server(
        server_id: McpServerIdPath,
        req: BrowserMcpDeleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        await mcp_mutation_lock.acquire()
        try:
            configured = mcp_server_store.get_config(server_id)
            if configured is None:
                raise KeyError(server_id)
            if configured.deployment_ref is not None:
                raise PermissionError(
                    "deployment-managed lifecycle must use MCP deployment API"
                )
            deleted_version = mcp_server_store.effective_version(server_id)
            deleted_hash = browser_mcp_config.configuration_hash(server_id)
            if configured.transport == "streamable_http":
                browser_mcp_config.delete_server(server_id, req)
            else:
                candidate = browser_mcp_config.create_delete_candidate(
                    server_id,
                    expected_config_version=req.expected_config_version,
                )
                runtime = _managed_mcp_runtime(list(candidate.configs))
                browser_mcp_config.commit_runtime_candidate(
                    candidate,
                    runtime,
                    _publish_managed_mcp_runtime,
                    connection_status="disconnected",
                )
        except Exception as exc:
            raise _mcp_server_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="mcp_server_delete",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "serverId": server_id,
                "configVersion": deleted_version,
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "configHash": deleted_hash,
            },
        )
        return {"id": server_id, "deleted": True}

    @app.post(
        f"{API_PREFIX}/admin/mcp-servers/{{server_id}}/connection-test",
        response_model=McpServerConnectionTestResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.post(
        f"{API_PREFIX}/mcp-config/servers/{{server_id}}/connection-test",
        response_model=McpServerConnectionTestResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def test_mcp_server_connection(
        server_id: McpServerIdPath,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        started = time.monotonic()
        configured = mcp_server_store.get_config(server_id)
        if configured is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
        if configured.transport == "streamable_http":
            checked = browser_mcp_config.test_configuration(server_id)
            result = _mcp_connection_result(
                server_id,
                started,
                error_code=(
                    "" if checked["status"] == "valid" else checked["errorCode"]
                ),
                listed=(
                    {
                        "serverName": configured.expected_server_name,
                        "serverVersion": "configuration-only",
                        "tools": [],
                    }
                    if checked["status"] == "valid"
                    else None
                ),
            )
            mcp_server_store.set_connection_status(server_id, "disconnected")
        elif not gateway_settings.mcp_proxy_enabled:
            result = _mcp_connection_result(
                server_id,
                started,
                error_code="MCP_PROXY_DISABLED",
            )
        else:
            try:
                listed = await request.app.state.mcp_proxy.list_tools(server_id)
                result = _mcp_connection_result(server_id, started, listed=listed)
                mcp_server_store.set_connection_status(server_id, "healthy")
            except McpProxyError as exc:
                result = _mcp_connection_result(
                    server_id,
                    started,
                    error_code=exc.code,
                )
                try:
                    mcp_server_store.set_connection_status(server_id, "unhealthy")
                except KeyError:
                    pass
        audit_logs.record(
            action="mcp_server_connection_test",
            success=result["status"] == "healthy",
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "serverId": server_id,
                "status": result["status"],
                "errorCode": result["errorCode"],
                "toolCount": result["toolCount"],
            },
        )
        return result

    @app.get(
        f"{API_PREFIX}/admin/secrets",
        response_model=list[SecretResponse],
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.get(
        f"{API_PREFIX}/mcp-config/secrets",
        response_model=list[SecretResponse],
        dependencies=[Depends(_require_client_api_key)],
    )
    async def list_managed_secrets(request: Request) -> list[dict[str, Any]]:
        return managed_secrets.list_public()

    @app.get(
        f"{API_PREFIX}/admin/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.get(
        f"{API_PREFIX}/mcp-config/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def get_managed_secret(
        secret_id: SecretIdPath,
        request: Request,
    ) -> dict[str, Any]:
        result = managed_secrets.get(secret_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "SECRET_NOT_FOUND"})
        return result

    @app.put(
        f"{API_PREFIX}/admin/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.put(
        f"{API_PREFIX}/mcp-config/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def put_managed_secret(
        secret_id: SecretIdPath,
        req: SecretUpsertRequest,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        if secret_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "SECRET_ID_MISMATCH"})
        await mcp_mutation_lock.acquire()
        try:
            candidate = None
            runtime = None
            if req.value:
                candidate = browser_mcp_config.create_secret_candidate([secret_id])
                if (
                    candidate.server_ids
                    and browser_mcp_config.candidate_requires_stdio_runtime(candidate)
                ):
                    runtime = await _connection_test_mcp_candidate(
                        candidate,
                        secret_overrides={secret_id: req.value},
                    )
            rollback_snapshot = (
                managed_secrets.snapshot_for_rollback(secret_id)
                if candidate is not None and candidate.server_ids
                else None
            )
            result = managed_secrets.upsert(req)
            if candidate is not None and candidate.server_ids:
                try:
                    if runtime is None:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                    else:
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                            connection_status="healthy",
                        )
                except Exception:
                    assert rollback_snapshot is not None
                    managed_secrets.restore_snapshot(rollback_snapshot)
                    raise
        except Exception as exc:
            if isinstance(exc, (McpProxyError, McpServerStoreError)):
                raise _mcp_server_http_error(exc) from exc
            raise _managed_secret_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="secret_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "secretId": secret_id,
                "kind": result["kind"],
                "configured": result["configured"],
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "serverConfigs": [
                    {
                        "serverId": affected_id,
                        "configVersion": mcp_server_store.effective_version(affected_id),
                        "configHash": browser_mcp_config.configuration_hash(affected_id),
                    }
                    for affected_id in (candidate.server_ids if candidate else ())
                ],
            },
        )
        return result

    @app.post(
        f"{API_PREFIX}/admin/secrets/{{secret_id}}/rotate",
        response_model=SecretResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.post(
        f"{API_PREFIX}/mcp-config/secrets/{{secret_id}}/rotate",
        response_model=SecretResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def rotate_managed_secret(
        secret_id: SecretIdPath,
        req: SecretRotateRequest,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        await mcp_mutation_lock.acquire()
        try:
            candidate = browser_mcp_config.create_secret_candidate([secret_id])
            runtime = None
            if (
                candidate.server_ids
                and browser_mcp_config.candidate_requires_stdio_runtime(candidate)
            ):
                runtime = await _connection_test_mcp_candidate(
                    candidate,
                    secret_overrides={secret_id: req.value},
                )
            rollback_snapshot = (
                managed_secrets.snapshot_for_rollback(secret_id)
                if candidate.server_ids
                else None
            )
            result = managed_secrets.rotate(secret_id, req)
            if candidate.server_ids:
                try:
                    if runtime is None:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                    else:
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                            connection_status="healthy",
                        )
                except Exception:
                    assert rollback_snapshot is not None
                    managed_secrets.restore_snapshot(rollback_snapshot)
                    raise
        except Exception as exc:
            if isinstance(exc, (McpProxyError, McpServerStoreError)):
                raise _mcp_server_http_error(exc) from exc
            raise _managed_secret_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="secret_rotate",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "secretId": secret_id,
                "kind": result["kind"],
                "configured": True,
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "serverConfigs": [
                    {
                        "serverId": affected_id,
                        "configVersion": mcp_server_store.effective_version(affected_id),
                        "configHash": browser_mcp_config.configuration_hash(affected_id),
                    }
                    for affected_id in candidate.server_ids
                ],
            },
        )
        return result

    @app.delete(
        f"{API_PREFIX}/admin/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_profile_admin_dependency)],
    )
    @app.delete(
        f"{API_PREFIX}/mcp-config/secrets/{{secret_id}}",
        response_model=SecretResponse,
        dependencies=[Depends(_require_client_api_key)],
    )
    async def revoke_managed_secret(
        secret_id: SecretIdPath,
        request: Request,
    ) -> dict[str, Any]:
        request_id = _audit_request_id(request)
        await mcp_mutation_lock.acquire()
        try:
            candidate = browser_mcp_config.create_secret_candidate([secret_id])
            rollback_snapshot = (
                managed_secrets.snapshot_for_rollback(secret_id)
                if candidate.server_ids
                else None
            )
            result = managed_secrets.revoke(secret_id)
            if candidate.server_ids:
                try:
                    if browser_mcp_config.candidate_requires_stdio_runtime(candidate):
                        runtime = _managed_mcp_runtime(list(candidate.configs))
                        browser_mcp_config.commit_runtime_candidate(
                            candidate,
                            runtime,
                            _publish_managed_mcp_runtime,
                            connection_status="disconnected",
                        )
                    else:
                        browser_mcp_config.commit_metadata_candidate(candidate)
                except Exception:
                    assert rollback_snapshot is not None
                    managed_secrets.restore_snapshot(rollback_snapshot)
                    raise
        except Exception as exc:
            if isinstance(exc, McpServerStoreError):
                raise _mcp_server_http_error(exc) from exc
            raise _managed_secret_http_error(exc) from exc
        finally:
            mcp_mutation_lock.release()
        audit_logs.record(
            action="secret_revoke",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
            details={
                "secretId": secret_id,
                "kind": result["kind"],
                "configured": False,
                "entry": "admin" if request.url.path.startswith(f"{API_PREFIX}/admin/") else "mcp-config",
                "serverConfigs": [
                    {
                        "serverId": affected_id,
                        "configVersion": mcp_server_store.effective_version(affected_id),
                        "configHash": browser_mcp_config.configuration_hash(affected_id),
                    }
                    for affected_id in candidate.server_ids
                ],
            },
        )
        return result

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
        protocol: Annotated[str, Query(pattern=r"^(openai|claude|openai-responses)?$")] = "",
        status: Annotated[str, Query(pattern=r"^(success|error|cancelled)?$")] = "",
        conversation_id: Annotated[str, Query(alias="conversationId", max_length=128)] = "",
        request_id: Annotated[str, Query(alias="requestId", max_length=256)] = "",
        run_id: Annotated[str, Query(alias="runId", max_length=256)] = "",
        model: Annotated[str, Query(max_length=256)] = "",
        stream: Annotated[bool | None, Query()] = None,
        error_code: Annotated[str, Query(alias="errorCode", max_length=256)] = "",
        started_at: Annotated[datetime | None, Query(alias="startedAt")] = None,
        ended_at: Annotated[datetime | None, Query(alias="endedAt")] = None,
    ) -> dict[str, Any]:
        _require_profile_admin(request, gateway_settings)
        started_at, ended_at = _validated_time_range(started_at, ended_at)
        items, total = request_logs.list(
            limit=limit,
            offset=offset,
            provider=provider,
            profile_id=profile_id,
            protocol=protocol,
            status=status,
            conversation_id=conversation_id,
            request_id=request_id,
            run_id=run_id,
            model=model,
            stream=stream,
            error_code=error_code,
            started_at=started_at,
            ended_at=ended_at,
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
        request_id = _audit_request_id(request)
        if profile_id != req.id:
            raise HTTPException(status_code=400, detail={"code": "PROFILE_ID_MISMATCH"})
        response = profile_store.upsert(req)
        audit_logs.record(
            action="profile_upsert",
            success=True,
            actor=_audit_actor(request),
            request_id=request_id,
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
        request_id = _audit_request_id(request)
        success = profile_store.delete(profile_id)
        audit_logs.record(
            action="profile_delete",
            success=success,
            actor=_audit_actor(request),
            request_id=request_id,
            details={"profileId": profile_id},
        )
        return {"success": success}

    # Keep the static mount last so exact /admin/* JSON routes registered above
    # are not swallowed by StaticFiles.
    admin_static_root = FilePath(__file__).with_name("static") / "admin"
    app.mount(
        "/admin",
        StaticFiles(directory=str(admin_static_root), html=True),
        name="actiondesign-admin",
    )
    return app


def _validated_header_identity(request: Request) -> RequestCorrelationIdentity | None:
    payload = {
        "requestId": request.headers.get("X-Request-Id", "").strip(),
        "correlationId": request.headers.get("X-Correlation-Id", "").strip(),
        "retryOf": request.headers.get("X-Retry-Of", "").strip(),
        "parentRequestId": request.headers.get("X-Parent-Request-Id", "").strip(),
    }
    if not payload["requestId"] or not payload["correlationId"]:
        return None
    try:
        return RequestCorrelationIdentity.model_validate(payload)
    except ValidationError:
        return None


def _compatible_internal_request(
    request: Request,
    body: AgentChatRequest,
) -> Request:
    scope = dict(request.scope)
    removed = {
        b"authorization",
        b"x-api-key",
        BROWSER_API_KEY_HEADER.lower().encode("ascii"),
        b"x-conversation-id",
        b"x-agent-run-id",
        b"x-request-id",
        b"x-correlation-id",
        b"x-retry-of",
        b"x-parent-request-id",
    }
    headers = [
        (name, value)
        for name, value in scope.get("headers", [])
        if name.lower() not in removed
    ]
    headers.extend(
        [
            (b"x-conversation-id", body.conversation_id.encode("utf-8")),
            (b"x-agent-run-id", body.run_id.encode("utf-8")),
            (b"x-request-id", body.request_id.encode("utf-8")),
            (b"x-correlation-id", body.correlation_id.encode("utf-8")),
        ]
    )
    if body.retry_of:
        headers.append((b"x-retry-of", body.retry_of.encode("utf-8")))
    if body.parent_request_id:
        headers.append(
            (b"x-parent-request-id", body.parent_request_id.encode("utf-8"))
        )
    scope["headers"] = headers
    return Request(scope, request.receive)


def _agent_response_payload(result: dict[str, Any]) -> AgentChatResponse:
    return AgentChatResponse.model_validate(
        {
            "provider": result.get("provider", ""),
            "model": result.get("model", ""),
            "content": result.get("content", ""),
            "tool_calls": result.get("toolCalls", result.get("tool_calls", [])),
            "success": result.get("success", True),
            "error": result.get("error"),
            "code": result.get("code"),
            "duration_ms": result.get("durationMs", result.get("duration_ms", 0)),
            "usage": result.get("usage", {}),
        }
    )


def _selected_response_headers(response: Response) -> dict[str, str]:
    allowed = {
        "x-route-attempts",
        "x-actual-profile",
        "x-actual-model",
        "x-request-id",
        "x-correlation-id",
    }
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() in allowed
    }


def _validated_runtime_header_identity(request: Request) -> RequestIdentity | None:
    payload = {
        "conversationId": request.headers.get("X-Conversation-Id", "").strip(),
        "runId": request.headers.get("X-Agent-Run-Id", "").strip(),
    }
    if not payload["conversationId"] or not payload["runId"]:
        return None
    try:
        return RequestIdentity.model_validate(payload)
    except ValidationError:
        return None


def _correlation_headers(identity: RequestCorrelationIdentity) -> dict[str, str]:
    headers = {
        "X-Request-Id": identity.request_id,
        "X-Correlation-Id": identity.correlation_id,
    }
    if identity.retry_of:
        headers["X-Retry-Of"] = identity.retry_of
    if identity.parent_request_id:
        headers["X-Parent-Request-Id"] = identity.parent_request_id
    return headers


def _apply_correlation_headers(
    response: Response,
    identity: RequestCorrelationIdentity | None,
) -> None:
    if identity is None:
        return
    for name, value in _correlation_headers(identity).items():
        response.headers[name] = value


def _apply_runtime_headers(
    response: Response,
    identity: RequestIdentity | None,
) -> None:
    if identity is None:
        return
    response.headers["X-Conversation-Id"] = identity.conversation_id
    response.headers["X-Agent-Run-Id"] = identity.run_id


async def _chat(
    req: AgentChatRequest,
    request: Request,
    provider: str | None,
    settings: Settings,
    profiles: ApiProfileStore,
    response: Response,
) -> dict[str, Any]:
    conversation_id, _ = resolve_request_identity(request, req)
    identity = resolve_correlation_identity(request, req)
    _apply_correlation_headers(response, identity)
    routes = _routes_for_request(req, provider, profiles)
    request_id = identity.request_id
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
            secret_values = _provider_secret_values(settings, route.profile)
            result = redact_secret_values(
                await _call_route(routed_request, route.profile),
                secret_values,
            )
        except HTTPException as exc:
            safe_exc = _sanitized_provider_exception(exc, secret_values)
            if routed_request.protocol_version != "1.1" and isinstance(safe_exc.detail, dict):
                safe_exc.detail.pop("budget", None)
            detail = _error_detail(safe_exc)
            profiles.record_health(
                route.profile.id,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=detail["code"],
                message="Provider request failed",
            )
            next_index = _next_route_index(routes, index, route, safe_exc)
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
                http_status=safe_exc.status_code,
                stream=False,
                attempt=attempt,
                endpoint=request.url.path,
            )
            if next_index is None:
                response.headers["X-Route-Attempts"] = str(attempt)
                raise safe_exc from exc
            index = next_index
            continue
        except Exception as exc:
            detail = _error_detail(exc)
            profiles.record_health(
                route.profile.id,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=detail["code"],
                message="Provider request failed",
            )
            _log_error(
                settings,
                conversation_id,
                routed_request,
                detail,
                request_id=request_id,
                attempt=attempt,
                terminal=True,
            )
            request_logs.record(
                request_id=request_id,
                request=routed_request,
                profile=route.profile,
                started=started,
                status="error",
                error=detail,
                http_status=500,
                stream=False,
                attempt=attempt,
                endpoint=request.url.path,
            )
            response.headers["X-Route-Attempts"] = str(attempt)
            raise HTTPException(status_code=500, detail=detail) from exc
        _log_response(
            settings,
            conversation_id,
            routed_request,
            result,
            request_id=request_id,
            attempt=attempt,
        )
        profiles.record_health(
            route.profile.id,
            success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            message="Provider request succeeded",
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
            endpoint=request.url.path,
        )
        response.headers["X-Route-Attempts"] = str(attempt)
        response.headers["X-Actual-Profile"] = route.profile.id
        response.headers["X-Actual-Model"] = route.model.id
        result.update(
            {
                "conversationId": conversation_id,
                "runId": req.run_id,
                "requestId": identity.request_id,
                "correlationId": identity.correlation_id,
                "sequence": 1,
            }
        )
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
    identity = resolve_correlation_identity(request, req)
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
    request_id = identity.request_id
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
                        event = redact_secret_values(
                            event,
                            _provider_secret_values(settings, route.profile),
                        )
                        sequence += 1
                        if event.get("kind") == "delta":
                            content = str(event.get("content") or "")
                            if content:
                                emitted_output = True
                                streamed_parts.append(content)
                                yield _sse_envelope(
                                    routed_request,
                                    trace_id,
                                    identity,
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
                            endpoint=request.url.path,
                        )
                        profiles.record_health(
                            route.profile.id,
                            success=True,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            message="Provider request succeeded",
                        )
                        finalized = True
                        yield _sse_envelope(
                            routed_request,
                            trace_id,
                            identity,
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
                    safe_exc = _sanitized_provider_exception(
                        exc,
                        _provider_secret_values(settings, route.profile),
                    )
                    detail = _error_detail(safe_exc)
                    profiles.record_health(
                        route.profile.id,
                        success=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error_code=detail["code"],
                        message="Provider request failed",
                    )
                    next_index = None if emitted_output else _next_route_index(
                        routes,
                        index,
                        route,
                        safe_exc,
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
                        http_status=(
                            safe_exc.status_code
                            if isinstance(safe_exc, HTTPException)
                            else 500
                        ),
                        stream=True,
                        attempt=attempt,
                        endpoint=request.url.path,
                    )
                    if next_index is not None:
                        index = next_index
                        continue
                    sequence += 1
                    finalized = True
                    yield _sse_envelope(
                        routed_request,
                        trace_id,
                        identity,
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
                    started=started,
                    status="cancelled",
                    error=cancelled_error,
                    http_status=499,
                    stream=True,
                    attempt=max(1, attempt),
                    endpoint=request.url.path,
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=_correlation_headers(identity),
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
    prepare_provider_body(req, profile, False)
    return response_accounting(req, profile, await call_provider(req, profile))


def _stream_route(
    req: AgentChatRequest,
    profile: ProviderProfile,
) -> AsyncIterator[dict[str, Any]]:
    async def accounted():
        prepare_provider_body(req, profile, True)
        async for event in stream_provider(req, profile):
            if event.get("kind") == "completed":
                event = {**event, "payload": response_accounting(req, profile, event["payload"])}
            yield event
    return accounted()


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
                "supportsStreaming": bool(metadata.get("supportsStreaming", True)),
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
    identity: RequestCorrelationIdentity,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    event_payload = dict(payload)
    if req.protocol_version != "1.1":
        event_payload.pop("budget", None)
    envelope = GatewayEventEnvelope.model_validate({
        "protocolVersion": req.protocol_version,
        "sequence": sequence,
        "conversationId": req.conversation_id,
        "runId": req.run_id,
        "traceId": trace_id,
        "requestId": identity.request_id,
        "correlationId": identity.correlation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "payload": event_payload,
    }).model_dump(mode="json", by_alias=True, exclude_unset=True)
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
            "correlationId": req.correlation_id,
            "parentRequestId": req.parent_request_id,
            "retryOf": req.retry_of,
            "runId": req.run_id,
            "attempt": attempt,
            "turnStatus": "running",
            "request": redact_secret_values(
                request_data,
                _provider_secret_values(settings, profile),
            ),
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
            "correlationId": req.correlation_id,
            "parentRequestId": req.parent_request_id,
            "retryOf": req.retry_of,
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
            "correlationId": req.correlation_id,
            "parentRequestId": req.parent_request_id,
            "retryOf": req.retry_of,
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
            **({"budget": detail["budget"]} if detail.get("code") == "CONTEXT_BUDGET_EXCEEDED" and isinstance(detail.get("budget"), dict) else {}),
        }
    return {
        "code": "GATEWAY_ERROR",
        "message": "Gateway request failed",
    }


@overload
def _sanitized_provider_exception(
    exc: HTTPException,
    secret_values: tuple[str, ...],
) -> HTTPException: ...


@overload
def _sanitized_provider_exception(
    exc: Exception,
    secret_values: tuple[str, ...],
) -> Exception: ...


def _sanitized_provider_exception(
    exc: Exception,
    secret_values: tuple[str, ...],
) -> Exception:
    if not isinstance(exc, HTTPException):
        return exc
    detail = redact_secret_values(exc.detail, secret_values)
    return HTTPException(
        status_code=exc.status_code,
        detail=detail,
        headers=exc.headers,
    )


def _settings_secret_values(settings: Settings) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            settings.mimo_api_key,
            settings.claude_code_api_key,
            settings.profile_admin_token,
            settings.admin_password,
            settings.service_auth_token,
            settings.langsmith_api_key,
            settings.local_default_client_api_key,
        )
        if value
    )


def _provider_secret_values(
    settings: Settings,
    profile: ProviderProfile,
) -> tuple[str, ...]:
    return (*_settings_secret_values(settings), profile.api_key)


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


def _require_profile_admin_dependency(request: Request) -> None:
    settings: Settings = request.app.state.settings
    _require_profile_admin(request, settings)


def _require_models_access(request: Request) -> None:
    try:
        _require_client_api_key(request)
        return
    except HTTPException as client_error:
        try:
            settings: Settings = request.app.state.settings
            _require_profile_admin(request, settings)
            return
        except HTTPException:
            raise client_error


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
    authorization = request.headers.get("Authorization", "")
    authorization_scheme = authorization.partition(" ")[0].lower()
    if authorization_scheme == "bearer" or request.headers.get("x-api-key", "").strip():
        _require_client_api_key(request)
        return
    if browser_api_key:
        sessions: AdminSessionStore = request.app.state.admin_sessions
        api_keys: BrowserApiKeyStore = request.app.state.browser_api_keys
        client_api_keys: ClientApiKeyStore = request.app.state.client_api_keys
        if (
            sessions.verify(browser_api_key)
            or api_keys.verify(browser_api_key)
            or client_api_keys.verify(browser_api_key) is not None
        ):
            return
    if (
        settings.local_default_client_api_key
        and not service_token
        and not browser_api_key
        and not authorization
        and not request.headers.get("x-api-key")
    ):
        _require_client_api_key(request)
        return
    api_keys = request.app.state.browser_api_keys
    if (
        settings.service_auth_mode == "disabled"
        and not api_keys.public()["configured"]
    ):
        return
    raise HTTPException(
        status_code=401,
        detail={"code": "GATEWAY_SERVICE_AUTH_REQUIRED"},
    )


def _local_default_client_key(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if (
        not settings.local_default_client_api_key
        or request.headers.get("Origin") != settings.local_default_client_origin
        or request.headers.get(SERVICE_AUTH_HEADER)
        or not request.url.path.startswith(f"{API_PREFIX}/")
        or request.url.path.startswith(f"{API_PREFIX}/admin/")
    ):
        return ""
    try:
        is_loopback = bool(request.client) and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return ""
    return settings.local_default_client_api_key if is_loopback else ""


def _require_client_api_key(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("Authorization", "").strip()
    bearer = ""
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=401,
                detail={"code": "CLIENT_API_KEY_REQUIRED"},
            )
        bearer = token.strip()
    candidates = [
        value
        for value in (
            bearer,
            request.headers.get("x-api-key", "").strip(),
            request.headers.get(BROWSER_API_KEY_HEADER, "").strip(),
        )
        if value
    ]
    if not candidates:
        default_key = _local_default_client_key(request)
        if default_key:
            candidates.append(default_key)
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        raise HTTPException(
            status_code=401,
            detail={"code": "CLIENT_API_KEY_REQUIRED"},
        )
    store: ClientApiKeyStore = request.app.state.client_api_keys
    result = store.verify(candidates[0])
    if result is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "CLIENT_API_KEY_INVALID"},
        )
    request.state.client_api_key_id = result["id"]
    request.state.client_api_key_prefix = result["prefix"]
    return result


def _audit_actor(request: Request) -> str:
    client_api_key_prefix = getattr(request.state, "client_api_key_prefix", "")
    if client_api_key_prefix:
        return f"client-api-key:{client_api_key_prefix}"
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _audit_request_id(request: Request) -> str:
    value = request.headers.get("X-Request-Id", "").strip()
    if not value:
        return f"admin_{secrets.token_hex(8)}"
    try:
        return validate_safe_request_id(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_REQUEST_ID"},
        ) from exc


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
    if (
        settings.admin_allow_remote
        and settings.admin_require_https_remote
        and not is_loopback
        and request.url.scheme != "https"
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_HTTPS_REQUIRED"},
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


def _require_local_default_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("Origin", "").rstrip("/")
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "localhost"
    if (
        not settings.local_default_client_api_key
        or not settings.local_default_client_origin
        or origin != settings.local_default_client_origin.rstrip("/")
        or not is_loopback
    ):
        raise HTTPException(status_code=403, detail={"code": "ADMIN_LOCAL_SESSION_NOT_ALLOWED"})
    if request.app.state.client_api_keys.verify(settings.local_default_client_api_key) is None:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_LOCAL_SESSION_NOT_ALLOWED"})


def _require_mcp_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("Origin", "").rstrip("/")
    if not origin:
        return
    allowed = {item.rstrip("/") for item in settings.allow_origins if item != "*"}
    if origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "MCP_ORIGIN_NOT_ALLOWED"},
        )


def _require_mcp_enabled(settings: Settings) -> None:
    if not settings.mcp_proxy_enabled:
        raise HTTPException(status_code=503, detail={"code": "MCP_PROXY_DISABLED"})


def _prepare_browser_mcp_request(
    request: Request,
    response: Response,
    settings: Settings,
) -> None:
    if not settings.public_browser_mcp_config:
        raise HTTPException(
            status_code=404,
            detail={"code": "BROWSER_MCP_CONFIG_DISABLED"},
        )
    origin = request.headers.get("Origin", "").rstrip("/")
    if not origin:
        raise HTTPException(
            status_code=403,
            detail={"code": "MCP_ORIGIN_REQUIRED"},
        )
    allowed = {item.rstrip("/") for item in settings.allow_origins if item != "*"}
    if origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "MCP_ORIGIN_NOT_ALLOWED"},
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _require_mcp_config_version(
    request: Request, store: McpServerStore, server_id: str
) -> None:
    """Refuse a stale Step binding before dispatch; in-flight calls keep their proxy.

    Older clients may omit the header. New clients pin every tools/list and call
    so a configuration replacement or Secret rotation cannot silently reroute it.
    """
    expected = request.headers.get("X-MCP-Config-Version")
    if expected is None:
        return
    if (
        not expected.isascii()
        or not expected.isdecimal()
        or len(expected) > 10
        or int(expected) < 1
    ):
        raise HTTPException(status_code=422, detail={"code": "MCP_CONFIG_VERSION_INVALID"})
    current = store.effective_version(server_id)
    if int(expected) != current:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONFIG_VERSION_CONFLICT",
                "expected": int(expected),
                "current": current,
            },
        )


def _require_stdio_mcp_server(
    store: McpServerStore,
    server_id: str,
) -> None:
    server = store.get_config(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
    if server.transport != "stdio":
        raise HTTPException(
            status_code=409,
            detail={"code": "MCP_BROWSER_DIRECT_REQUIRED"},
        )


def _browser_mcp_config_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, McpServerConfigVersionConflictError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONFIG_VERSION_CONFLICT",
                "serverId": exc.server_id,
                "expectedConfigVersion": exc.expected,
                "currentConfigVersion": exc.current,
            },
        )
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=403,
            detail={"code": "MCP_SERVER_READ_ONLY"},
        )
    if isinstance(exc, (ValueError, ValidationError)):
        return HTTPException(
            status_code=422,
            detail={"code": "MCP_BROWSER_CONFIG_INVALID"},
        )
    if isinstance(exc, (McpServerStoreError, ManagedSecretStoreError)):
        return HTTPException(
            status_code=503,
            detail={"code": "MCP_CONFIG_STORE_UNAVAILABLE"},
        )
    return HTTPException(
        status_code=500,
        detail={"code": "MCP_BROWSER_CONFIG_OPERATION_FAILED"},
    )


def _log_mcp_call(
    settings: Settings,
    server_id: str,
    request_id: str,
    req: McpToolCallRequest,
    response: dict[str, Any] | None,
    error: McpProxyError | None,
) -> None:
    arguments_text = json.dumps(redact_value(req.arguments), ensure_ascii=False, default=str)
    result_value = response.get("result") if response else None
    result_text = json.dumps(redact_value(result_value), ensure_ascii=False, default=str)
    inline_result = redact_value(result_value) if len(result_text) <= 32_768 else None
    artifact_ref = ""
    if response is not None and inline_result is None:
        artifact_ref = store_conversation_artifact(
            settings,
            req.conversation_id,
            result_value,
        )
    append_conversation_event(
        settings,
        req.conversation_id,
        {
            "type": "mcp_call",
            "requestId": request_id,
            "correlationId": req.correlation_id,
            "parentRequestId": req.parent_request_id,
            "retryOf": req.retry_of,
            "runId": req.run_id,
            "topicId": req.topic_id,
            "checkpointId": req.checkpoint_id,
            "traceId": req.trace_id,
            "serverId": server_id,
            "toolName": req.tool_name,
            "argumentKeys": sorted(req.arguments),
            "argumentsMetadata": {
                "sha256": hashlib.sha256(arguments_text.encode("utf-8")).hexdigest(),
                "characters": len(arguments_text),
            },
            "resultMetadata": {
                "sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
                "characters": len(result_text),
                "artifactStored": bool(artifact_ref),
                "isError": bool(
                    response
                    and isinstance(response.get("result"), dict)
                    and response["result"].get("isError")
                ),
                "durationMs": int(response.get("durationMs", 0)) if response else 0,
            },
            **({"inlineResult": inline_result} if inline_result is not None else {}),
            **({"artifactRef": artifact_ref} if artifact_ref else {}),
            **(
                {"incompleteReason": "artifact_store_unavailable"}
                if response is not None and inline_result is None and not artifact_ref
                else {}
            ),
            **(
                {"error": {"code": error.code, "message": str(error)}}
                if error
                else {}
            ),
        },
    )


def _log_mcp_tools(
    settings: Settings,
    server_id: str,
    identity: RequestCorrelationIdentity,
    response: dict[str, Any] | None,
    error: McpProxyError | None,
) -> None:
    tools = response.get("tools") if isinstance(response, dict) else []
    tool_metadata = []
    if isinstance(tools, list):
        for tool in tools[:256]:
            if not isinstance(tool, dict):
                continue
            schema_text = json.dumps(
                tool.get("inputSchema") or {},
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
            tool_metadata.append(
                {
                    "name": str(tool.get("name") or ""),
                    "schemaSha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
                    "schemaCharacters": len(schema_text),
                }
            )
    append_log(
        settings,
        identity.request_id,
        "mcp-tools.jsonl",
        {
            "type": "mcp_tools",
            "requestId": identity.request_id,
            "correlationId": identity.correlation_id,
            "parentRequestId": identity.parent_request_id,
            "retryOf": identity.retry_of,
            "serverId": server_id,
            "toolCount": len(tool_metadata),
            "tools": tool_metadata,
            **(
                {"error": {"code": error.code, "message": str(error)}}
                if error
                else {}
            ),
        },
    )


def _build_mcp_proxy(
    servers: list[McpServerConfig],
    managed_secrets: McpSecretResolver,
    settings: Settings,
) -> McpProxy:
    return McpProxy(
        servers,
        managed_secrets,
        allowed_stdio_paths={
            FilePath(path).resolve()
            for path in settings.mcp_allowed_executable_paths
        },
        allowed_http_urls=settings.mcp_allowed_http_urls,
        allowed_stdio_roots=[FilePath(path) for path in settings.mcp_allowed_directory_paths],
    )


def _snapshot_mcp_secrets(
    store: ManagedSecretStore,
    servers: list[McpServerConfig] | tuple[McpServerConfig, ...],
    *,
    overrides: dict[str, str] | None = None,
) -> McpSecretResolver:
    candidate_values = dict(overrides or {})
    for server in servers:
        for secret_id in _mcp_server_secret_ids(server):
            if secret_id in candidate_values:
                continue
            try:
                candidate_values[secret_id] = store.resolve_value(secret_id)
            except (KeyError, ManagedSecretStoreError):
                # Preserve the existing fail-closed behavior for unconfigured bindings.
                continue
    return store.candidate_overlay(candidate_values)


def _mcp_server_secret_ids(server: McpServerConfig) -> list[str]:
    transport = server.stdio if server.transport == "stdio" else server.streamable_http
    if transport is None:
        return []
    return sorted({binding.secret_id for binding in transport.secret_bindings})


def _mcp_connection_result(
    server_id: str,
    started: float,
    *,
    listed: dict[str, Any] | None = None,
    error_code: str = "",
) -> dict[str, Any]:
    tools = listed.get("tools", []) if isinstance(listed, dict) else []
    result = McpServerConnectionTestResponse.model_validate(
        {
            "id": server_id,
            "status": "unhealthy" if error_code else "healthy",
            "checkedAt": datetime.now(timezone.utc),
            "latencyMs": max(0, int((time.monotonic() - started) * 1_000)),
            "serverName": listed.get("serverName", "") if listed else "",
            "serverVersion": listed.get("serverVersion", "") if listed else "",
            "toolCount": len(tools),
            "tools": tools,
            "errorCode": error_code,
        }
    )
    return result.model_dump(mode="python", by_alias=True)


def _mcp_proxy_config(server: Any, timeout_seconds: float) -> dict[str, Any]:
    secret_bindings = [
        {
            "secretId": binding.secret_id,
            "target": binding.target,
            "name": binding.name,
            "valuePrefix": binding.prefix,
        }
        for binding in server.secret_bindings
    ]
    if server.secret_binding is not None:
        secret_bindings.append(
            {
                "secretId": server.secret_id,
                "target": server.secret_binding.target,
                "name": server.secret_binding.name,
                "valuePrefix": server.secret_binding.prefix,
            }
        )
    common = {
        "id": server.id,
        "displayName": server.id,
        "transport": server.transport,
        "timeoutSeconds": timeout_seconds,
        "enabled": server.enabled,
    }
    if server.transport == "stdio":
        return {
            **common,
            "stdio": {
                "command": str(server.executable_path),
                "args": server.args,
                "secretBindings": secret_bindings,
            },
        }
    return {
        **common,
        "streamableHttp": {
            "url": server.url,
            "secretBindings": secret_bindings,
        },
    }


def _managed_secret_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"code": "SECRET_NOT_FOUND"})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail={"code": "SECRET_STATE_INVALID"})
    if isinstance(exc, ManagedSecretStoreError):
        return HTTPException(
            status_code=503,
            detail={"code": "SECRET_STORE_UNAVAILABLE"},
        )
    return HTTPException(status_code=500, detail={"code": "SECRET_OPERATION_FAILED"})


def _mcp_server_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, McpProxyError):
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=409, detail={"code": "MCP_SERVER_READ_ONLY"})
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
    if isinstance(exc, McpServerCandidateStaleError):
        return HTTPException(
            status_code=409,
            detail={"code": "MCP_SERVER_CANDIDATE_STALE"},
        )
    if isinstance(exc, McpServerConfigVersionConflictError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONFIG_VERSION_CONFLICT",
                "serverId": exc.server_id,
                "expectedConfigVersion": exc.expected,
                "currentConfigVersion": exc.current,
            },
        )
    if isinstance(exc, McpServerStoreError):
        return HTTPException(
            status_code=503,
            detail={"code": "MCP_SERVER_STORE_UNAVAILABLE"},
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=422,
            detail={"code": "MCP_SERVER_CONFIGURATION_INVALID"},
        )
    return HTTPException(
        status_code=500,
        detail={"code": "MCP_SERVER_OPERATION_FAILED"},
    )


def _validated_time_range(
    started_at: datetime | None,
    ended_at: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    for value in (started_at, ended_at):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail={"code": "TIMEZONE_REQUIRED"})
    if started_at is not None and ended_at is not None and started_at > ended_at:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TIME_RANGE"})
    return started_at, ended_at


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("claude_agent_sdk.actiondesign_gateway.app:app", host="0.0.0.0", port=8318)


if __name__ == "__main__":
    main()
