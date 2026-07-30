from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_SCHEMA_VERSION = "1"
_KEY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KEY_RE = re.compile(r"^adk_([0-9a-f]{32})_([A-Za-z0-9_-]{43})$")
_KEY_PREFIX_RE = re.compile(r"^adk_[0-9a-f]{8}$")
_HEX_SALT_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_KEY_COUNT = 10_000
_MAX_NAME_LENGTH = 256
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class ClientApiKeyStoreError(RuntimeError):
    """Raised when Client API Key metadata cannot be loaded or persisted."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


def _validate_name(value: str) -> str:
    if not value or len(value) > _MAX_NAME_LENGTH:
        raise ValueError("Client API Key name is invalid")
    if value != value.strip():
        raise ValueError("Client API Key name must not contain surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Client API Key name contains control characters")
    return value


def _validate_key_id(value: str) -> str:
    if not _KEY_ID_RE.fullmatch(value):
        raise ValueError("Client API Key id is invalid")
    return value


def _validate_timestamp(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Client API Key timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("Client API Key timestamp must include a timezone")
    return value


def _prefix_for_id(key_id: str) -> str:
    return f"adk_{key_id[:8]}"


class ClientApiKeyCreateRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, value: str) -> str:
        return _validate_name(value)


class ClientApiKeySummary(_StrictModel):
    id: str
    name: str
    prefix: str
    created_at: str = Field(alias="createdAt")
    last_used_at: str | None = Field(default=None, alias="lastUsedAt")

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_key_id(value)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("prefix")
    @classmethod
    def _prefix_is_safe(cls, value: str) -> str:
        if not _KEY_PREFIX_RE.fullmatch(value):
            raise ValueError("Client API Key prefix is invalid")
        return value

    @field_validator("created_at", "last_used_at")
    @classmethod
    def _timestamp_is_valid(cls, value: str | None) -> str | None:
        return _validate_timestamp(value)

    @model_validator(mode="after")
    def _prefix_matches_id(self) -> ClientApiKeySummary:
        if self.prefix != _prefix_for_id(self.id):
            raise ValueError("Client API Key prefix does not match id")
        return self


class ClientApiKeyCreateResponse(ClientApiKeySummary):
    key: str = Field(repr=False)

    @field_validator("key")
    @classmethod
    def _key_is_valid(cls, value: str) -> str:
        if not _KEY_RE.fullmatch(value):
            raise ValueError("generated Client API Key is invalid")
        return value

    @model_validator(mode="after")
    def _key_matches_id(self) -> ClientApiKeyCreateResponse:
        match = _KEY_RE.fullmatch(self.key)
        if match is None or match.group(1) != self.id:
            raise ValueError("generated Client API Key does not match id")
        return self


class ClientApiKeyDeleteResponse(_StrictModel):
    id: str
    deleted: Literal[True] = True

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_key_id(value)


class _StoredClientApiKey(_StrictModel):
    id: str
    name: str
    prefix: str
    hash_algorithm: Literal["scrypt-v1"] = Field(alias="hashAlgorithm")
    salt: str
    key_hash: str = Field(alias="hash")
    created_at: str = Field(alias="createdAt")
    last_used_at: str | None = Field(default=None, alias="lastUsedAt")

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_key_id(value)

    @field_validator("name")
    @classmethod
    def _name_is_safe(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("prefix")
    @classmethod
    def _prefix_is_safe(cls, value: str) -> str:
        if not _KEY_PREFIX_RE.fullmatch(value):
            raise ValueError("Client API Key prefix is invalid")
        return value

    @field_validator("salt")
    @classmethod
    def _salt_is_valid(cls, value: str) -> str:
        if not _HEX_SALT_RE.fullmatch(value):
            raise ValueError("Client API Key salt is invalid")
        return value

    @field_validator("key_hash")
    @classmethod
    def _hash_is_valid(cls, value: str) -> str:
        if not _HEX_DIGEST_RE.fullmatch(value):
            raise ValueError("Client API Key hash is invalid")
        return value

    @field_validator("created_at", "last_used_at")
    @classmethod
    def _timestamp_is_valid(cls, value: str | None) -> str | None:
        return _validate_timestamp(value)

    @model_validator(mode="after")
    def _prefix_matches_id(self) -> _StoredClientApiKey:
        if self.prefix != _prefix_for_id(self.id):
            raise ValueError("Client API Key prefix does not match id")
        return self

    def public(self) -> dict[str, Any]:
        return ClientApiKeySummary.model_validate(
            {
                "id": self.id,
                "name": self.name,
                "prefix": self.prefix,
                "createdAt": self.created_at,
                "lastUsedAt": self.last_used_at,
            }
        ).model_dump(mode="json", by_alias=True)


class _ClientApiKeyFile(_StrictModel):
    schema_version: Literal["1"] = Field(alias="schemaVersion")
    keys: list[_StoredClientApiKey] = Field(
        default_factory=list,
        max_length=_MAX_KEY_COUNT,
    )

    @model_validator(mode="after")
    def _identities_are_unique(self) -> _ClientApiKeyFile:
        ids = [item.id for item in self.keys]
        if len(ids) != len(set(ids)):
            raise ValueError("Client API Key ids must be unique")
        prefixes = [item.prefix for item in self.keys]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("Client API Key prefixes must be unique")
        return self


class ClientApiKeyStore:
    """Thread-safe API Key hashes persisted through atomic file replacement."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._keys = self._load()

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._keys[key_id].public() for key_id in sorted(self._keys)]

    def list(self) -> list[dict[str, Any]]:
        return self.list_public()

    def get_public(self, key_id: str) -> dict[str, Any] | None:
        safe_id = _validate_key_id(key_id)
        with self._lock:
            item = self._keys.get(safe_id)
            return item.public() if item is not None else None

    def create(
        self,
        request: ClientApiKeyCreateRequest | dict[str, Any],
    ) -> dict[str, Any]:
        payload = ClientApiKeyCreateRequest.model_validate(request)
        with self._lock:
            if len(self._keys) >= _MAX_KEY_COUNT:
                raise ClientApiKeyStoreError("Client API Key limit has been reached")
            key_id = self._new_key_id()
            full_key = f"adk_{key_id}_{secrets.token_urlsafe(32)}"
            salt = secrets.token_bytes(16)
            now = self._now()
            item = _StoredClientApiKey.model_validate(
                {
                    "id": key_id,
                    "name": payload.name,
                    "prefix": _prefix_for_id(key_id),
                    "hashAlgorithm": "scrypt-v1",
                    "salt": salt.hex(),
                    "hash": self._derive_hash(full_key, salt).hex(),
                    "createdAt": now,
                    "lastUsedAt": None,
                }
            )
            candidate = dict(self._keys)
            candidate[key_id] = item
            self._save(candidate)
            self._keys = candidate
            return ClientApiKeyCreateResponse.model_validate(
                {**item.public(), "key": full_key}
            ).model_dump(mode="json", by_alias=True)

    def verify(self, full_key: str) -> dict[str, Any] | None:
        match = _KEY_RE.fullmatch(full_key)
        if match is None:
            return None
        key_id = match.group(1)
        with self._lock:
            item = self._keys.get(key_id)
            if item is None:
                return None
            actual_hash = self._derive_hash(full_key, bytes.fromhex(item.salt))
            if not hmac.compare_digest(actual_hash, bytes.fromhex(item.key_hash)):
                return None
            updated = _StoredClientApiKey.model_validate(
                {
                    **item.model_dump(mode="python", by_alias=True),
                    "lastUsedAt": self._now(),
                }
            )
            candidate = dict(self._keys)
            candidate[key_id] = updated
            self._save(candidate)
            self._keys = candidate
            return updated.public()

    def delete(self, key_id: str) -> dict[str, Any]:
        safe_id = _validate_key_id(key_id)
        with self._lock:
            if safe_id not in self._keys:
                raise KeyError(safe_id)
            candidate = dict(self._keys)
            del candidate[safe_id]
            self._save(candidate)
            self._keys = candidate
            return ClientApiKeyDeleteResponse(id=safe_id).model_dump(
                mode="json",
                by_alias=True,
            )

    def _new_key_id(self) -> str:
        existing_prefixes = {item.prefix for item in self._keys.values()}
        while True:
            candidate = secrets.token_hex(16)
            if (
                candidate not in self._keys
                and _prefix_for_id(candidate) not in existing_prefixes
            ):
                return candidate

    def _load(self) -> dict[str, _StoredClientApiKey]:
        if not self.path.exists():
            return {}
        try:
            payload = _ClientApiKeyFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            return {item.id: item for item in payload.keys}
        except (OSError, ValueError, ValidationError) as exc:
            raise ClientApiKeyStoreError(
                "Client API Key metadata is invalid and was not loaded"
            ) from exc

    def _save(self, keys: dict[str, _StoredClientApiKey]) -> None:
        payload = _ClientApiKeyFile.model_validate(
            {
                "schemaVersion": _SCHEMA_VERSION,
                "keys": [keys[key_id] for key_id in sorted(keys)],
            }
        ).model_dump(mode="json", by_alias=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            with suppress(OSError):
                temp_path.chmod(0o600)
            temp_path.replace(self.path)
            with suppress(OSError):
                self.path.chmod(0o600)
        except OSError as exc:
            raise ClientApiKeyStoreError(
                "Client API Key metadata is unavailable"
            ) from exc
        finally:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _derive_hash(full_key: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            full_key.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
            maxmem=_SCRYPT_MAXMEM,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
