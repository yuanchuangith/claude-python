from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.secret_store import (
    ManagedSecretStore,
    ManagedSecretStoreError,
    SecretRotateRequest,
    SecretUpsertRequest,
)


class DictSecretBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, secret_ref: str) -> str | None:
        return self.values.get(secret_ref)

    def set(self, secret_ref: str, secret: str) -> None:
        self.values[secret_ref] = secret

    def delete(self, secret_ref: str) -> None:
        self.values.pop(secret_ref, None)


def secret_request(value: str = "provider-secret-value") -> dict[str, str]:
    return {
        "id": "provider.team-primary",
        "kind": "provider_api_key",
        "displayName": "Team primary provider",
        "value": value,
    }


def test_upsert_persists_only_strict_metadata_and_never_returns_plaintext(tmp_path):
    backend = DictSecretBackend()
    path = tmp_path / "managed-secrets.json"
    store = ManagedSecretStore(path, backend)
    plaintext = "provider-secret-value"

    created = store.upsert(secret_request(plaintext))
    listed = store.list()
    fetched = store.get("provider.team-primary")

    assert created == fetched == listed[0]
    assert created["configured"] is True
    assert "masked" not in created
    assert "value" not in created
    assert plaintext not in json.dumps(created, ensure_ascii=False)
    persisted = path.read_text(encoding="utf-8")
    assert plaintext not in persisted
    assert "secretRef" in persisted
    assert "masked" in persisted
    assert "provider_api_key" in persisted
    assert len(backend.values) == 1

    reloaded = ManagedSecretStore(path, backend)
    assert reloaded.get("provider.team-primary") == created
    assert reloaded.resolve_value("provider.team-primary") == plaintext


@pytest.mark.parametrize(
    ("payload", "model"),
    [
        ({**secret_request(), "id": "../escape"}, SecretUpsertRequest),
        ({**secret_request(), "kind": "Provider Key"}, SecretUpsertRequest),
        ({**secret_request(), "displayName": " surrounding "}, SecretUpsertRequest),
        ({**secret_request(), "value": "short"}, SecretUpsertRequest),
        ({**secret_request(), "value": "secret-with-newline\n"}, SecretUpsertRequest),
        ({**secret_request(), "id": 123}, SecretUpsertRequest),
        ({"value": 12345678}, SecretRotateRequest),
        ({"value": " valid-secret"}, SecretRotateRequest),
    ],
)
def test_request_models_reject_invalid_or_coerced_secret_fields(payload, model):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_update_rotate_and_revoke_keep_plaintext_out_of_metadata(tmp_path):
    backend = DictSecretBackend()
    path = tmp_path / "managed-secrets.json"
    store = ManagedSecretStore(path, backend)
    old_value = "old-provider-secret"
    new_value = "new-provider-secret"
    store.upsert(secret_request(old_value))

    updated = store.upsert(
        {
            "id": "provider.team-primary",
            "kind": "provider_api_key",
            "displayName": "Renamed provider",
        }
    )
    assert updated["displayName"] == "Renamed provider"
    assert store.resolve_value("provider.team-primary") == old_value

    rotated = store.rotate("provider.team-primary", {"value": new_value})
    assert rotated["configured"] is True
    assert "masked" not in rotated
    assert rotated["rotatedAt"]
    assert "value" not in rotated
    assert store.resolve_value("provider.team-primary") == new_value

    revoked = store.revoke("provider.team-primary")
    assert revoked["configured"] is False
    assert "masked" not in revoked
    assert revoked["revokedAt"]
    assert backend.values == {}
    with pytest.raises(KeyError):
        store.resolve_value("provider.team-primary")
    assert store.revoke("provider.team-primary") == revoked

    persisted = path.read_text(encoding="utf-8")
    assert old_value not in persisted
    assert new_value not in persisted
    assert '"secretRef": null' in persisted


def test_metadata_save_failures_restore_secret_and_metadata_state(tmp_path, monkeypatch):
    backend = DictSecretBackend()
    path = tmp_path / "managed-secrets.json"
    store = ManagedSecretStore(path, backend)
    old_value = "old-provider-secret"
    store.upsert(secret_request(old_value))
    before = store.get("provider.team-primary")

    def fail_save() -> None:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(store, "_save", fail_save)
    with pytest.raises(OSError, match="simulated metadata failure"):
        store.rotate("provider.team-primary", "new-provider-secret")
    assert store.get("provider.team-primary") == before
    assert store.resolve_value("provider.team-primary") == old_value

    with pytest.raises(OSError, match="simulated metadata failure"):
        store.revoke("provider.team-primary")
    assert store.get("provider.team-primary") == before
    assert store.resolve_value("provider.team-primary") == old_value


def test_failed_create_removes_new_backend_value_and_corrupt_metadata_fails_closed(
    tmp_path,
    monkeypatch,
):
    backend = DictSecretBackend()
    path = tmp_path / "managed-secrets.json"
    store = ManagedSecretStore(path, backend)

    def fail_save() -> None:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(store, "_save", fail_save)
    with pytest.raises(OSError, match="simulated metadata failure"):
        store.upsert(secret_request())
    assert store.list() == []
    assert backend.values == {}

    path.write_text(
        json.dumps(
            {
                "schemaVersion": "1",
                "secrets": [
                    {
                        "id": "provider.team-primary",
                        "kind": "provider_api_key",
                        "displayName": "Team provider",
                        "secretRef": "profile:" + ("a" * 32),
                        "masked": "****alue",
                        "createdAt": "2026-07-29T00:00:00+00:00",
                        "updatedAt": "2026-07-29T00:00:00+00:00",
                        "rotatedAt": "",
                        "revokedAt": "",
                        "value": "must-never-be-accepted",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManagedSecretStoreError) as exc_info:
        ManagedSecretStore(path, backend)
    assert "must-never-be-accepted" not in str(exc_info.value)
