import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from claude_agent_sdk.actiondesign_gateway.app import create_app  # noqa: E402
from claude_agent_sdk.actiondesign_gateway.profile_store import (  # noqa: E402
    MemoryProfileSecretStore,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings  # noqa: E402


def test_admin_ui_is_served_without_bundled_credentials(tmp_path):
    settings = Settings(
        allow_origins=["*"],
        log_root=tmp_path / "logs",
        profile_store_path=tmp_path / "profiles.json",
        request_log_path=tmp_path / "request-logs.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        browser_api_key_store_path=tmp_path / "browser-key.json",
        profile_allowed_hosts=[
            "api.example.test",
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
        admin_password="test-admin-password",
        admin_allow_remote=True,
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    page = client.get("/admin/")
    script = client.get("/admin/admin.js")
    stylesheet = client.get("/admin/admin.css")

    assert page.status_code == 200
    assert "ActionDesign Gateway 管理中心" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert "/admin/login" in script.text
    assert "/admin/profiles" in script.text
    assert "/admin/audit-logs" in script.text
    assert "/admin/conversations" in script.text
    assert "/admin/request-logs" in script.text
    assert "routingOrder" in script.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "test-admin-password" not in page.text + script.text


def test_admin_ui_same_origin_login_is_allowed_without_cors_origin_entry(tmp_path):
    settings = Settings(
        allow_origins=["*"],
        log_root=tmp_path / "logs",
        profile_store_path=tmp_path / "profiles.json",
        request_log_path=tmp_path / "request-logs.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        browser_api_key_store_path=tmp_path / "browser-key.json",
        profile_allowed_hosts=[
            "api.example.test",
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
        admin_password="test-admin-password",
        admin_allow_remote=True,
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    response = client.post(
        "/api/actiondesign-agent/admin/login",
        headers={"Origin": "http://testserver"},
        json={"password": "test-admin-password"},
    )

    assert response.status_code == 200
    assert response.json()["sessionToken"]
