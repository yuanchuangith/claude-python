import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.app import (
    _require_service_auth,
    _settings_secret_values,
    create_app,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from tests.test_actiondesign_gateway_compatible_api import _client, _create_key, _settings

ORIGIN = "http://cpm.rongchang.gxp2.com:51124"
PREFIX = "/api/actiondesign-agent"


def setup_local_client(tmp_path, peer="127.0.0.1"):
    created = _create_key(_client(tmp_path), "CPM local default")
    settings = _settings(
        tmp_path,
        allow_origins=[ORIGIN],
        local_default_client_origin=ORIGIN,
        local_default_client_api_key=created["key"],
        service_auth_mode="required",
        service_auth_token="s" * 32,
        admin_password="test-admin-password",
    )
    app = create_app(settings, profile_secret_store=MemoryProfileSecretStore())

    @app.get(f"{PREFIX}/test-runtime", dependencies=[Depends(_require_service_auth)])
    def runtime(request: Request):
        return {"keyId": request.state.client_api_key_id}

    return TestClient(app, client=(peer, 50000)), settings, created


def test_local_default_models_runtime_and_revocation(tmp_path):
    client, settings, created = setup_local_client(tmp_path)
    headers = {"Origin": ORIGIN}
    models = client.get(f"{PREFIX}/models", headers=headers)
    assert models.status_code == 200
    assert created["key"] not in models.text
    assert created["key"] in _settings_secret_values(settings)
    runtime = client.get(f"{PREFIX}/test-runtime", headers=headers)
    assert runtime.status_code == 200
    assert runtime.json()["keyId"] == created["id"]
    assert client.get(f"{PREFIX}/admin/profiles", headers=headers).status_code == 401
    assert client.get("/v1/models", headers=headers).status_code == 401
    client.app.state.client_api_keys.delete(created["id"])
    assert client.get(f"{PREFIX}/models", headers=headers).status_code == 401
    assert client.get(f"{PREFIX}/test-runtime", headers=headers).status_code == 401


def test_local_default_origin_can_issue_short_admin_session_without_password(tmp_path):
    client, _, _ = setup_local_client(tmp_path)
    response = client.post(f"{PREFIX}/admin/local-session", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    token = response.json()["sessionToken"]
    profiles = client.get(
        f"{PREFIX}/admin/profiles",
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert profiles.status_code == 200
    assert client.post(
        f"{PREFIX}/admin/local-session",
        headers={"Origin": "http://untrusted.example"},
    ).status_code == 403


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://untrusted.example"},
    {"Origin": ORIGIN, "Authorization": "Bearer invalid"},
    {"Origin": ORIGIN, "X-ActionDesign-Api-Key": "invalid"},
    {"Origin": ORIGIN, "X-ActionDesign-Service-Token": "invalid"},
])
def test_local_default_does_not_replace_explicit_or_missing_identity(tmp_path, headers):
    client, _, _ = setup_local_client(tmp_path)
    assert client.get(f"{PREFIX}/models", headers=headers).status_code == 401
    assert client.get(f"{PREFIX}/test-runtime", headers=headers).status_code == 401


def test_local_default_rejects_non_loopback_peer(tmp_path):
    client, _, _ = setup_local_client(tmp_path, peer="192.0.2.10")
    assert client.get(f"{PREFIX}/models", headers={"Origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("overrides", [
    {"local_default_client_api_key": "test-key"},
    {"local_default_client_origin": ORIGIN},
    {"local_default_client_api_key": "test-key", "local_default_client_origin": "*"},
    {"local_default_client_api_key": "test-key", "local_default_client_origin": ORIGIN},
])
def test_local_default_config_fails_closed(tmp_path, overrides):
    with pytest.raises(ValidationError, match="Local default client"):
        _settings(tmp_path, **overrides)
