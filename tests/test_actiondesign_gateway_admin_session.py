import pytest
from fastapi.testclient import TestClient

from claude_agent_sdk.actiondesign_gateway.admin_security import AdminSessionStore
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from claude_agent_sdk.actiondesign_gateway.settings import Settings

SESSION = "/api/actiondesign-agent/admin/session"
LOGIN = "/api/actiondesign-agent/admin/login"
HEADERS = {"Origin": "http://testserver", "X-ActionDesign-Admin-Session": "1"}


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        admin_password="session-test-password",
        admin_session_ttl_seconds=300,
        admin_allow_remote=True,
        admin_require_https_remote=False,
        allow_origins=["http://allowed.test"],
        log_root=tmp_path / "audit",
        profile_store_path=tmp_path / "profiles.json",
        full_conversation_log_root=tmp_path / "conversations",
        request_log_path=tmp_path / "requests.jsonl",
        system_log_enabled=False,
    )
    return TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )


def login(client):
    response = client.post(
        LOGIN,
        json={"password": "session-test-password"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    return response


def test_renewal_extends_expiry_but_does_not_revive_expired_or_revoked_sessions():
    now = [1000.0]
    store = AdminSessionStore("password", ttl_seconds=300, clock=lambda: now[0])
    issued, _ = store.login("password")
    token = issued["sessionToken"]
    now[0] = 1299
    renewed = store.renew(token)
    assert renewed["sessionToken"] == token
    assert renewed["expiresAt"] != issued["expiresAt"]
    now[0] = 1301
    assert store.verify(token)
    store.revoke(token)
    assert not store.verify(token)
    assert store.renew(token) is None
    issued, _ = store.login("password")
    now[0] += 301
    assert store.renew(issued["sessionToken"]) is None


def test_cookie_restores_session_without_authorizing_other_admin_routes(client):
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Path={SESSION}" in cookie
    assert response.headers["cache-control"] == "no-store"
    restored = client.post(SESSION, headers=HEADERS)
    assert restored.status_code == 200
    assert restored.json()["sessionToken"] == response.json()["sessionToken"]
    assert restored.headers["cache-control"] == "no-store"
    assert client.get("/api/actiondesign-agent/admin/providers").status_code == 401
    auth = {"Authorization": "Bearer " + restored.json()["sessionToken"]}
    assert (
        client.get("/api/actiondesign-agent/admin/providers", headers=auth).status_code
        == 200
    )
    assert client.get("/api/actiondesign-agent/models", headers=auth).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://testserver"},
        {"Origin": "http://allowed.test", "X-ActionDesign-Admin-Session": "1"},
        {"Origin": "http://evil.test", "X-ActionDesign-Admin-Session": "1"},
    ],
)
def test_cookie_bootstrap_and_logout_reject_cross_origin_or_missing_guard(
    client, headers
):
    login(client)
    assert client.post(SESSION, headers=headers).status_code == 403
    assert client.delete(SESSION, headers=headers).status_code == 403


def test_logout_revokes_bearer_and_cookie_and_prevents_reload_login(client):
    token = login(client).json()["sessionToken"]
    response = client.delete(
        SESSION, headers={**HEADERS, "Authorization": "Bearer " + token}
    )
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert (
        client.get(
            "/api/actiondesign-agent/admin/providers",
            headers={"Authorization": "Bearer " + token},
        ).status_code
        == 401
    )
    assert client.post(SESSION, headers=HEADERS).status_code == 401


def test_expired_bootstrap_clears_cookie_and_invalid_bearer_does_not_fall_back(client):
    login(client)
    response = client.post(
        SESSION, headers={**HEADERS, "Authorization": "Bearer invalid"}
    )
    assert response.status_code == 401
    assert "Max-Age=0" in response.headers["set-cookie"]
    login(client)
    store = client.app.state.admin_sessions
    original_now = store._clock()
    store._clock = lambda: original_now + 301
    assert client.post(SESSION, headers=HEADERS).status_code == 401
