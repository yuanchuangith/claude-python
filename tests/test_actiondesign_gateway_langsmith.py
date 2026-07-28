from types import SimpleNamespace

import anyio
import httpx

from claude_agent_sdk.actiondesign_gateway import langsmith_forwarder


def test_langsmith_maps_graph_and_interrupt_runs_to_chain(monkeypatch):
    requests = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            requests.append(("POST", url, kwargs["json"]))
            return FakeResponse()

        async def patch(self, url, **kwargs):
            requests.append(("PATCH", url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", FakeClient)
    settings = SimpleNamespace(
        langsmith_enabled=True,
        langsmith_api_key="secret",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="workspace",
        langsmith_project="actiondesign-test",
        langsmith_timeout_seconds=5,
    )
    events = [
        {
            "eventId": "graph-start",
            "traceId": "trace",
            "traceRunId": "trace",
            "name": "actiondesign-agent",
            "runType": "graph",
            "event": "start",
            "timestamp": "2026-07-25T00:00:00Z",
        },
        {
            "eventId": "interrupt-start",
            "traceId": "trace",
            "traceRunId": "interrupt",
            "parentTraceRunId": "trace",
            "name": "plan-approval",
            "runType": "interrupt",
            "event": "start",
            "timestamp": "2026-07-25T00:00:01Z",
        },
    ]

    accepted, rejected = anyio.run(
        langsmith_forwarder.forward_trace_batch,
        settings,
        events,
    )

    assert (accepted, rejected) == (2, 0)
    assert [request[2]["run_type"] for request in requests] == ["chain", "chain"]
    assert requests[0][1].endswith("/runs")
    assert requests[0][2]["id"] == requests[0][2]["trace_id"]
    assert requests[0][2]["session_name"] == "actiondesign-test"
    assert "project_name" not in requests[0][2]
    assert requests[1][2]["parent_run_id"] == langsmith_forwarder._uuid("trace")


def test_langsmith_preserves_run_tree_metadata_and_redacts_errors(monkeypatch):
    requests = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            requests.append(("POST", url, kwargs["headers"], kwargs["json"]))
            return FakeResponse()

        async def patch(self, url, **kwargs):
            requests.append(("PATCH", url, kwargs["headers"], kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", FakeClient)
    settings = SimpleNamespace(
        langsmith_enabled=True,
        langsmith_api_key="backend-only-key",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="workspace",
        langsmith_project="actiondesign-test",
        langsmith_timeout_seconds=5,
    )
    events = [
        {
            "eventId": "graph-start", "traceId": "trace", "traceRunId": "trace",
            "name": "actiondesign-agent", "runType": "graph", "event": "start",
            "timestamp": "2026-07-25T00:00:00Z",
            "metadata": {"capabilityDecision": "skill_active", "apiKey": "must-redact"},
        },
        {
            "eventId": "node-start", "traceId": "trace", "traceRunId": "node",
            "parentTraceRunId": "trace", "name": "model", "runType": "chain", "event": "start",
            "timestamp": "2026-07-25T00:00:01Z",
            "inputs": ["wrapped-input"],
        },
        {
            "eventId": "llm-start", "traceId": "trace", "traceRunId": "llm",
            "parentTraceRunId": "node", "name": "mimo-v2.5", "runType": "llm", "event": "start",
            "timestamp": "2026-07-25T00:00:02Z",
        },
        {
            "eventId": "tool-start", "traceId": "trace", "traceRunId": "tool",
            "parentTraceRunId": "node", "name": "gxp_inspect_action", "runType": "tool", "event": "start",
            "timestamp": "2026-07-25T00:00:03Z",
            "metadata": {
                "toolVersion": "1.1.0",
                "skillId": "gxp-lowcode-debug", "skillVersion": "1.2.0",
                "mcpServerId": "gxp-lowcode-readonly", "mcpToolName": "inspect_action",
            },
        },
        {
            "eventId": "tool-error", "traceId": "trace", "traceRunId": "tool",
            "parentTraceRunId": "node", "name": "gxp_inspect_action", "runType": "tool", "event": "error",
            "timestamp": "2026-07-25T00:00:04Z",
            "error": {
                "code": "MCP_TIMEOUT",
                "message": (
                    "password=hunter2 host=db.internal "
                    "config_path=C:\\Users\\agent\\database.json Bearer abc.def"
                ),
                "retryable": True,
            },
            "outputs": ["partial-output"],
            "metadata": {
                "toolVersion": "1.1.0",
                "skillId": "gxp-lowcode-debug", "skillVersion": "1.2.0",
                "mcpServerId": "gxp-lowcode-readonly", "mcpToolName": "inspect_action",
            },
        },
    ]

    accepted, rejected = anyio.run(langsmith_forwarder.forward_trace_batch, settings, events)

    assert (accepted, rejected) == (5, 0)
    post_payloads = [item[3] for item in requests if item[0] == "POST"]
    patch_payload = next(item[3] for item in requests if item[0] == "PATCH")
    assert [payload["run_type"] for payload in post_payloads] == ["chain", "chain", "llm", "tool"]
    assert post_payloads[0]["id"] == post_payloads[0]["trace_id"]
    assert post_payloads[1]["parent_run_id"] == langsmith_forwarder._uuid("trace")
    assert post_payloads[2]["parent_run_id"] == langsmith_forwarder._uuid("node")
    assert post_payloads[3]["parent_run_id"] == langsmith_forwarder._uuid("node")
    assert post_payloads[1]["inputs"] == {"value": ["wrapped-input"]}
    assert all(payload["session_name"] == "actiondesign-test" for payload in post_payloads)
    assert all("project_name" not in payload for payload in post_payloads)
    assert post_payloads[0]["extra"]["metadata"]["apiKey"] == "[REDACTED]"
    assert post_payloads[3]["extra"]["metadata"]["toolVersion"] == "1.1.0"
    assert post_payloads[3]["extra"]["metadata"]["skillId"] == "gxp-lowcode-debug"
    assert post_payloads[3]["extra"]["metadata"]["mcpServerId"] == "gxp-lowcode-readonly"
    assert "hunter2" not in patch_payload["error"]
    assert "db.internal" not in patch_payload["error"]
    assert "C:\\Users" not in patch_payload["error"]
    assert "abc.def" not in patch_payload["error"]
    assert "MCP_TIMEOUT" in patch_payload["error"]
    assert "retryable=True" in patch_payload["error"]
    assert patch_payload["outputs"] == {"value": ["partial-output"]}
    assert patch_payload["trace_id"] == langsmith_forwarder._uuid("trace")
    assert patch_payload["extra"]["metadata"]["traceError"] == {
        "code": "MCP_TIMEOUT", "retryable": True,
    }
    assert patch_payload["extra"]["metadata"]["toolVersion"] == "1.1.0"
    assert patch_payload["extra"]["metadata"]["skillId"] == "gxp-lowcode-debug"
    assert next(item[1] for item in requests if item[0] == "PATCH").endswith(
        f"/runs/{langsmith_forwarder._uuid('tool')}"
    )
    assert all(item[2]["x-api-key"] == "backend-only-key" for item in requests)


def test_langsmith_http_failure_is_counted_without_raising(monkeypatch):
    class FailingResponse:
        def raise_for_status(self):
            raise httpx.HTTPError("telemetry unavailable")

    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return FailingResponse()

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", FailingClient)
    settings = SimpleNamespace(
        langsmith_enabled=True,
        langsmith_api_key="secret",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="",
        langsmith_project="actiondesign-test",
        langsmith_timeout_seconds=5,
    )
    event = {
        "eventId": "graph-start", "traceId": "trace", "traceRunId": "graph",
        "name": "actiondesign-agent", "runType": "graph", "event": "start",
        "timestamp": "2026-07-25T00:00:00Z",
    }

    assert anyio.run(langsmith_forwarder.forward_trace_batch, settings, [event]) == (0, 1)


def test_langsmith_retries_transient_failures_and_accepts_idempotent_conflict(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            calls.append(url)
            status = 503 if len(calls) < 3 else 200
            return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", FakeClient)
    settings = SimpleNamespace(
        langsmith_enabled=True,
        langsmith_api_key="secret",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="",
        langsmith_project="actiondesign-test",
        langsmith_timeout_seconds=5,
    )
    event = {
        "eventId": "graph-start", "traceId": "trace", "traceRunId": "trace",
        "name": "actiondesign-agent", "runType": "graph", "event": "start",
        "timestamp": "2026-07-25T00:00:00Z",
    }

    assert anyio.run(langsmith_forwarder.forward_trace_batch, settings, [event]) == (1, 0)
    assert len(calls) == 3

    calls.clear()

    class ConflictClient(FakeClient):
        async def post(self, url, **_kwargs):
            calls.append(url)
            return httpx.Response(409, request=httpx.Request("POST", url))

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", ConflictClient)
    assert anyio.run(langsmith_forwarder.forward_trace_batch, settings, [event]) == (1, 0)
    assert len(calls) == 1


def test_langsmith_does_not_retry_non_retryable_validation_failure(monkeypatch):
    calls = []

    class InvalidClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            calls.append(url)
            return httpx.Response(400, request=httpx.Request("POST", url))

    monkeypatch.setattr(langsmith_forwarder.httpx, "AsyncClient", InvalidClient)
    settings = SimpleNamespace(
        langsmith_enabled=True,
        langsmith_api_key="secret",
        langsmith_endpoint="https://api.smith.langchain.com",
        langsmith_workspace_id="",
        langsmith_project="actiondesign-test",
        langsmith_timeout_seconds=5,
    )
    event = {
        "eventId": "graph-start", "traceId": "trace", "traceRunId": "trace",
        "name": "actiondesign-agent", "runType": "graph", "event": "start",
        "timestamp": "2026-07-25T00:00:00Z",
    }

    assert anyio.run(langsmith_forwarder.forward_trace_batch, settings, [event]) == (0, 1)
    assert len(calls) == 1
