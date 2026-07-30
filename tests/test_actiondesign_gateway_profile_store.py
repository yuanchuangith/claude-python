import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.models import (
    ApiProfileWriteRequest,
    ProviderModelDiscoveryRequest,
    ToolResultRequest,
)
from claude_agent_sdk.actiondesign_gateway.profile_store import (
    ApiProfileStore,
    MemoryProfileSecretStore,
    ProfileSecretStoreError,
)
from claude_agent_sdk.actiondesign_gateway.settings import Settings


def settings(tmp_path):
    return Settings(
        mimo_api_key="mimo-secret",
        claude_code_api_key="claude-secret",
        profile_store_path=tmp_path / "profiles.json",
        profile_allowed_hosts=[
            "api.example.test",
            "token-plan-cn.xiaomimimo.com",
            "api.anthropic.com",
        ],
    )


def request(api_key="managed-secret", base_url="https://api.example.test/provider"):
    return ApiProfileWriteRequest(
        id="managed",
        provider="mimo",
        baseUrl=base_url,
        apiKey=api_key,
        defaultModel="mimo-v2.5",
    )


def discovery_request(
    *,
    api_key="",
    base_url="https://api.example.test/provider",
    protocol="claude",
    auth_mode="api-key",
    timeout_seconds=30,
):
    return ProviderModelDiscoveryRequest(
        protocol=protocol,
        baseUrl=base_url,
        apiKey=api_key,
        authMode=auth_mode,
        timeoutSeconds=timeout_seconds,
    )


def test_managed_profile_persists_only_secret_reference_and_never_plaintext(tmp_path):
    secrets = MemoryProfileSecretStore()
    store = ApiProfileStore(settings(tmp_path), secrets)

    public = store.upsert(request())
    persisted_text = (tmp_path / "profiles.json").read_text(encoding="utf-8")
    payload = json.loads(persisted_text)

    assert public["keyConfigured"] is True
    assert "api_key" not in public
    assert payload["schemaVersion"] == "5"
    assert payload["profiles"][0]["secret_ref"].startswith("profile:")
    assert "api_key" not in payload["profiles"][0]
    assert "managed-secret" not in persisted_text
    assert secrets.get(payload["profiles"][0]["secret_ref"]) == "managed-secret"


def test_blank_key_preserves_existing_managed_secret(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())
    store.upsert(request())
    store.upsert(request(api_key=""))

    assert store.resolve("mimo", "managed").api_key == "managed-secret"


def test_model_discovery_profile_reuses_secret_without_persisting(tmp_path):
    secrets = MemoryProfileSecretStore()
    store = ApiProfileStore(settings(tmp_path), secrets)
    store.upsert(request())
    persisted_before = (tmp_path / "profiles.json").read_bytes()

    discovery = store.build_model_discovery_profile(
        "managed",
        discovery_request(timeout_seconds=45),
    )

    assert discovery.api_key == "managed-secret"
    assert discovery.timeout_seconds == 45
    assert discovery.source == "ephemeral"
    assert (tmp_path / "profiles.json").read_bytes() == persisted_before


def test_model_discovery_requires_key_for_new_or_changed_route(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())

    with pytest.raises(HTTPException) as missing:
        store.build_model_discovery_profile("unsaved", discovery_request())
    assert missing.value.detail["code"] == "API_PROFILE_KEY_MISSING"

    store.upsert(request())
    with pytest.raises(HTTPException) as changed:
        store.build_model_discovery_profile(
            "managed",
            discovery_request(
                base_url="https://token-plan-cn.xiaomimimo.com/anthropic",
            ),
        )
    assert changed.value.detail["code"] == "PROFILE_KEY_REQUIRED_FOR_ROUTE_CHANGE"


def test_model_discovery_keeps_profile_host_allowlist(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())

    with pytest.raises(HTTPException) as blocked:
        store.build_model_discovery_profile(
            "unsaved",
            discovery_request(
                api_key="submitted-secret",
                base_url="https://attacker.example/v1",
            ),
        )

    assert blocked.value.detail["code"] == "PROFILE_HOST_NOT_ALLOWED"


def test_dynamic_provider_uses_its_real_ready_default_profile(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())
    store.upsert(
        ApiProfileWriteRequest(
            id="openai-primary",
            provider="team-openai",
            displayName="Team OpenAI",
            protocol="openai",
            baseUrl="https://api.example.test/v1",
            apiKey="managed-secret",
            defaultModel="gpt-5.1",
            models=[{"id": "gpt-5.1", "name": "GPT 5.1"}],
            authMode="bearer",
        )
    )

    assert store.default_profile_id("team-openai") == "openai-primary"
    assert store.resolve("team-openai").id == "openai-primary"


def test_route_change_requires_explicit_key_to_prevent_cross_provider_leak(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())
    store.upsert(request())
    changed_route = request(
        api_key="",
        base_url="https://token-plan-cn.xiaomimimo.com/anthropic",
    )

    with pytest.raises(HTTPException) as error:
        store.upsert(changed_route)

    assert error.value.status_code == 400
    assert error.value.detail["code"] == "PROFILE_KEY_REQUIRED_FOR_ROUTE_CHANGE"
    assert store.resolve("mimo", "managed").base_url == (
        "https://api.example.test/provider"
    )


def test_valid_managed_profile_is_strictly_reloaded(tmp_path):
    secrets = MemoryProfileSecretStore()
    store = ApiProfileStore(settings(tmp_path), secrets)
    store.upsert(request())

    reloaded = ApiProfileStore(settings(tmp_path), secrets)

    assert reloaded.resolve("mimo", "managed").api_key == "managed-secret"


@pytest.mark.parametrize(
    "payload",
    [
        {"schemaVersion": "2", "profiles": [], "unknown": True},
        {
            "schemaVersion": "2",
            "profiles": [
                {
                    "id": "managed",
                    "provider": "mimo",
                    "base_url": "https://api.example.test/provider",
                    "secret_ref": None,
                    "default_model": "mimo-v2.5",
                    "auth_mode": "api-key",
                    "timeout_seconds": 120.0,
                    "enabled": "true",
                    "source": "managed",
                }
            ],
        },
        {
            "schemaVersion": "2",
            "profiles": [
                {
                    "id": "managed",
                    "provider": "mimo",
                    "base_url": "https://api.example.test/provider",
                    "secret_ref": None,
                    "default_model": "mimo-v2.5",
                    "auth_mode": "api-key",
                    "timeout_seconds": 120.0,
                    "enabled": True,
                    "source": "environment",
                }
            ],
        },
    ],
)
def test_invalid_persisted_profile_file_fails_closed(tmp_path, payload):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="managed profile store is invalid"):
        ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())


def test_legacy_plaintext_profile_file_is_rejected(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "managed",
                        "provider": "mimo",
                        "base_url": "https://api.example.test/provider",
                        "api_key": "legacy-plaintext-secret",
                        "default_model": "mimo-v2.5",
                        "auth_mode": "api-key",
                        "timeout_seconds": 120.0,
                        "enabled": True,
                        "source": "managed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="managed profile store is invalid"):
        ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())


def test_profile_host_allowlist_blocks_arbitrary_forwarding(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())

    with pytest.raises(HTTPException) as error:
        store.upsert(request(base_url="https://attacker.example/v1"))

    assert error.value.detail["code"] == "PROFILE_HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.test/v1?api_key=not-allowed",
        "https://api.example.test/v1#not-allowed",
    ],
)
def test_profile_url_rejects_query_parameters_and_fragments(base_url):
    with pytest.raises(ValidationError, match="query parameters or fragments"):
        request(base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.anthropic.com/v1/messages?api_key=not-allowed",
        "https://api.anthropic.com/v1/messages#not-allowed",
    ],
)
def test_environment_provider_url_rejects_query_parameters_and_fragments(base_url):
    with pytest.raises(ValidationError, match="query parameters or fragments"):
        Settings(mimo_messages_url=base_url)


def test_environment_profile_host_is_validated_before_registration(tmp_path):
    gateway_settings = Settings(
        mimo_api_key="mimo-secret",
        claude_code_api_key="claude-secret",
        mimo_messages_url="https://attacker.example/v1/messages",
        profile_store_path=tmp_path / "profiles.json",
        profile_allowed_hosts=["api.anthropic.com"],
    )

    with pytest.raises(ValueError, match="PROFILE_HOST_NOT_ALLOWED"):
        ApiProfileStore(gateway_settings, MemoryProfileSecretStore())


def test_environment_profile_delete_is_persisted_and_removed_from_routing(tmp_path):
    gateway_settings = settings(tmp_path)
    store = ApiProfileStore(gateway_settings, MemoryProfileSecretStore())

    assert store.delete("default-mimo") is True

    payload = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    reloaded = ApiProfileStore(gateway_settings, MemoryProfileSecretStore())
    assert payload["schemaVersion"] == "5"
    assert payload["environmentOverrides"][0]["id"] == "default-mimo"
    assert payload["environmentOverrides"][0]["deleted"] is True
    assert "default-mimo" not in {item["id"] for item in reloaded.list_public()}
    assert "default-mimo" not in {
        item.profile.id for item in reloaded.route_candidates(provider="auto")
    }


def test_environment_profiles_cannot_be_overwritten(tmp_path):
    store = ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())
    overwrite = request()
    overwrite.id = "default-mimo"

    with pytest.raises(HTTPException) as error:
        store.upsert(overwrite)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ENVIRONMENT_PROFILE_ROUTE_READ_ONLY"


def test_corrupt_profile_store_fails_closed_instead_of_silently_erasing_profiles(tmp_path):
    gateway_settings = settings(tmp_path)
    gateway_settings.profile_store_path.write_text(
        '{"profiles":[{"id":"managed","unexpected":true}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="managed profile store is invalid"):
        ApiProfileStore(gateway_settings, MemoryProfileSecretStore())


class _UnavailableSecretStore:
    def get(self, _secret_ref):
        raise ProfileSecretStoreError("unavailable")

    def set(self, _secret_ref, _secret):
        raise ProfileSecretStoreError("unavailable")

    def delete(self, _secret_ref):
        raise ProfileSecretStoreError("unavailable")


def test_secret_write_fails_closed_when_secure_backend_is_unavailable(tmp_path):
    store = ApiProfileStore(settings(tmp_path), _UnavailableSecretStore())

    with pytest.raises(HTTPException) as error:
        store.upsert(request())

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "PROFILE_SECRET_STORE_UNAVAILABLE"
    assert not (tmp_path / "profiles.json").exists()


def test_existing_secret_reference_fails_startup_when_secure_backend_is_unavailable(
    tmp_path,
):
    (tmp_path / "profiles.json").write_text(
        json.dumps(
            {
                "schemaVersion": "2",
                "profiles": [
                    {
                        "id": "managed",
                        "provider": "mimo",
                        "base_url": "https://api.example.test/provider",
                        "secret_ref": "profile:" + ("a" * 32),
                        "default_model": "mimo-v2.5",
                        "auth_mode": "api-key",
                        "timeout_seconds": 120.0,
                        "enabled": True,
                        "source": "managed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="managed profile store is invalid"):
        ApiProfileStore(settings(tmp_path), _UnavailableSecretStore())


def test_delete_removes_managed_secret_from_secure_backend(tmp_path):
    secrets = MemoryProfileSecretStore()
    store = ApiProfileStore(settings(tmp_path), secrets)
    store.upsert(request())
    payload = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    secret_ref = payload["profiles"][0]["secret_ref"]

    assert store.delete("managed") is True

    assert secrets.get(secret_ref) is None
    assert json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))[
        "profiles"
    ] == []


def test_explicit_file_secret_backend_supports_headless_managed_profiles(tmp_path):
    gateway_settings = settings(tmp_path)
    gateway_settings.profile_secret_store_backend = "file"
    gateway_settings.profile_secret_store_path = tmp_path / "secrets" / "profiles.json"

    store = ApiProfileStore(gateway_settings)
    store.upsert(request())
    reloaded = ApiProfileStore(gateway_settings)

    assert reloaded.resolve("mimo", "managed").api_key == "managed-secret"
    assert "managed-secret" not in gateway_settings.profile_store_path.read_text(encoding="utf-8")
    assert "managed-secret" in gateway_settings.profile_secret_store_path.read_text(encoding="utf-8")


def test_invalid_boolean_environment_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("ACTIONDESIGN_LANGSMITH_ENABLED", "sometimes")

    with pytest.raises(ValueError, match="must be a boolean value"):
        Settings()


def test_settings_default_factories_are_validated(monkeypatch):
    monkeypatch.setenv("ACTIONDESIGN_AGENT_DEFAULT_PROVIDER", "invalid.provider")

    with pytest.raises(ValidationError):
        Settings()


def test_settings_accept_dynamic_default_provider(monkeypatch):
    monkeypatch.setenv("ACTIONDESIGN_AGENT_DEFAULT_PROVIDER", "team-openai")

    assert Settings().default_provider == "team-openai"


def test_full_conversation_log_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("ACTIONDESIGN_FULL_CONVERSATION_LOG", raising=False)

    assert Settings().full_conversation_log_enabled is True


def test_environment_profile_routing_metadata_override_persists(tmp_path):
    gateway_settings = settings(tmp_path)
    store = ApiProfileStore(gateway_settings, MemoryProfileSecretStore())
    current = store.resolve("mimo", "default-mimo")
    models = [
        {
            **item.model_dump(mode="json", by_alias=True),
            "priority": 800 if item.id == "mimo-v2.5-pro" else 700,
        }
        for item in current.models
    ]

    updated = store.upsert(
        ApiProfileWriteRequest(
            id=current.id,
            provider=current.provider,
            displayName=current.display_name,
            protocol=current.protocol,
            baseUrl=current.base_url,
            apiKey="",
            defaultModel="mimo-v2.5-pro",
            models=models,
            authMode=current.auth_mode,
            timeoutSeconds=current.timeout_seconds,
            priority=900,
            enabled=current.enabled,
        )
    )
    reloaded = ApiProfileStore(gateway_settings, MemoryProfileSecretStore())
    resolved = reloaded.resolve("mimo", "default-mimo")
    payload = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))

    assert updated["source"] == "environment"
    assert updated["priority"] == 900
    assert resolved.default_model == "mimo-v2.5-pro"
    assert resolved.priority == 900
    assert resolved.models[0].priority in {700, 800}
    assert payload["environmentOverrides"][0]["id"] == "default-mimo"


def test_schema_v3_profiles_are_migrated_with_default_priorities(tmp_path):
    secret_store = MemoryProfileSecretStore()
    secret_ref = "profile:" + ("b" * 32)
    secret_store.set(secret_ref, "managed-secret")
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "3",
                "profiles": [
                    {
                        "id": "legacy-v3",
                        "provider": "team-openai",
                        "display_name": "Legacy V3",
                        "protocol": "openai",
                        "base_url": "https://api.example.test/v1",
                        "secret_ref": secret_ref,
                        "default_model": "legacy-model",
                        "models": [{"id": "legacy-model", "name": "Legacy"}],
                        "auth_mode": "bearer",
                        "timeout_seconds": 60.0,
                        "enabled": True,
                        "source": "managed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ApiProfileStore(settings(tmp_path), secret_store)
    migrated = store.resolve("team-openai", "legacy-v3")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert migrated.priority == 100
    assert migrated.models[0].priority == 100
    assert payload["schemaVersion"] == "5"
    assert payload["profiles"][0]["priority"] == 100


def test_schema_v4_profile_store_is_migrated_to_environment_deletion_schema(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "4",
                "profiles": [],
                "environmentOverrides": [],
            }
        ),
        encoding="utf-8",
    )

    ApiProfileStore(settings(tmp_path), MemoryProfileSecretStore())
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schemaVersion"] == "5"
    assert payload["environmentOverrides"] == []


def test_schema_v2_profiles_are_migrated_to_protocol_and_model_schema(tmp_path):
    secrets = MemoryProfileSecretStore()
    secret_ref = "profile:" + ("a" * 32)
    secrets.set(secret_ref, "managed-secret")
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "2",
                "profiles": [
                    {
                        "id": "legacy",
                        "provider": "mimo",
                        "base_url": "https://api.example.test/provider",
                        "secret_ref": secret_ref,
                        "default_model": "mimo-v2.5",
                        "auth_mode": "api-key",
                        "timeout_seconds": 120.0,
                        "enabled": True,
                        "source": "managed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ApiProfileStore(settings(tmp_path), secrets)
    migrated = store.resolve("mimo", "legacy")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert migrated.protocol == "claude"
    assert migrated.models[0].id == "mimo-v2.5"
    assert payload["schemaVersion"] == "5"
    assert payload["profiles"][0]["protocol"] == "claude"
    assert payload["profiles"][0]["models"]


def test_settings_accept_claude_code_anthropic_auth_token_environment(monkeypatch):
    for name in (
        "ACTIONDESIGN_AGENT_DEFAULT_PROVIDER",
        "CLAUDE_CODE_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_MESSAGES_URL",
        "CLAUDE_CODE_DEFAULT_MODEL",
        "CLAUDE_CODE_AUTH_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "environment-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example.test/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "provider-model")

    gateway_settings = Settings()

    assert gateway_settings.default_provider == "claude-code"
    assert gateway_settings.claude_code_api_key == "environment-secret"
    assert gateway_settings.claude_code_messages_url == "https://provider.example.test/anthropic"
    assert gateway_settings.claude_code_default_model == "provider-model"
    assert gateway_settings.claude_code_auth_mode == "bearer"


def test_explicit_claude_code_settings_override_anthropic_compatibility(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "environment-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example.test/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "provider-model")
    monkeypatch.setenv("ACTIONDESIGN_AGENT_DEFAULT_PROVIDER", "mimo")
    monkeypatch.setenv("CLAUDE_CODE_API_KEY", "explicit-secret")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGES_URL", "https://explicit.example.test/messages")
    monkeypatch.setenv("CLAUDE_CODE_DEFAULT_MODEL", "explicit-model")
    monkeypatch.setenv("CLAUDE_CODE_AUTH_MODE", "api-key")

    gateway_settings = Settings()

    assert gateway_settings.default_provider == "mimo"
    assert gateway_settings.claude_code_api_key == "explicit-secret"
    assert gateway_settings.claude_code_messages_url == "https://explicit.example.test/messages"
    assert gateway_settings.claude_code_default_model == "explicit-model"
    assert gateway_settings.claude_code_auth_mode == "api-key"


def test_gateway_models_reject_type_coercion():
    with pytest.raises(ValidationError):
        ApiProfileWriteRequest.model_validate({
            "id": "managed",
            "provider": "mimo",
            "baseUrl": "https://api.example.test/provider",
            "apiKey": "managed-secret",
            "defaultModel": "mimo-v2.5",
            "timeoutSeconds": "12",
            "enabled": "false",
        })

    with pytest.raises(ValidationError):
        ToolResultRequest.model_validate({
            "conversationId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "runId": "run_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "requestId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "correlationId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "toolCallId": "call_1",
            "toolName": "create_node",
            "turn": "1",
        })
