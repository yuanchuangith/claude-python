"""Verify the deployed fixed GXP read-only MCP through the thin gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from claude_agent_sdk.actiondesign_gateway.models import (
    HealthResponse,
    ReadonlyMcpToolCallResponse,
    ReadonlyMcpToolListResponse,
)
from claude_agent_sdk.actiondesign_gateway.readonly_mcp import (
    GXP_LOWCODE_DEBUG_SKILL_ID,
    GXP_LOWCODE_DEBUG_SKILL_VERSION,
    GXP_READONLY_MCP_TOOLS,
)


def _request_json(
    url: str,
    *,
    origin: str,
    service_token: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="GET" if payload is None else "POST",
        headers={
            "Accept": "application/json",
            "Origin": origin,
            **(
                {"X-ActionDesign-Service-Token": service_token}
                if service_token
                else {}
            ),
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:4_000]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {error_body}") from exc


def _call_payload(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "toolName": tool_name,
        "arguments": arguments,
        "conversationId": "verify_readonly_mcp",
        "runId": "verify_readonly_mcp_run",
        "topicId": "verify_readonly_mcp_topic",
        "checkpointId": "verify_readonly_mcp_checkpoint",
        "traceId": "verify_readonly_mcp_trace",
        "skillId": GXP_LOWCODE_DEBUG_SKILL_ID,
        "skillVersion": GXP_LOWCODE_DEBUG_SKILL_VERSION,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ACTIONDESIGN_GATEWAY_BASE_URL", ""),
        help="Gateway API base URL ending in /api/actiondesign-agent.",
    )
    parser.add_argument(
        "--origin",
        default=os.environ.get("ACTIONDESIGN_GATEWAY_ALLOWED_ORIGIN", ""),
        help="Exact browser Origin allowed by the deployed gateway.",
    )
    parser.add_argument(
        "--text",
        default="检查当前 GXP 只读诊断连接状态。",
        help="Harmless natural-language input passed to diagnose_codex_input.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        parser.error("--base-url must be an absolute http(s) URL")
    if not args.origin.startswith(("http://", "https://")):
        parser.error("--origin must be an absolute http(s) Origin")
    service_token = os.environ.get("ACTIONDESIGN_SERVICE_AUTH_TOKEN", "")

    health = HealthResponse.model_validate(
        _request_json(
            f"{base_url}/health",
            origin=args.origin,
            service_token=service_token,
            timeout=args.timeout,
        )
    )
    if not health.readonly_mcp_enabled:
        print("BLOCKED: deployed gateway reports readonlyMcpEnabled=false", file=sys.stderr)
        return 2

    tool_list = ReadonlyMcpToolListResponse.model_validate(
        _request_json(
            f"{base_url}/mcp/gxp-lowcode-readonly/tools",
            origin=args.origin,
            service_token=service_token,
            timeout=args.timeout,
        )
    )
    tool_names = tuple(sorted(tool.name for tool in tool_list.tools))
    expected_tool_names = tuple(sorted(GXP_READONLY_MCP_TOOLS))
    if tool_names != expected_tool_names:
        raise RuntimeError(f"fixed MCP tool list mismatch: {tool_names!r}")

    call_url = f"{base_url}/mcp/gxp-lowcode-readonly/call"
    connection = ReadonlyMcpToolCallResponse.model_validate(
        _request_json(
            call_url,
            origin=args.origin,
            service_token=service_token,
            payload=_call_payload("connection_status", {}),
            timeout=args.timeout,
        )
    )
    if not connection.success or connection.tool_name != "connection_status":
        raise RuntimeError("connection_status did not return a successful fixed-tool response")

    diagnose = ReadonlyMcpToolCallResponse.model_validate(
        _request_json(
            call_url,
            origin=args.origin,
            service_token=service_token,
            payload=_call_payload("diagnose_codex_input", {"text": args.text}),
            timeout=args.timeout,
        )
    )
    if not diagnose.success or diagnose.tool_name != "diagnose_codex_input":
        raise RuntimeError("diagnose_codex_input did not return a successful fixed-tool response")

    print(
        json.dumps(
            {
                "status": "passed",
                "serverVersion": tool_list.server_version,
                "skill": f"{tool_list.skill_id}@{tool_list.skill_version}",
                "toolCount": len(tool_list.tools),
                "connectionConfigured": bool((connection.result or {}).get("configured")),
                "diagnoseInputKind": (diagnose.result or {}).get("input_kind", ""),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
