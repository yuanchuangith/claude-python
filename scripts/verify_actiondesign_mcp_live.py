"""Live acceptance verifier for the ActionDesign generic MCP transport.

The verifier temporarily registers the public, credential-free DeepWiki MCP
server, exercises the real admin and runtime HTTP contracts, and removes the
temporary configuration before exiting. Credentials are read only from the
environment and are never included in verifier output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import httpx


DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"
DEEPWIKI_TOOL = "read_wiki_structure"
DEFAULT_REPOSITORY = "modelcontextprotocol/python-sdk"
API_PATH = "/api/actiondesign-agent"
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_FORBIDDEN_MCP_PUBLIC_KEYS = {
    "args",
    "command",
    "cwd",
    "headers",
    "masked",
    "stdio",
    "streamablehttp",
    "url",
    "value",
}


class VerificationError(RuntimeError):
    """Safe-to-print verification failure."""

    def __init__(self, reason: str, *, blocked: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blocked = blocked


@dataclass(frozen=True)
class LiveConfig:
    gateway: str
    origin: str
    admin_password: str
    repository: str


def _required_environment() -> LiveConfig:
    gateway_value = os.getenv("ACTIONDESIGN_GATEWAY_BASE_URL", "").strip()
    admin_password = os.getenv("ACTIONDESIGN_ADMIN_PASSWORD", "")
    repository = os.getenv(
        "ACTIONDESIGN_MCP_LIVE_REPOSITORY",
        DEFAULT_REPOSITORY,
    ).strip()
    missing = []
    if not gateway_value:
        missing.append("ACTIONDESIGN_GATEWAY_BASE_URL")
    if not admin_password:
        missing.append("ACTIONDESIGN_ADMIN_PASSWORD")
    if missing:
        raise VerificationError(
            f"required live configuration is missing: {', '.join(missing)}",
            blocked=True,
        )

    parsed = urlsplit(gateway_value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError(
            "ACTIONDESIGN_GATEWAY_BASE_URL must be a credential-free HTTPS URL",
            blocked=True,
        )
    path = parsed.path.rstrip("/")
    if not path:
        gateway = f"{parsed.scheme}://{parsed.netloc}{API_PATH}"
    elif path == API_PATH:
        gateway = f"{parsed.scheme}://{parsed.netloc}{path}"
    else:
        raise VerificationError(
            f"ACTIONDESIGN_GATEWAY_BASE_URL must end with {API_PATH}",
            blocked=True,
        )
    if not _REPOSITORY_RE.fullmatch(repository):
        raise VerificationError(
            "ACTIONDESIGN_MCP_LIVE_REPOSITORY must use owner/repository format",
            blocked=True,
        )
    return LiveConfig(
        gateway=gateway,
        origin=f"{parsed.scheme}://{parsed.netloc}",
        admin_password=admin_password,
        repository=repository,
    )


def _response_code(response: Any) -> str:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    if isinstance(payload.get("code"), str):
        return payload["code"]
    return ""


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    try:
        return json.dumps(response.json(), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _forbidden_public_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in _FORBIDDEN_MCP_PUBLIC_KEYS:
                found.add(str(key))
            found.update(_forbidden_public_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_forbidden_public_keys(item))
    return found


def _contains_mcp_policy_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower().startswith("mcp") or _contains_mcp_policy_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_mcp_policy_key(item) for item in value)
    return False


def _request(
    client: Any,
    method: str,
    url: str,
    *,
    stage: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    try:
        return client.request(
            method,
            url,
            headers=headers,
            json=payload,
        )
    except httpx.RequestError as exc:
        raise VerificationError(f"{stage} could not reach the gateway") from exc


def _expect_json(
    response: Any,
    *,
    stage: str,
    statuses: set[int] = {200},
    blocked_codes: set[str] | None = None,
) -> dict[str, Any]:
    code = _response_code(response)
    if response.status_code not in statuses:
        blocked = code in (blocked_codes or set())
        suffix = f" ({code})" if code else ""
        raise VerificationError(
            f"{stage} returned HTTP {response.status_code}{suffix}",
            blocked=blocked,
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{stage} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"{stage} returned a non-object JSON payload")
    return payload


def _admin_headers(session_token: str, origin: str, *, audit: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": origin,
    }
    if audit:
        headers["X-Request-Id"] = str(uuid.uuid4())
    return headers


def _runtime_identity() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def _runtime_headers(
    session_token: str,
    origin: str,
    request_id: str,
    correlation_id: str,
    *,
    conversation_id: str = "",
    run_id: str = "",
) -> dict[str, str]:
    headers = {
        "X-ActionDesign-Api-Key": session_token,
        "X-Request-Id": request_id,
        "X-Correlation-Id": correlation_id,
        "Origin": origin,
    }
    if conversation_id:
        headers["X-Conversation-Id"] = conversation_id
    if run_id:
        headers["X-Agent-Run-Id"] = run_id
    return headers


def _server_payload(server_id: str, timeout_seconds: float) -> dict[str, Any]:
    return {
        "id": server_id,
        "displayName": "DeepWiki Live Verification",
        "transport": "streamable_http",
        "streamableHttp": {
            "url": DEEPWIKI_URL,
            "secretBindings": [],
        },
        "expectedServerName": "DeepWiki",
        "timeoutSeconds": timeout_seconds,
        "enabled": True,
    }


def _assert_safe_server_summary(
    payload: dict[str, Any],
    *,
    stage: str,
    server_id: str,
    enabled: bool,
) -> None:
    if payload.get("id") != server_id:
        raise VerificationError(f"{stage} returned the wrong MCP Server id")
    if payload.get("browserConfigurable") is not True:
        raise VerificationError(f"{stage} did not confirm browserConfigurable=true")
    if payload.get("enabled") is not enabled:
        raise VerificationError(f"{stage} returned the wrong enabled state")
    forbidden = _forbidden_public_keys(payload)
    if forbidden:
        raise VerificationError(f"{stage} exposed MCP transport or Secret fields")


def _assert_secret_response(
    response: Any,
    *,
    stage: str,
    canary_values: tuple[str, str],
) -> dict[str, Any]:
    response_text = _response_text(response)
    if any(value in response_text for value in canary_values):
        raise VerificationError(f"{stage} exposed the Secret canary")
    try:
        raw_payload = response.json()
    except (ValueError, json.JSONDecodeError):
        raw_payload = None
    if _contains_key(raw_payload, "masked") or '"masked"' in response_text:
        raise VerificationError(f"{stage} exposed a masked Secret field")
    return _expect_json(response, stage=stage)


def _assert_secret_list_response(response: Any) -> int:
    response_text = _response_text(response)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise VerificationError("Secret reference list did not return JSON") from exc
    if not isinstance(payload, list):
        raise VerificationError("Secret reference list did not return an array")
    allowed = {
        "id",
        "kind",
        "displayName",
        "configured",
        "createdAt",
        "updatedAt",
        "rotatedAt",
        "revokedAt",
    }
    for item in payload:
        if not isinstance(item, dict) or set(item) != allowed:
            raise VerificationError("Secret reference list violated the public contract")
    if '"masked"' in response_text or '"value"' in response_text:
        raise VerificationError("Secret reference list exposed Secret material")
    return len(payload)


def _exercise_secret_canary(
    client: Any,
    config: LiveConfig,
    session_token: str,
) -> bool:
    secret_id = f"live.mcp.canary.{uuid.uuid4().hex}"
    first = f"live-canary-{secrets.token_urlsafe(32)}"
    second = f"live-canary-{secrets.token_urlsafe(32)}"
    canary_values = (first, second)
    secret_url = f"{config.gateway}/admin/secrets/{secret_id}"
    created = False
    revoked = False
    primary_error: VerificationError | None = None
    cleanup_errors: list[str] = []
    try:
        create_response = _request(
            client,
            "PUT",
            secret_url,
            stage="Secret canary creation",
            headers=_admin_headers(session_token, config.origin, audit=True),
            payload={
                "id": secret_id,
                "kind": "mcp-live-canary",
                "displayName": "MCP Live Secret Canary",
                "value": first,
            },
        )
        created = create_response.status_code == 200
        created_payload = _assert_secret_response(
            create_response,
            stage="Secret canary creation",
            canary_values=canary_values,
        )
        if created_payload.get("id") != secret_id or created_payload.get("configured") is not True:
            raise VerificationError("Secret canary creation violated the public contract")

        rotated = _assert_secret_response(
            _request(
                client,
                "POST",
                f"{secret_url}/rotate",
                stage="Secret canary rotation",
                headers=_admin_headers(session_token, config.origin, audit=True),
                payload={"value": second},
            ),
            stage="Secret canary rotation",
            canary_values=canary_values,
        )
        if rotated.get("id") != secret_id or rotated.get("configured") is not True:
            raise VerificationError("Secret canary rotation violated the public contract")

        fetched = _assert_secret_response(
            _request(
                client,
                "GET",
                secret_url,
                stage="Secret canary query",
                headers=_admin_headers(session_token, config.origin),
            ),
            stage="Secret canary query",
            canary_values=canary_values,
        )
        if fetched.get("id") != secret_id or fetched.get("configured") is not True:
            raise VerificationError("Secret canary query violated the public contract")
    except VerificationError as exc:
        primary_error = exc
    finally:
        if created:
            try:
                revoked_payload = _assert_secret_response(
                    _request(
                        client,
                        "DELETE",
                        secret_url,
                        stage="Secret canary revocation",
                        headers=_admin_headers(session_token, config.origin, audit=True),
                    ),
                    stage="Secret canary revocation",
                    canary_values=canary_values,
                )
                revoked = (
                    revoked_payload.get("id") == secret_id
                    and revoked_payload.get("configured") is False
                )
                if not revoked:
                    cleanup_errors.append("revocation state was not confirmed")
                else:
                    verified = _assert_secret_response(
                        _request(
                            client,
                            "GET",
                            secret_url,
                            stage="Secret canary cleanup verification",
                            headers=_admin_headers(session_token, config.origin),
                        ),
                        stage="Secret canary cleanup verification",
                        canary_values=canary_values,
                    )
                    if verified.get("configured") is not False:
                        cleanup_errors.append("revocation could not be verified")
            except VerificationError as exc:
                cleanup_errors.append(exc.reason)

    if primary_error is not None:
        reason = primary_error.reason
        if cleanup_errors:
            reason += "; Secret canary cleanup failed"
        raise VerificationError(reason)
    if cleanup_errors or not revoked:
        raise VerificationError("Secret canary cleanup failed")
    return True


def _tool_definition(payload: dict[str, Any]) -> dict[str, Any]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise VerificationError("tools/list did not return a tools array")
    for item in tools:
        if isinstance(item, dict) and item.get("name") == DEEPWIKI_TOOL:
            schema = item.get("inputSchema")
            if not isinstance(schema, dict):
                break
            properties = schema.get("properties")
            required = schema.get("required")
            repo_schema = properties.get("repoName") if isinstance(properties, dict) else None
            if (
                schema.get("type") == "object"
                and isinstance(repo_schema, dict)
                and repo_schema.get("type") == "string"
                and isinstance(required, list)
                and "repoName" in required
            ):
                return item
            break
    raise VerificationError("tools/list did not expose the expected read-only tool schema")


def _cleanup(
    client: Any,
    config: LiveConfig,
    session_token: str,
    server_id: str,
    *,
    disabled: bool,
) -> tuple[bool, bool, list[str]]:
    restored = not disabled
    deleted = False
    errors: list[str] = []
    server_url = f"{config.gateway}/admin/mcp-servers/{server_id}"
    if disabled:
        response = _request(
            client,
            "PUT",
            f"{server_url}/enabled",
            stage="cleanup restore",
            headers=_admin_headers(session_token, config.origin, audit=True),
            payload={"enabled": True},
        )
        if response.status_code == 200:
            try:
                restored_payload = _expect_json(response, stage="cleanup restore")
                _assert_safe_server_summary(
                    restored_payload,
                    stage="cleanup restore",
                    server_id=server_id,
                    enabled=True,
                )
                restored = True
            except VerificationError as exc:
                errors.append(exc.reason)
        else:
            errors.append(f"restore HTTP {response.status_code}")

    response = _request(
        client,
        "DELETE",
        server_url,
        stage="cleanup delete",
        headers=_admin_headers(session_token, config.origin, audit=True),
    )
    if response.status_code == 200:
        deleted = True
    elif response.status_code == 404:
        deleted = True
    else:
        errors.append(f"delete HTTP {response.status_code}")

    if deleted:
        response = _request(
            client,
            "GET",
            server_url,
            stage="cleanup verification",
            headers=_admin_headers(session_token, config.origin),
        )
        if response.status_code != 404 or _response_code(response) != "MCP_SERVER_NOT_FOUND":
            deleted = False
            errors.append("deletion could not be verified")
    return restored, deleted, errors


def _run(
    client: Any,
    config: LiveConfig,
    *,
    timeout_seconds: float,
    verify_secret_canary: bool,
) -> dict[str, Any]:
    openapi_url = f"{config.origin}/openapi.json"
    openapi = _expect_json(
        _request(client, "GET", openapi_url, stage="OpenAPI check"),
        stage="OpenAPI check",
    )
    paths = openapi.get("paths")
    required_paths = {
        f"{API_PATH}/admin/mcp-servers",
        f"{API_PATH}/admin/mcp-servers/{{server_id}}",
        f"{API_PATH}/admin/mcp-servers/{{server_id}}/enabled",
        f"{API_PATH}/admin/mcp-servers/{{server_id}}/connection-test",
        f"{API_PATH}/admin/secrets",
        f"{API_PATH}/mcp/{{server_id}}/tools",
        f"{API_PATH}/mcp/{{server_id}}/call",
    }
    if verify_secret_canary:
        required_paths.update({
            f"{API_PATH}/admin/secrets/{{secret_id}}",
            f"{API_PATH}/admin/secrets/{{secret_id}}/rotate",
        })
    if not isinstance(paths, dict) or not required_paths.issubset(paths):
        raise VerificationError("gateway OpenAPI is missing MCP live-verification routes", blocked=True)

    health = _expect_json(
        _request(client, "GET", f"{config.gateway}/health", stage="health check"),
        stage="health check",
    )
    if health.get("mcpProxyEnabled") is not True:
        raise VerificationError(
            "gateway health does not report mcpProxyEnabled=true",
            blocked=True,
        )

    login = _expect_json(
        _request(
            client,
            "POST",
            f"{config.gateway}/admin/login",
            stage="admin login",
            headers={"Origin": config.origin},
            payload={"password": config.admin_password},
        ),
        stage="admin login",
    )
    session_token = login.get("sessionToken")
    if not isinstance(session_token, str) or not session_token:
        raise VerificationError("admin login did not return a session")

    secret_reference_count = _assert_secret_list_response(
        _request(
            client,
            "GET",
            f"{config.gateway}/admin/secrets",
            stage="Secret reference list",
            headers=_admin_headers(session_token, config.origin),
        )
    )

    secret_canary_verified = False
    if verify_secret_canary:
        secret_canary_verified = _exercise_secret_canary(client, config, session_token)

    policy = _expect_json(
        _request(
            client,
            "GET",
            f"{config.gateway}/admin/runtime-policy",
            stage="runtime policy check",
            headers=_admin_headers(session_token, config.origin),
        ),
        stage="runtime policy check",
    )
    if _contains_mcp_policy_key(policy):
        raise VerificationError("runtime policy exposed MCP target configuration")

    server_id = f"live.deepwiki.{uuid.uuid4().hex}"
    server_url = f"{config.gateway}/admin/mcp-servers/{server_id}"
    created = False
    disabled = False
    restored = False
    deleted = False
    result: dict[str, Any] | None = None
    primary_error: VerificationError | None = None
    cleanup_errors: list[str] = []
    try:
        create_response = _request(
            client,
            "PUT",
            server_url,
            stage="temporary MCP Server creation",
            headers=_admin_headers(session_token, config.origin, audit=True),
            payload=_server_payload(server_id, timeout_seconds),
        )
        created = create_response.status_code == 200
        created_payload = _expect_json(
            create_response,
            stage="temporary MCP Server creation",
            blocked_codes={"MCP_SERVER_CONFIGURATION_INVALID", "MCP_PROXY_DISABLED"},
        )
        _assert_safe_server_summary(
            created_payload,
            stage="temporary MCP Server creation",
            server_id=server_id,
            enabled=True,
        )
        if (
            created_payload.get("source") != "managed"
            or created_payload.get("enabled") is not True
            or created_payload.get("proxyEnabled") is not True
            or created_payload.get("secretBindingStatus") != []
        ):
            raise VerificationError("temporary MCP Server response violated the public contract")

        connection = _expect_json(
            _request(
                client,
                "POST",
                f"{server_url}/connection-test",
                stage="MCP connection test",
                headers=_admin_headers(session_token, config.origin, audit=True),
            ),
            stage="MCP connection test",
        )
        if connection.get("status") != "healthy" or connection.get("errorCode"):
            raise VerificationError("MCP connection test was not healthy")
        _tool_definition(connection)

        list_request_id, list_correlation_id = _runtime_identity()
        listed = _expect_json(
            _request(
                client,
                "GET",
                f"{config.gateway}/mcp/{server_id}/tools",
                stage="tools/list",
                headers=_runtime_headers(
                    session_token,
                    config.origin,
                    list_request_id,
                    list_correlation_id,
                ),
            ),
            stage="tools/list",
        )
        if listed.get("serverId") != server_id:
            raise VerificationError("tools/list returned the wrong MCP Server id")
        _tool_definition(listed)

        request_id, correlation_id = _runtime_identity()
        conversation_id = str(uuid.uuid4())
        run_id = f"run_{uuid.uuid4()}"
        called = _expect_json(
            _request(
                client,
                "POST",
                f"{config.gateway}/mcp/{server_id}/call",
                stage="tools/call",
                headers=_runtime_headers(
                    session_token,
                    config.origin,
                    request_id,
                    correlation_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                ),
                payload={
                    "requestId": request_id,
                    "correlationId": correlation_id,
                    "conversationId": conversation_id,
                    "runId": run_id,
                    "toolName": DEEPWIKI_TOOL,
                    "arguments": {"repoName": config.repository},
                },
            ),
            stage="tools/call",
        )
        call_result = called.get("result")
        if (
            called.get("requestId") != request_id
            or called.get("correlationId") != correlation_id
            or called.get("conversationId") != conversation_id
            or called.get("runId") != run_id
            or called.get("serverId") != server_id
            or called.get("toolName") != DEEPWIKI_TOOL
            or not isinstance(call_result, dict)
            or call_result.get("isError") is not False
            or not isinstance(call_result.get("content"), list)
            or not call_result["content"]
        ):
            raise VerificationError("tools/call violated the MCP result or identity contract")

        disabled_payload = _expect_json(
            _request(
                client,
                "PUT",
                f"{server_url}/enabled",
                stage="temporary MCP Server disable",
                headers=_admin_headers(session_token, config.origin, audit=True),
                payload={"enabled": False},
            ),
            stage="temporary MCP Server disable",
        )
        disabled = True
        _assert_safe_server_summary(
            disabled_payload,
            stage="temporary MCP Server disable",
            server_id=server_id,
            enabled=False,
        )

        disabled_request_id, disabled_correlation_id = _runtime_identity()
        unavailable = _request(
            client,
            "GET",
            f"{config.gateway}/mcp/{server_id}/tools",
            stage="disabled MCP rejection",
            headers=_runtime_headers(
                session_token,
                config.origin,
                disabled_request_id,
                disabled_correlation_id,
            ),
        )
        if unavailable.status_code != 503 or _response_code(unavailable) != "MCP_SERVER_DISABLED":
            raise VerificationError("disabled MCP Server was not rejected deterministically")

        result = {
            "status": "passed",
            "gatewayVersion": ((openapi.get("info") or {}).get("version") or ""),
            "serverId": server_id,
            "connectionStatus": "healthy",
            "toolCount": len(listed.get("tools") or []),
            "readWikiStructureCalled": True,
            "disabledRejected": True,
            "secretReferenceCount": secret_reference_count,
            "secretCanaryVerified": secret_canary_verified,
        }
    except VerificationError as exc:
        primary_error = exc
    finally:
        if created:
            try:
                restored, deleted, cleanup_errors = _cleanup(
                    client,
                    config,
                    session_token,
                    server_id,
                    disabled=disabled,
                )
            except VerificationError as exc:
                cleanup_errors = [exc.reason]

    if primary_error is not None:
        reason = primary_error.reason
        if cleanup_errors:
            reason += "; cleanup failed"
        raise VerificationError(reason, blocked=primary_error.blocked and not cleanup_errors)
    if result is None:
        raise VerificationError("MCP live verification did not produce a result")
    if cleanup_errors or not restored or not deleted:
        raise VerificationError("temporary MCP Server cleanup failed")
    result.update({"restored": restored, "deleted": deleted})
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., Any] = httpx.Client,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--secret-canary",
        action="store_true",
        help="exercise create/rotate/query/revoke without binding the canary",
    )
    args = parser.parse_args(argv)
    if not 5.0 <= args.timeout_seconds <= 120.0:
        print(json.dumps({"status": "blocked", "reason": "timeout must be between 5 and 120 seconds"}))
        return 2

    try:
        config = _required_environment()
        with client_factory(
            timeout=httpx.Timeout(args.timeout_seconds),
            follow_redirects=False,
        ) as client:
            result = _run(
                client,
                config,
                timeout_seconds=args.timeout_seconds,
                verify_secret_canary=args.secret_canary,
            )
    except VerificationError as exc:
        print(json.dumps({
            "status": "blocked" if exc.blocked else "failed",
            "reason": exc.reason,
        }, ensure_ascii=False))
        return 2 if exc.blocked else 1
    except Exception:
        # Never let an unexpected client/library exception print request data,
        # session credentials, or canary values through a traceback.
        print(json.dumps({
            "status": "failed",
            "reason": "unexpected live verifier error",
        }))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
