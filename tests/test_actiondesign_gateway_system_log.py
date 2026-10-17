import asyncio
import gzip
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway import provider_http
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import (
    MemoryProfileSecretStore,
    ProviderProfile,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings

API = "/api/actiondesign-agent/admin/system-logs"
AUTH = {"Authorization": "Bearer system-admin"}


def client_for(tmp_path, enabled=True):
    settings = Settings(
        system_log_enabled=enabled,
        system_log_path=tmp_path / "system.sqlite3",
        log_root=tmp_path / "audit",
        profile_store_path=tmp_path / "profiles.json",
        request_log_path=tmp_path / "requests.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        browser_api_key_store_path=tmp_path / "browser.json",
        client_api_key_store_path=tmp_path / "clients.json",
        profile_admin_token="system-admin",
        admin_password="test-system-password",
        admin_allow_remote=True,
        admin_require_https_remote=False,
        allow_origins=["*"],
    )
    return TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )


def test_plaintext_all_parameters_responses_filters_and_clear(tmp_path):
    client = client_for(tmp_path)

    @client.app.post("/debug-echo/{item}")
    async def echo(item: str, request: Request):
        return {"item": item, "received": await request.json()}

    payload = {
        "prompt": "中文调试" + "x" * 100_000,
        "password": "plaintext-password",
        "max_tokens": 1234,
        "nested": {"data": "<script>alert(1)</script>"},
        "requestId": "req-debug",
        "conversationId": "conv-debug",
        "runId": "run-debug",
    }
    response = client.post(
        "/debug-echo/42?tag=a&tag=b&empty=",
        json=payload,
        headers={"Authorization": "Bearer plaintext-credential"},
    )
    assert response.status_code == 200
    listing = client.get(API, headers=AUTH).json()
    assert listing["enabled"] is True
    assert listing["total"] == 1
    assert "request" not in listing["items"][0]
    log_id = listing["items"][0]["id"]
    detail = client.get(f"{API}/{log_id}", headers=AUTH)
    assert detail.headers["cache-control"] == "no-store"
    log = detail.json()
    assert log["request"]["body"]["json"] == payload
    assert ["authorization", "Bearer plaintext-credential"] in log["request"]["headers"]
    assert log["request"]["queryParams"] == [["tag", "a"], ["tag", "b"], ["empty", ""]]
    assert log["response"]["body"]["json"] == response.json()
    assert log["complete"] is True
    for params in (
        {"q": "plaintext-password"},
        {"conversationId": "conv-debug"},
        {"requestId": "req-debug", "status": 200, "method": "POST"},
        {"q": "中文调试"},
        {"runId": "run-debug", "direction": "inbound"},
    ):
        assert client.get(API, headers=AUTH, params=params).json()["total"] == 1
    assert (
        client.get(API, headers=AUTH, params={"errorsOnly": True}).json()["total"] == 0
    )
    assert (
        client.get(API, headers=AUTH, params={"q": "' OR 1=1 --"}).json()["total"] == 0
    )
    assert client.get(API, headers=AUTH, params={"offset": 1}).json()["items"] == []
    assert (
        client.get(API, headers=AUTH).json()["total"] == 1
    )  # no recursive viewer logs
    assert client.delete(API, headers=AUTH).json()["deleted"] == 1
    assert client.get(API, headers=AUTH).json()["total"] == 0
    assert client.get(f"{API}/{log_id}", headers=AUTH).status_code == 404


def test_authentication_disabled_capture_and_rejected_request_body(tmp_path):
    client = client_for(tmp_path)
    assert client.get(API).status_code == 401
    assert client.delete(API).status_code == 401
    assert client.get(f"{API}/missing").status_code == 401
    rejected = client.post(
        "/api/actiondesign-agent/admin/login", json={"password": "wrong-password"}
    )
    assert rejected.status_code == 401
    records = client.get(API, headers=AUTH, params={"errorsOnly": True}).json()
    assert records["total"] == 1
    detail = client.get(f"{API}/{records['items'][0]['id']}", headers=AUTH).json()
    assert detail["request"]["body"]["json"]["password"] == "wrong-password"
    assert detail["response"]["body"]["json"] == rejected.json()
    disabled = client_for(tmp_path / "disabled", enabled=False)
    disabled.get("/api/actiondesign-agent/health")
    assert disabled.get(API, headers=AUTH).json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "enabled": False,
    }


def test_provider_actual_payload_sse_and_network_errors(tmp_path, monkeypatch):
    client = client_for(tmp_path)
    original_factory = provider_http._provider_http_client
    profile = ProviderProfile(
        id="debug-profile",
        provider="debug",
        base_url="https://model.test",
        api_key="provider-plaintext",
        default_model="debug-model",
        auth_mode="api-key",
        timeout_seconds=1,
        enabled=True,
        source="builtin",
        protocol="claude",
    )
    expected = b'data: {"text":"first"}\n\ndata: [DONE]\n\n'

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield expected[:10]
            await asyncio.sleep(0)
            yield expected[10:]

    async def handler(request):
        if request.url.path == "/network-error":
            raise httpx.ConnectError("debug network error", request=request)
        return httpx.Response(
            200, stream=ProviderStream(), headers={"content-type": "text/event-stream"}
        )

    def factory(profile):
        result = original_factory(profile)
        result._transport = httpx.MockTransport(handler)
        result._mounts.clear()
        return result

    monkeypatch.setattr(provider_http, "_provider_http_client", factory)

    @client.app.post("/debug-upstream")
    async def upstream(request: Request):
        payload = await request.json()
        async with (
            provider_http._provider_http_client(profile) as upstream_client,
            upstream_client.stream(
                "POST",
                "https://model.test/v1/messages",
                json=payload,
                headers={"x-api-key": profile.api_key},
            ) as response,
        ):
            raw = await response.aread()

        async def chunks():
            for chunk in (raw[:10], raw[10:]):
                yield chunk
                await asyncio.sleep(0)

        return StreamingResponse(chunks(), media_type="text/event-stream")

    @client.app.get("/debug-network")
    async def network():
        async with provider_http._provider_http_client(profile) as upstream_client:
            try:
                await upstream_client.get("https://model.test/network-error")
            except httpx.ConnectError:
                return {"error": "unreachable"}

    assert (
        client.post(
            "/debug-upstream",
            json={"model": "debug-model", "system": "all instructions"},
        ).content
        == expected
    )
    records = client.get(API, headers=AUTH).json()["items"]
    outbound = next(record for record in records if record["direction"] == "outbound")
    inbound = next(record for record in records if record["direction"] == "inbound")
    assert outbound["parentId"] == inbound["id"]
    raw_log = client.get(f"{API}/{outbound['id']}", headers=AUTH).json()
    assert raw_log["request"]["body"]["json"]["system"] == "all instructions"
    assert ["x-api-key", "provider-plaintext"] in raw_log["request"]["headers"]
    assert raw_log["response"]["body"]["text"] == expected.decode()
    assert raw_log["complete"] is True
    assert (
        client.get(
            API, headers=AUTH, params={"provider": "debug", "model": "debug-model"}
        ).json()["total"]
        == 1
    )
    detail = client.get(f"{API}/{inbound['id']}", headers=AUTH).json()
    assert detail["response"]["body"]["text"] == expected.decode()
    client.get("/debug-network")
    errors = client.get(API, headers=AUTH, params={"errorsOnly": True}).json()["items"]
    assert len(errors) == 1
    assert "ConnectError" in errors[0]["error"]


def test_time_filter_validation_and_timezone_normalization(tmp_path):
    client = client_for(tmp_path)
    client.get("/api/actiondesign-agent/health")
    now = datetime.now(timezone(timedelta(hours=8)))
    params = {
        "startedAt": (now - timedelta(minutes=1)).isoformat(),
        "endedAt": (now + timedelta(minutes=1)).isoformat(),
    }
    assert client.get(API, headers=AUTH, params=params).json()["total"] == 1
    assert (
        client.get(
            API,
            headers=AUTH,
            params={"startedAt": params["endedAt"], "endedAt": params["startedAt"]},
        ).status_code
        == 422
    )
    assert (
        client.get(
            API, headers=AUTH, params={"startedAt": "2026-10-16T00:00:00"}
        ).status_code
        == 422
    )


def test_binary_compressed_response_and_unhandled_error(tmp_path):
    from fastapi.responses import Response

    client = client_for(tmp_path)

    @client.app.get("/compressed")
    async def compressed():
        return Response(
            gzip.compress("完整返回正文".encode()), headers={"content-encoding": "gzip"}
        )

    @client.app.post("/binary")
    async def binary(request: Request):
        return Response(await request.body(), media_type="application/octet-stream")

    @client.app.get("/unhandled")
    async def unhandled():
        raise RuntimeError("debug exception detail")

    client.get("/compressed")
    client.post("/binary", content=b"\xff\xfe\x00")
    with pytest.raises(RuntimeError):
        client.get("/unhandled")
    records = client.get(API, headers=AUTH).json()["items"]
    details = {
        item["path"]: client.get(f"{API}/{item['id']}", headers=AUTH).json()
        for item in records
    }
    assert details["/compressed"]["response"]["body"]["text"] == "完整返回正文"
    assert details["/binary"]["request"]["body"]["encoding"] == "base64"
    assert details["/binary"]["response"]["body"]["encoding"] == "base64"
    assert details["/unhandled"]["status"] == 500
    assert "debug exception detail" in details["/unhandled"]["error"]
    assert (
        details["/unhandled"]["response"]["body"]["json"]["detail"]["code"]
        == "GATEWAY_INTERNAL_ERROR"
    )


@pytest.mark.parametrize("raw", [b'{"x": NaN}', b'{"model": "\\ud800"}'])
def test_malformed_json_stays_readable_as_original_text(tmp_path, raw):
    client = client_for(tmp_path)
    client.post("/missing-route", content=raw)
    item = client.get(API, headers=AUTH).json()["items"][0]
    detail = client.get(f"{API}/{item['id']}", headers=AUTH).json()
    assert detail["request"]["body"]["text"] == raw.decode()
    assert "json" not in detail["request"]["body"]


def test_sse_is_forwarded_before_stream_finishes_and_partial_response_is_logged(
    tmp_path,
):
    from claude_agent_sdk.actiondesign_gateway.system_log import (
        SystemLogMiddleware,
        SystemLogStore,
    )

    async def scenario():
        store = SystemLogStore(tmp_path / "stream.sqlite3")
        delivered = asyncio.Event()
        finish = asyncio.Event()

        async def stream_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: first\n\n",
                    "more_body": True,
                }
            )
            await finish.wait()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                delivered.set()

        middleware = SystemLogMiddleware(stream_app, store, error_handler=None)
        task = asyncio.create_task(
            middleware(
                {"type": "http", "method": "GET", "path": "/stream", "headers": []},
                receive,
                send,
            )
        )
        await asyncio.wait_for(delivered.wait(), 2)
        assert (
            not task.done()
        )  # First SSE chunk reached the client without waiting for completion.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        item = store.list(limit=1, offset=0, filters={})["items"][0]
        detail = store.get(item["id"])
        assert detail["complete"] is False
        assert detail["response"]["body"]["text"] == "data: first\n\n"
        assert "CancelledError" in detail["error"]

    asyncio.run(scenario())
