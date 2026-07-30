import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.session_log import (
    list_conversations,
    read_conversation,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings


def test_admin_conversation_queries_redact_historically_polluted_events(tmp_path):
    conversation_id = "legacy_conversation"
    path = tmp_path / f"20260729_120000_{conversation_id}.jsonl"
    polluted_events = [
        {
            "type": "message",
            "time": "authorization=Bearer started-at-secret",
            "authorization": "Bearer top-level-secret",
            "nested": {
                "apiKey": "nested-api-secret",
                "items": [
                    {"password": "nested-password"},
                    "token=inline-token-secret",
                ],
            },
        },
        {
            "type": "conversation_sealed",
            "status": "password=status-secret",
            "incompleteReason": "Bearer incomplete-secret",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(item) for item in polluted_events) + "\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(full_conversation_log_root=tmp_path)

    events, event_total = read_conversation(
        settings,
        conversation_id,
        limit=100,
        offset=0,
    )
    summaries, summary_total = list_conversations(
        settings,
        limit=100,
        offset=0,
    )

    serialized_events = json.dumps(events, ensure_ascii=False)
    serialized_summaries = json.dumps(summaries, ensure_ascii=False)
    for secret in (
        "started-at-secret",
        "top-level-secret",
        "nested-api-secret",
        "nested-password",
        "inline-token-secret",
        "status-secret",
        "incomplete-secret",
    ):
        assert secret not in serialized_events
        assert secret not in serialized_summaries
    assert event_total == 2
    assert summary_total == 1
    assert summaries[0]["status"] == "open"
    assert summaries[0]["incompleteReason"] == "invalid_conversation_status"
    assert events[0]["authorization"] == "[REDACTED]"
    assert events[0]["nested"]["apiKey"] == "[REDACTED]"
    assert events[0]["nested"]["items"][0]["password"] == "[REDACTED]"
    assert "[REDACTED]" in serialized_summaries

    persisted = path.read_text(encoding="utf-8")
    assert "top-level-secret" in persisted
    assert "nested-api-secret" in persisted


def test_admin_conversation_filters_and_event_pagination(tmp_path):
    conversation_id = "88888888-8888-4888-8888-888888888888"
    run_id = "run_88888888-8888-4888-8888-888888888888"
    request_id = "req_conversation_filter"
    now = datetime.now(timezone.utc)
    events = [
        {
            "type": "gateway_request",
            "time": (now + timedelta(seconds=index)).isoformat(),
            "conversationId": conversation_id,
            "requestId": request_id,
            "runId": run_id,
            "provider": "mimo",
            "request": {"model": "mimo-v2.5", "prompt": f"turn-{index}"},
        }
        for index in range(75)
    ]
    events.append(
        {
            "type": "conversation_sealed",
            "time": (now + timedelta(seconds=80)).isoformat(),
            "status": "completed",
        }
    )
    path = tmp_path / "conversations" / f"20260729_120000_{conversation_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
        encoding="utf-8",
    )
    settings = Settings(
        allow_origins=["*"],
        log_root=tmp_path / "logs",
        profile_store_path=tmp_path / "profiles.json",
        request_log_path=tmp_path / "request-logs.jsonl",
        full_conversation_log_root=path.parent,
        profile_admin_token="admin-token",
        admin_allow_remote=True,
        admin_require_https_remote=False,
        profile_allowed_hosts=[
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )
    headers = {"Authorization": "Bearer admin-token"}

    listing = client.get(
        "/api/actiondesign-agent/admin/conversations",
        headers=headers,
        params={
            "conversationId": conversation_id,
            "requestId": request_id,
            "runId": run_id,
            "provider": "mimo",
            "model": "mimo-v2.5",
            "status": "completed",
        },
    )
    detail = client.get(
        f"/api/actiondesign-agent/admin/conversations/{conversation_id}",
        headers=headers,
        params={
            "limit": 10,
            "offset": 20,
            "type": "gateway_request",
            "requestId": request_id,
            "runId": run_id,
        },
    )

    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["status"] == "completed"
    assert detail.status_code == 200
    assert detail.json()["total"] == 75
    assert len(detail.json()["items"]) == 10
    assert detail.json()["items"][0]["request"]["prompt"] == "turn-20"
