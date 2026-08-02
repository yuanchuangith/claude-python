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
        admin_require_https_remote=False,
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
    assert "/admin/providers" in script.text
    assert "/admin/profiles/" in script.text
    assert "/default-model" in script.text
    assert "/models" in script.text
    assert "/connection-test" in script.text
    assert "/admin/channels/health" in script.text
    assert "/admin/runtime-policy" in script.text
    assert "/admin/secrets" in script.text
    assert "Client API Key · 唯一业务访问 Key" in page.text
    assert "不授予任何后台管理权限" in page.text
    assert "测试阶段创建后完整值会在当前页面持续显示" in page.text
    assert "完整 Key 仅显示一次" not in page.text
    assert "Browser API Key" not in page.text
    assert "/admin/access-key" not in script.text
    assert 'document.execCommand("copy")' in script.text
    assert "navigator.clipboard?.writeText" in script.text
    assert "setTimeout(clearDisplayedClientKey" not in script.text
    assert 'document.addEventListener("visibilitychange"' not in script.text
    assert "secret.masked" not in script.text
    assert "/admin/audit-logs" in script.text
    assert "/admin/conversations" in script.text
    assert "/admin/request-logs" in script.text
    assert "routingOrder" in script.text
    assert 'id="model-metric-default-provider"' in page.text
    assert 'id="model-metric-default-model"' in page.text
    assert 'id="model-metric-route-count"' in page.text
    assert "渠道默认" in page.text
    assert 'route.position === 1' in script.text
    assert 'badge("全局默认路由", "green")' in script.text
    assert 'badge(`路由 #${route.position}`, "")' in script.text
    assert 'cache: "no-store"' in script.text
    assert 'redirect: "error"' in script.text
    assert 'referrerPolicy: "no-referrer"' in script.text
    assert "clearSensitiveUi" in script.text
    assert 'document.body.classList.toggle("authenticated", loggedIn)' in script.text
    assert "ADMIN_REMOTE_DISABLED" in script.text
    assert "禁止公网管理登录" in script.text
    assert "body:not(.authenticated) .sidebar" in stylesheet.text
    assert "body:not(.authenticated) .view" in stylesheet.text
    assert '<input id="provider-type" required maxlength="64"' in page.text
    assert 'pattern="[A-Za-z0-9_-]+"' in page.text
    assert 'list="provider-groups"' in page.text
    assert '<datalist id="provider-groups">' in page.text
    assert '<select id="provider-type"' not in page.text
    assert '<input id="provider-api-key" type="password" required' in page.text
    assert 'list="provider-model-options"' in page.text
    assert '<datalist id="provider-model-options"></datalist>' in page.text
    assert 'id="discover-provider-models"' in page.text
    assert 'type="button">获取模型</button>' in page.text
    assert 'id="save-test-provider"' in page.text
    assert 'event.submitter.id === "save-test-provider"' in script.text
    assert 'apiKey: $("provider-api-key").value' in script.text
    assert '/models/discover' in script.text
    assert '新建渠道获取模型前必须填写 API Key' in script.text
    assert 'button.textContent = "获取中…"' in script.text
    assert 'clearProviderModelCandidates' in script.text
    assert 'defaultModel: defaultModelId' in script.text
    assert 'models.some((model) => model.id === defaultModelId)' in script.text
    assert 'PROFILE_HOST_NOT_ALLOWED' in script.text
    assert 'ACTIONDESIGN_PROFILE_ALLOWED_HOSTS' in script.text
    assert "已保存，但连接测试失败" in script.text
    assert 'const PROVIDER_GROUPS = [' in script.text
    assert 'const suggestions = $("provider-groups")' in script.text
    assert 'populateProviderGroups(defaultProviderGroup($("provider-protocol").value))' in script.text
    assert 'profile.source === "environment"' in script.text
    assert "持久化隐藏该环境渠道" in script.text
    assert "profile-key" not in page.text
    assert "EVENT_PAGE_SIZE" in script.text
    assert "admin/conversations/${encodeURIComponent(summary.conversationId)}?limit=500" not in script.text
    assert "innerHTML" not in script.text
    assert "insertAdjacentHTML" not in script.text
    assert "document.write" not in script.text
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
        admin_require_https_remote=False,
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


def test_admin_runtime_policy_requires_bearer_and_exposes_only_boundary_metadata(tmp_path):
    settings = Settings(
        allow_origins=["*"],
        log_root=tmp_path / "logs",
        profile_store_path=tmp_path / "profiles.json",
        request_log_path=tmp_path / "request-logs.jsonl",
        full_conversation_log_root=tmp_path / "conversations",
        browser_api_key_store_path=tmp_path / "browser-key.json",
        profile_allowed_hosts=[
            "api.example.test",
            "api.anthropic.com",
            "token-plan-cn.xiaomimimo.com",
        ],
        mcp_allowed_hosts=["mcp.example.test"],
        admin_password="test-admin-password",
        admin_allow_remote=True,
        admin_require_https_remote=False,
    )
    client = TestClient(
        create_app(settings, profile_secret_store=MemoryProfileSecretStore())
    )

    denied = client.get("/api/actiondesign-agent/admin/runtime-policy")
    login = client.post(
        "/api/actiondesign-agent/admin/login",
        headers={"Origin": "http://testserver"},
        json={"password": "test-admin-password"},
    )
    response = client.get(
        "/api/actiondesign-agent/admin/runtime-policy",
        headers={"Authorization": f"Bearer {login.json()['sessionToken']}"},
    )

    assert denied.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "profileAllowedHosts": [
            "api.anthropic.com",
            "api.example.test",
            "token-plan-cn.xiaomimimo.com",
        ],
        "providerPaths": {
            "openai": "/v1/chat/completions",
            "claude": "/v1/messages",
        },
        "retryPolicy": {
            "mode": "retryable-failover",
            "attemptsPerRoute": 1,
            "retryableErrorsOnly": True,
            "partialStreamFailover": False,
        },
    }
    assert "secret" not in response.text.lower()
