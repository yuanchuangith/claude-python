import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway import business_knowledge
from claude_agent_sdk.actiondesign_gateway.app import create_app
from claude_agent_sdk.actiondesign_gateway.profile_store import MemoryProfileSecretStore
from tests.test_actiondesign_gateway_compatible_api import _settings

PREFIX = "/api/actiondesign-agent/knowledge"
KB = "11111111-1111-4111-8111-111111111111"
CHUNK = "22222222-2222-4222-8222-222222222222"
AUTH = {"X-ActionDesign-Service-Token": "service-test-token-32-characters-minimum"}
VERSION = "sha256:" + "a" * 64


def client_for(tmp_path, **overrides):
    settings = _settings(
        tmp_path, service_auth_mode="required", service_auth_token="service-test-token-32-characters-minimum",
        localdoc_base_url="http://127.0.0.1:7860", localdoc_knowledge_base_id=KB,
        **overrides,
    )
    return TestClient(create_app(settings, profile_secret_store=MemoryProfileSecretStore()))


def mock_upstream(monkeypatch, handler):
    original = httpx.AsyncClient

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(business_knowledge.httpx, "AsyncClient", factory)


def test_existing_auth_and_config_and_read_only_routes(tmp_path):
    client = client_for(tmp_path)
    assert client.get(PREFIX + "/config").status_code == 401
    response = client.get(PREFIX + "/config", headers=AUTH)
    assert response.json() == {
        "enabled": True, "knowledgeBaseId": KB,
        "processing": {"retrieval": "local", "model": "configured_gateway"},
    }
    assert response.headers["cache-control"] == "no-store"
    assert "127.0.0.1:7860" not in response.text
    for method, path in [("delete", "/documents/" + CHUNK), ("post", "/documents/" + CHUNK), ("post", "/upload")]:
        assert client.request(method, PREFIX + path, headers=AUTH).status_code in {404, 405}


def test_scope_is_fixed_and_credentials_are_not_forwarded(tmp_path, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["origin"] == "http://127.0.0.1:7860"
        assert request.headers["x-localdoc-request"] == "1"
        assert not {"authorization", "cookie", "x-actiondesign-service-token", "sec-fetch-site"} & set(request.headers)
        if request.method == "POST":
            data = json.loads(request.content)
            assert data["knowledge_base_id"] == KB
            assert data["result_version"] == VERSION
            return httpx.Response(200, json={"results": [{"path": "/documents/manual/preview?version_id=one"}], "resultVersion": VERSION})
        assert request.url.params["knowledge_base_id"] == KB
        return httpx.Response(200, json={"id": "localdoc:" + CHUNK, "path": "/documents/manual/preview?version_id=one",
            "metadata": {"status": "evidence_only", "source": {"preview_url": "/documents/manual/preview?version_id=one"}}})

    mock_upstream(monkeypatch, upstream)
    client = client_for(tmp_path)
    headers = {**AUTH, "Origin": "https://platform.example", "Cookie": "browser=private", "Sec-Fetch-Site": "cross-site"}
    response = client.post(PREFIX + "/search", json={"query": "培训", "result_version": VERSION}, headers=headers)
    assert response.status_code == 200
    assert response.json()["results"][0]["path"].startswith("http://127.0.0.1:7860/documents/")
    response = client.get(PREFIX + "/documents/" + CHUNK, headers=headers)
    assert response.json()["metadata"]["status"] == "evidence_only"
    assert response.json()["path"] == response.json()["metadata"]["source"]["preview_url"]
    assert response.json()["path"].startswith("http://127.0.0.1:7860/documents/")
    assert len(calls) == 2
    assert client.post(PREFIX + "/search", json={"query": "培训", "knowledge_base_id": CHUNK}, headers=AUTH).status_code == 403
    assert client.get(PREFIX + "/documents/" + CHUNK + "?knowledge_base_id=" + CHUNK, headers=AUTH).status_code == 403
    assert client.get(PREFIX + "/documents/not-a-uuid", headers=AUTH).status_code == 422
    assert client.post(PREFIX + "/search", json={"query": "培训", "url": "http://elsewhere"}, headers=AUTH).status_code == 422
    for invalid in [{"query": "x" * 501}, {"offset": 1001}, {"result_version": "version-one"}]:
        assert client.post(PREFIX + "/search", json={"query": "培训", **invalid}, headers=AUTH).status_code == 422
    assert len(calls) == 2


def test_disabled_is_explicit(tmp_path):
    settings = _settings(tmp_path, localdoc_base_url="", localdoc_knowledge_base_id="")
    client = TestClient(create_app(settings, profile_secret_store=MemoryProfileSecretStore()))
    assert client.get(PREFIX + "/config").json() == {"enabled": False}
    assert client.post(PREFIX + "/search", json={"query": "培训"}).status_code == 503


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.2", "http://127.0.0.1/path", "http://user:password@localhost", "http://localhost?target=x", "http://localhost:99999", "http://[::1]:7860"])
def test_configuration_cannot_select_remote_or_arbitrary_path(tmp_path, url):
    with pytest.raises(ValidationError):
        _settings(tmp_path, localdoc_base_url=url, localdoc_knowledge_base_id=KB)


def test_library_id_is_canonical_before_config_and_scope_comparison(tmp_path):
    settings = _settings(tmp_path, localdoc_base_url="http://localhost:7860", localdoc_knowledge_base_id="CB65EB15C69F42AAB53BA44615B04F0B")
    assert settings.localdoc_knowledge_base_id == "cb65eb15-c69f-42aa-b53b-a44615b04f0b"


def test_version_conflict_preserved_and_upstream_errors_bounded(tmp_path, monkeypatch):
    mode = "conflict"

    def upstream(request):
        if mode == "conflict":
            return httpx.Response(409, json={"code": "KNOWLEDGE_RESULT_VERSION_CHANGED", "message": "Restart search"})
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://remote.example"})
        raise httpx.ConnectError("private path /credentials", request=request)

    mock_upstream(monkeypatch, upstream)
    client = client_for(tmp_path)
    assert client.post(PREFIX + "/search", json={"query": "培训"}, headers=AUTH).status_code == 409
    for next_mode in ["redirect", "unavailable"]:
        mode = next_mode
        response = client.post(PREFIX + "/search", json={"query": "培训"}, headers=AUTH)
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "BUSINESS_KNOWLEDGE_UNAVAILABLE"
        assert "credentials" not in response.text
