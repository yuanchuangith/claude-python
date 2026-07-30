"""Live-only LangSmith acceptance check for the ActionDesign thin gateway.

This script intentionally exits with code 2 when real LangSmith configuration is
missing. It never turns a missing workspace into a passing test and does not use
mock transports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx


CONVERSATION_ID = "66666666-6666-4666-8666-666666666666"
AGENT_RUN_ID = "run_77777777-7777-4777-8777-777777777777"
REQUEST_ID = "88888888-8888-4888-8888-888888888888"
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"


def _required_environment() -> dict[str, str]:
    values = {
        "api_key": os.getenv("LANGSMITH_API_KEY", "").strip(),
        "workspace_id": os.getenv("LANGSMITH_WORKSPACE_ID", "").strip(),
        "project": os.getenv("LANGSMITH_PROJECT", "").strip(),
        "endpoint": os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").strip(),
        "enabled": os.getenv("ACTIONDESIGN_LANGSMITH_ENABLED", "").strip().lower(),
    }
    missing = [
        name for name in ("api_key", "workspace_id", "project")
        if not values[name]
    ]
    if values["enabled"] not in {"1", "true", "yes", "on"}:
        missing.append("ACTIONDESIGN_LANGSMITH_ENABLED=true")
    if missing:
        raise RuntimeError(f"real LangSmith configuration is missing: {', '.join(missing)}")
    if not values["endpoint"].startswith("https://"):
        raise RuntimeError("LANGSMITH_ENDPOINT must use HTTPS")
    return values


def _run_uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"actiondesign:{value}"))


def _timestamp(offset_ms: int = 0) -> str:
    now = datetime.now(UTC).timestamp() + (offset_ms / 1000)
    return datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")


def _metadata(node_name: str, *, tool: bool = False) -> dict[str, Any]:
    sha = f"sha256:{'a' * 64}"
    return {
        "environment": "live-acceptance",
        "conversationId": CONVERSATION_ID,
        "topicId": "",
        "checkpointId": "",
        "graphVersion": "2.0.0",
        "protocolVersion": "1.0",
        "promptManifestVersion": "2.2.0",
        "promptManifestHash": sha,
        "toolCatalogVersion": "1.8.0",
        "toolCatalogHash": sha,
        "skillCatalogVersion": "1.3.0",
        "skillCatalogHash": sha,
        "agentMode": "chat",
        "nodeName": node_name,
        "toolName": "gxp_connection_status" if tool else "",
        "toolVersion": "1.0.0" if tool else "",
        "provider": "live-verifier",
        "model": "live-verifier",
        "skillId": "gxp-lowcode-debug" if tool else "",
        "skillVersion": "1.2.0" if tool else "",
        "mcpServerId": "gxp-lowcode-readonly" if tool else "",
        "mcpToolName": "connection_status" if tool else "",
    }


def _event(
    *,
    event_id: str,
    trace_id: str,
    run_id: str,
    parent_id: str,
    name: str,
    run_type: str,
    event: str,
    offset_ms: int,
    tool: bool = False,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "traceId": trace_id,
        "traceRunId": run_id,
        "parentTraceRunId": parent_id,
        "agentRunId": AGENT_RUN_ID,
        "requestId": REQUEST_ID,
        "correlationId": CORRELATION_ID,
        "name": name,
        "runType": run_type,
        "event": event,
        "timestamp": _timestamp(offset_ms),
        "inputs": {"apiKey": "live-secret-probe", "phase": "start"} if event == "start" else None,
        "outputs": {"status": "ok", "phase": "end"} if event == "end" else None,
        "tags": ["actiondesign", "live-acceptance"],
        "metadata": _metadata(name, tool=tool),
    }


def _extract_run(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("run"), dict):
            return payload["run"]
        return payload
    return None


def _read_run(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    deadline: float,
) -> dict[str, Any]:
    last_status = 0
    while time.monotonic() < deadline:
        response = client.get(url, headers=headers)
        last_status = response.status_code
        if response.status_code == 200:
            run = _extract_run(response.json())
            if run and run.get("end_time"):
                return run
        elif response.status_code not in {404, 408, 425, 429, 500, 502, 503, 504}:
            response.raise_for_status()
        time.sleep(1)
    raise RuntimeError(f"LangSmith run was not readable before timeout: {url} (last status {last_status})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gateway-base-url",
        default=os.getenv(
            "ACTIONDESIGN_GATEWAY_BASE_URL",
            "http://127.0.0.1:8890/api/actiondesign-agent",
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=25.0)
    args = parser.parse_args()

    try:
        config = _required_environment()
    except RuntimeError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2

    gateway = args.gateway_base_url.rstrip("/")
    token = uuid.uuid4().hex
    trace_id = f"trace_live_{token}"
    graph_id = trace_id
    node_id = f"node_live_{token}"
    llm_id = f"llm_live_{token}"
    tool_id = f"tool_live_{token}"
    runs = [
        (graph_id, "", "actiondesign_graph", "graph", False),
        (node_id, graph_id, "context_node", "chain", False),
        (llm_id, node_id, "model_invocation", "llm", False),
        (tool_id, node_id, "gxp_connection_status", "tool", True),
    ]
    events: list[dict[str, Any]] = []
    for index, (run_id, parent_id, name, run_type, tool) in enumerate(runs):
        events.append(_event(
            event_id=f"event_start_{index}_{token}", trace_id=trace_id, run_id=run_id,
            parent_id=parent_id, name=name, run_type=run_type, event="start",
            offset_ms=index * 10, tool=tool,
        ))
    for index, (run_id, parent_id, name, run_type, tool) in enumerate(reversed(runs), start=len(runs)):
        events.append(_event(
            event_id=f"event_end_{index}_{token}", trace_id=trace_id, run_id=run_id,
            parent_id=parent_id, name=name, run_type=run_type, event="end",
            offset_ms=index * 10, tool=tool,
        ))

    headers = {"Content-Type": "application/json"}
    service_token = os.getenv("ACTIONDESIGN_SERVICE_AUTH_TOKEN", "")
    if service_token:
        headers["X-ActionDesign-Service-Token"] = service_token
    headers.update(
        {
            "X-Conversation-Id": CONVERSATION_ID,
            "X-Agent-Run-Id": AGENT_RUN_ID,
            "X-Request-Id": REQUEST_ID,
            "X-Correlation-Id": CORRELATION_ID,
        }
    )
    langsmith_headers = {
        "x-api-key": config["api_key"],
        "X-Tenant-Id": config["workspace_id"],
    }
    deadline = time.monotonic() + max(5.0, min(args.timeout_seconds, 60.0))
    try:
        with httpx.Client(timeout=15.0) as client:
            health = client.get(f"{gateway}/health")
            health.raise_for_status()
            if health.json().get("langsmithEnabled") is not True:
                raise RuntimeError("gateway health does not report langsmithEnabled=true")
            response = client.post(
                f"{gateway}/telemetry/langsmith/runs/batch",
                headers=headers,
                json={
                    "conversationId": CONVERSATION_ID,
                    "runId": AGENT_RUN_ID,
                    "requestId": REQUEST_ID,
                    "correlationId": CORRELATION_ID,
                    "sequence": 1,
                    "schemaVersion": "1.0",
                    "contentLevel": "sanitized",
                    "sentAt": _timestamp(),
                    "events": events,
                },
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "accepted" or result.get("accepted") != len(events):
                raise RuntimeError(f"gateway rejected live trace events: {result}")

            read_runs: dict[str, dict[str, Any]] = {}
            endpoint = config["endpoint"].rstrip("/")
            for run_id, _parent_id, _name, _run_type, _tool in runs:
                read_runs[run_id] = _read_run(
                    client,
                    f"{endpoint}/runs/{_run_uuid(run_id)}",
                    langsmith_headers,
                    deadline,
                )

        root = read_runs[graph_id]
        if str(root.get("id")) != _run_uuid(graph_id) or str(root.get("trace_id")) != _run_uuid(trace_id):
            raise RuntimeError("root LangSmith run id/trace_id does not match the deterministic trace contract")
        if str(read_runs[node_id].get("parent_run_id")) != _run_uuid(graph_id):
            raise RuntimeError("node parent_run_id is incorrect")
        if str(read_runs[llm_id].get("parent_run_id")) != _run_uuid(node_id):
            raise RuntimeError("llm parent_run_id is incorrect")
        if str(read_runs[tool_id].get("parent_run_id")) != _run_uuid(node_id):
            raise RuntimeError("tool parent_run_id is incorrect")
        serialized = json.dumps(read_runs, ensure_ascii=False)
        if "live-secret-probe" in serialized:
            raise RuntimeError("LangSmith read-back contains the secret redaction probe")
        tool_metadata = ((read_runs[tool_id].get("extra") or {}).get("metadata") or {})
        expected_metadata = {
            "skillId": "gxp-lowcode-debug",
            "skillVersion": "1.2.0",
            "mcpServerId": "gxp-lowcode-readonly",
            "mcpToolName": "connection_status",
            "toolVersion": "1.0.0",
        }
        for key, expected in expected_metadata.items():
            if tool_metadata.get(key) != expected:
                raise RuntimeError(f"tool metadata mismatch for {key}")
        if root.get("session_name") not in {None, config["project"]}:
            raise RuntimeError("LangSmith project/session_name does not match LANGSMITH_PROJECT")
    except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({
        "status": "passed",
        "project": config["project"],
        "workspaceConfigured": True,
        "traceId": _run_uuid(trace_id),
        "runIds": {name: _run_uuid(run_id) for run_id, _parent, name, _type, _tool in runs},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
