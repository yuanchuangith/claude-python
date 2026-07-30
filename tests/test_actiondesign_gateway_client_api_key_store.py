import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from claude_agent_sdk.actiondesign_gateway.client_api_key_store import (
    ClientApiKeyCreateRequest,
    ClientApiKeyStore,
    ClientApiKeyStoreError,
)

PUBLIC_FIELDS = {"id", "name", "prefix", "createdAt", "lastUsedAt"}


def _change_last_character(value: str) -> str:
    replacement = "A" if value[-1] != "A" else "B"
    return f"{value[:-1]}{replacement}"


def test_create_returns_random_full_key_once_and_persists_only_salted_hash(
    tmp_path,
):
    path = tmp_path / "client-api-keys.json"
    store = ClientApiKeyStore(path)

    first = store.create({"name": "Browser client"})
    second = store.create(ClientApiKeyCreateRequest(name="External client"))

    assert set(first) == PUBLIC_FIELDS | {"key"}
    assert first["key"].startswith(f"adk_{first['id']}_")
    assert first["prefix"] == first["key"][:12]
    assert first["lastUsedAt"] is None
    assert second["key"] != first["key"]

    listed = store.list_public()
    assert len(listed) == 2
    assert all(set(item) == PUBLIC_FIELDS for item in listed)
    assert first["key"] not in path.read_text(encoding="utf-8")
    assert second["key"] not in path.read_text(encoding="utf-8")

    persisted = json.loads(path.read_text(encoding="utf-8"))
    records = {item["id"]: item for item in persisted["keys"]}
    assert records[first["id"]]["hashAlgorithm"] == "scrypt-v1"
    assert len(records[first["id"]]["salt"]) == 32
    assert len(records[first["id"]]["hash"]) == 64
    assert records[first["id"]]["salt"] != records[second["id"]]["salt"]


def test_verify_requires_full_exact_key_and_updates_last_used_at(tmp_path):
    path = tmp_path / "client-api-keys.json"
    store = ClientApiKeyStore(path)
    created = store.create({"name": "Exact match"})

    assert store.verify(created["prefix"]) is None
    assert store.verify(_change_last_character(created["key"])) is None
    assert store.get_public(created["id"])["lastUsedAt"] is None

    verified = store.verify(created["key"])

    assert verified is not None
    assert set(verified) == PUBLIC_FIELDS
    assert verified["id"] == created["id"]
    assert verified["lastUsedAt"] is not None
    assert (
        ClientApiKeyStore(path).get_public(created["id"])["lastUsedAt"]
        == (verified["lastUsedAt"])
    )


def test_delete_invalidates_new_verification_without_mutating_authenticated_result(
    tmp_path,
):
    path = tmp_path / "client-api-keys.json"
    store = ClientApiKeyStore(path)
    created = store.create({"name": "Disposable"})
    authenticated = store.verify(created["key"])

    assert authenticated is not None
    assert store.delete(created["id"]) == {"id": created["id"], "deleted": True}
    assert authenticated["id"] == created["id"]
    assert store.verify(created["key"]) is None
    assert ClientApiKeyStore(path).verify(created["key"]) is None


def test_concurrent_creates_are_thread_safe_and_leave_valid_atomic_file(tmp_path):
    path = tmp_path / "client-api-keys.json"
    store = ClientApiKeyStore(path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(
            pool.map(
                lambda index: store.create({"name": f"Concurrent {index}"}),
                range(12),
            )
        )

    assert len({item["id"] for item in created}) == 12
    assert len({item["key"] for item in created}) == 12
    assert len(ClientApiKeyStore(path).list_public()) == 12
    assert json.loads(path.read_text(encoding="utf-8"))["schemaVersion"] == "1"
    assert not list(tmp_path.glob(".client-api-keys.json.*.tmp"))


def test_key_value_is_not_written_to_normal_logs(tmp_path, caplog):
    store = ClientApiKeyStore(tmp_path / "client-api-keys.json")

    with caplog.at_level("DEBUG"):
        created = store.create({"name": "No logging"})
        assert store.verify(_change_last_character(created["key"])) is None
        assert store.verify(created["key"]) is not None

    assert created["key"] not in caplog.text


def test_corrupt_or_duplicate_persisted_metadata_fails_closed(tmp_path):
    path = tmp_path / "client-api-keys.json"
    store = ClientApiKeyStore(path)
    created = store.create({"name": "Original"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["keys"].append(dict(payload["keys"][0]))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ClientApiKeyStoreError, match="invalid and was not loaded"):
        ClientApiKeyStore(path)

    assert created["key"] not in path.read_text(encoding="utf-8")


def test_create_request_is_strict_and_rejects_unsafe_names():
    with pytest.raises(ValidationError):
        ClientApiKeyCreateRequest.model_validate({"name": " client "})
    with pytest.raises(ValidationError):
        ClientApiKeyCreateRequest.model_validate({"name": "client", "scope": "all"})
