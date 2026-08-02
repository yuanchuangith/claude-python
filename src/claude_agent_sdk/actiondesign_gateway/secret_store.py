from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
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

from .profile_store import ProfileSecretStore, ProfileSecretStoreError

_SECRET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SECRET_REF_RE = re.compile(r"^profile:[0-9a-f]{32}$")
_SCHEMA_VERSION = "1"
_MIN_SECRET_LENGTH = 8
_MAX_SECRET_LENGTH = 8_192


class ManagedSecretStoreError(RuntimeError):
    """Raised when managed Secret metadata or its secure backend is unavailable."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )


def _validate_secret_id(value: str) -> str:
    if not _SECRET_ID_RE.fullmatch(value):
        raise ValueError("secret id is invalid")
    return value


def _validate_secret_kind(value: str) -> str:
    if not _SECRET_KIND_RE.fullmatch(value):
        raise ValueError("secret kind is invalid")
    return value


def _validate_display_name(value: str) -> str:
    if not value or len(value) > 256:
        raise ValueError("secret displayName is invalid")
    if value != value.strip():
        raise ValueError("secret displayName must not contain surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("secret displayName contains control characters")
    return value


def _validate_secret_value(value: str, *, allow_empty: bool) -> str:
    if not value and allow_empty:
        return value
    if len(value) < _MIN_SECRET_LENGTH or len(value) > _MAX_SECRET_LENGTH:
        raise ValueError("secret value length is invalid")
    if value != value.strip():
        raise ValueError("secret value must not contain leading or trailing whitespace")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("secret value contains invalid control characters")
    return value


def _validate_timestamp(value: str) -> str:
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("secret timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("secret timestamp must include a timezone")
    return value


class SecretUpsertRequest(_StrictModel):
    id: str
    kind: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    value: str = Field(default="", max_length=_MAX_SECRET_LENGTH)

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_secret_id(value)

    @field_validator("kind")
    @classmethod
    def _kind_is_safe(cls, value: str) -> str:
        return _validate_secret_kind(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_is_safe(cls, value: str) -> str:
        return _validate_display_name(value)

    @field_validator("value")
    @classmethod
    def _value_is_safe(cls, value: str) -> str:
        return _validate_secret_value(value, allow_empty=True)


class SecretRotateRequest(_StrictModel):
    value: str = Field(min_length=_MIN_SECRET_LENGTH, max_length=_MAX_SECRET_LENGTH)

    @field_validator("value")
    @classmethod
    def _value_is_safe(cls, value: str) -> str:
        return _validate_secret_value(value, allow_empty=False)


class SecretResponse(_StrictModel):
    id: str
    kind: str
    display_name: str = Field(alias="displayName")
    configured: bool
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    rotated_at: str = Field(default="", alias="rotatedAt")
    revoked_at: str = Field(default="", alias="revokedAt")

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_secret_id(value)

    @field_validator("kind")
    @classmethod
    def _kind_is_safe(cls, value: str) -> str:
        return _validate_secret_kind(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_is_safe(cls, value: str) -> str:
        return _validate_display_name(value)

    @field_validator("created_at", "updated_at", "rotated_at", "revoked_at")
    @classmethod
    def _timestamp_is_valid(cls, value: str) -> str:
        return _validate_timestamp(value)

class _SecretMetadata(_StrictModel):
    id: str
    kind: str
    display_name: str = Field(alias="displayName", min_length=1, max_length=256)
    secret_ref: str | None = Field(default=None, alias="secretRef", max_length=64)
    masked: str = Field(default="", max_length=64)
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    rotated_at: str = Field(default="", alias="rotatedAt")
    revoked_at: str = Field(default="", alias="revokedAt")

    @field_validator("id")
    @classmethod
    def _id_is_safe(cls, value: str) -> str:
        return _validate_secret_id(value)

    @field_validator("kind")
    @classmethod
    def _kind_is_safe(cls, value: str) -> str:
        return _validate_secret_kind(value)

    @field_validator("display_name")
    @classmethod
    def _display_name_is_safe(cls, value: str) -> str:
        return _validate_display_name(value)

    @field_validator("secret_ref")
    @classmethod
    def _secret_ref_is_safe(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_REF_RE.fullmatch(value):
            raise ValueError("secret reference is invalid")
        return value

    @field_validator("created_at", "updated_at", "rotated_at", "revoked_at")
    @classmethod
    def _timestamp_is_valid(cls, value: str) -> str:
        return _validate_timestamp(value)

    @model_validator(mode="after")
    def _state_is_consistent(self) -> _SecretMetadata:
        configured = self.secret_ref is not None
        if configured:
            if not self.masked:
                raise ValueError("configured Secret metadata requires a masked value")
            if self.revoked_at:
                raise ValueError("configured Secret metadata must not have revokedAt")
        else:
            if self.masked:
                raise ValueError("revoked Secret metadata must not have a masked value")
            if not self.revoked_at:
                raise ValueError("revoked Secret metadata requires revokedAt")
        return self

    def public(self) -> dict[str, Any]:
        return SecretResponse.model_validate(
            {
                "id": self.id,
                "kind": self.kind,
                "displayName": self.display_name,
                "configured": self.secret_ref is not None,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
                "rotatedAt": self.rotated_at,
                "revokedAt": self.revoked_at,
            }
        ).model_dump(mode="json", by_alias=True)


class _SecretMetadataFile(_StrictModel):
    schema_version: Literal["1"] = Field(alias="schemaVersion")
    secrets: list[_SecretMetadata] = Field(default_factory=list, max_length=1_000)

    @model_validator(mode="after")
    def _identities_are_unique(self) -> _SecretMetadataFile:
        ids = [item.id for item in self.secrets]
        if len(ids) != len(set(ids)):
            raise ValueError("managed Secret ids must be unique")
        refs = [item.secret_ref for item in self.secrets if item.secret_ref]
        if len(refs) != len(set(refs)):
            raise ValueError("managed Secret references must be unique")
        return self


@dataclass(frozen=True, repr=False)
class ManagedSecretRollbackSnapshot:
    """Opaque pre-mutation Secret state used only for failure rollback."""

    secret_id: str
    metadata: _SecretMetadata | None
    value: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(secret_id={self.secret_id!r}, "
            "metadata=<redacted>, value=<redacted>)"
        )


class ManagedSecretStore:
    """Metadata-only registry backed by an existing secure Secret backend."""

    def __init__(self, path: Path, secret_store: ProfileSecretStore) -> None:
        self.path = Path(path)
        self._secret_store = secret_store
        self._lock = threading.RLock()
        self._secrets = self._load()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._secrets[item].public() for item in sorted(self._secrets)]

    def list_public(self) -> list[dict[str, Any]]:
        return self.list()

    def get(self, secret_id: str) -> dict[str, Any] | None:
        safe_id = _validate_secret_id(secret_id)
        with self._lock:
            item = self._secrets.get(safe_id)
            return item.public() if item is not None else None

    def resolve_value(self, secret_id: str) -> str:
        safe_id = _validate_secret_id(secret_id)
        with self._lock:
            item = self._secrets.get(safe_id)
            if item is None or item.secret_ref is None:
                raise KeyError(safe_id)
            value = self._backend_get(item.secret_ref)
            if not value:
                raise ManagedSecretStoreError("configured Secret value is unavailable")
            return value

    def candidate_overlay(
        self,
        values: Mapping[str, str],
    ) -> ManagedSecretCandidateOverlay:
        """Create a memory-only Secret view for connection testing.

        Candidate values are validated but never written to the metadata file or
        secure backend.  Dropping the overlay drops the only additional references.
        """

        return ManagedSecretCandidateOverlay(self, values)

    def snapshot_for_rollback(
        self,
        secret_id: str,
    ) -> ManagedSecretRollbackSnapshot:
        """Capture the current Secret state without exposing it publicly."""

        safe_id = _validate_secret_id(secret_id)
        with self._lock:
            current = self._secrets.get(safe_id)
            value = ""
            if current is not None and current.secret_ref is not None:
                value = self._backend_get(current.secret_ref) or ""
                if not value:
                    raise ManagedSecretStoreError(
                        "configured Secret value is unavailable"
                    )
            return ManagedSecretRollbackSnapshot(
                secret_id=safe_id,
                metadata=(
                    current.model_copy(deep=True) if current is not None else None
                ),
                value=value,
            )

    def restore_snapshot(
        self,
        snapshot: ManagedSecretRollbackSnapshot,
    ) -> dict[str, Any] | None:
        """Restore metadata and secure storage after a later publish failure."""

        safe_id = _validate_secret_id(snapshot.secret_id)
        with self._lock:
            current = self._secrets.get(safe_id)
            current_ref = current.secret_ref if current is not None else None
            previous = snapshot.metadata
            previous_ref = previous.secret_ref if previous is not None else None
            if previous_ref is not None:
                if not snapshot.value:
                    raise ManagedSecretStoreError(
                        "Secret rollback snapshot is incomplete"
                    )
                self._backend_set(previous_ref, snapshot.value)
            if previous is None:
                self._secrets.pop(safe_id, None)
            else:
                self._secrets[safe_id] = previous.model_copy(deep=True)
            try:
                self._save()
            except Exception:
                if current is None:
                    self._secrets.pop(safe_id, None)
                else:
                    self._secrets[safe_id] = current
                if previous_ref is not None and previous_ref != current_ref:
                    self._restore_backend(previous_ref, "")
                raise
            if current_ref is not None and current_ref != previous_ref:
                self._backend_delete(current_ref)
            return previous.public() if previous is not None else None

    def upsert(self, request: SecretUpsertRequest | dict[str, Any]) -> dict[str, Any]:
        payload = SecretUpsertRequest.model_validate(request)
        with self._lock:
            current = self._secrets.get(payload.id)
            if current is None and not payload.value:
                raise ValueError("secret value is required when creating a Secret")

            previous = current.model_copy(deep=True) if current is not None else None
            previous_value = ""
            secret_ref = current.secret_ref if current is not None else None
            if payload.value:
                if secret_ref is not None:
                    previous_value = self._backend_get(secret_ref) or ""
                secret_ref = secret_ref or self._new_secret_ref()
                self._backend_set(secret_ref, payload.value)

            now = self._now()
            configured = secret_ref is not None
            item = _SecretMetadata.model_validate(
                {
                    "id": payload.id,
                    "kind": payload.kind,
                    "displayName": payload.display_name,
                    "secretRef": secret_ref,
                    "masked": (
                        self._mask(payload.value)
                        if payload.value
                        else (current.masked if current is not None else "")
                    ),
                    "createdAt": current.created_at if current is not None else now,
                    "updatedAt": now,
                    "rotatedAt": (
                        now
                        if payload.value and current is not None and current.secret_ref is not None
                        else (current.rotated_at if current is not None else "")
                    ),
                    "revokedAt": "" if configured else (current.revoked_at if current else now),
                }
            )
            self._secrets[payload.id] = item
            try:
                self._save()
            except Exception as exc:
                self._restore_metadata(payload.id, previous)
                if payload.value and secret_ref is not None:
                    self._restore_backend(secret_ref, previous_value)
                raise exc
            return item.public()

    def rotate(
        self,
        secret_id: str,
        request: SecretRotateRequest | dict[str, Any] | str,
    ) -> dict[str, Any]:
        safe_id = _validate_secret_id(secret_id)
        payload = SecretRotateRequest.model_validate(
            {"value": request} if isinstance(request, str) else request
        )
        with self._lock:
            current = self._secrets.get(safe_id)
            if current is None:
                raise KeyError(safe_id)
            if current.secret_ref is None:
                raise ValueError("revoked Secret must be configured with upsert before rotation")
            previous = current.model_copy(deep=True)
            previous_value = self._backend_get(current.secret_ref)
            if not previous_value:
                raise ManagedSecretStoreError("configured Secret value is unavailable")
            new_ref = self._new_secret_ref()
            self._backend_set(new_ref, payload.value)
            now = self._now()
            item = current.model_copy(
                update={
                    "secret_ref": new_ref,
                    "masked": self._mask(payload.value),
                    "updated_at": now,
                    "rotated_at": now,
                }
            )
            item = _SecretMetadata.model_validate(item.model_dump(mode="python"))
            self._secrets[safe_id] = item
            try:
                self._save()
            except Exception as exc:
                self._secrets[safe_id] = previous
                self._restore_backend(new_ref, "")
                raise exc
            try:
                self._backend_delete(current.secret_ref)
            except Exception as exc:
                self._secrets[safe_id] = previous
                try:
                    self._save()
                    self._restore_backend(new_ref, "")
                except Exception as rollback_exc:
                    raise ManagedSecretStoreError(
                        "Secret rotation failed and metadata rollback also failed"
                    ) from rollback_exc
                raise exc
            return item.public()

    def revoke(self, secret_id: str) -> dict[str, Any]:
        safe_id = _validate_secret_id(secret_id)
        with self._lock:
            current = self._secrets.get(safe_id)
            if current is None:
                raise KeyError(safe_id)
            if current.secret_ref is None:
                return current.public()
            previous = current.model_copy(deep=True)
            previous_value = self._backend_get(current.secret_ref)
            if not previous_value:
                raise ManagedSecretStoreError("configured Secret value is unavailable")
            self._backend_delete(current.secret_ref)
            now = self._now()
            item = _SecretMetadata.model_validate(
                {
                    **current.model_dump(mode="python", by_alias=True),
                    "secretRef": None,
                    "masked": "",
                    "updatedAt": now,
                    "revokedAt": now,
                }
            )
            self._secrets[safe_id] = item
            try:
                self._save()
            except Exception as exc:
                self._secrets[safe_id] = previous
                self._restore_backend(current.secret_ref, previous_value)
                raise exc
            return item.public()

    def _load(self) -> dict[str, _SecretMetadata]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            payload = _SecretMetadataFile.model_validate(raw)
            secrets = {item.id: item for item in payload.secrets}
            for item in secrets.values():
                if item.secret_ref and not self._backend_get(item.secret_ref):
                    raise ValueError("configured Secret value is unavailable")
            return secrets
        except (
            OSError,
            TypeError,
            ValueError,
            ValidationError,
            ProfileSecretStoreError,
        ) as exc:
            raise ManagedSecretStoreError(
                "managed Secret metadata is invalid and was not loaded"
            ) from exc

    def _save(self) -> None:
        payload = _SecretMetadataFile.model_validate(
            {
                "schemaVersion": _SCHEMA_VERSION,
                "secrets": [self._secrets[item] for item in sorted(self._secrets)],
            }
        ).model_dump(mode="json", by_alias=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            temp_path.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            raise ManagedSecretStoreError("managed Secret metadata is unavailable") from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _backend_get(self, secret_ref: str) -> str | None:
        try:
            return self._secret_store.get(secret_ref)
        except ProfileSecretStoreError as exc:
            raise ManagedSecretStoreError("secure Secret storage is unavailable") from exc

    def _backend_set(self, secret_ref: str, value: str) -> None:
        try:
            self._secret_store.set(secret_ref, value)
        except ProfileSecretStoreError as exc:
            raise ManagedSecretStoreError("secure Secret storage is unavailable") from exc

    def _backend_delete(self, secret_ref: str) -> None:
        try:
            self._secret_store.delete(secret_ref)
        except ProfileSecretStoreError as exc:
            raise ManagedSecretStoreError("secure Secret storage is unavailable") from exc

    def _restore_backend(self, secret_ref: str, previous_value: str) -> None:
        try:
            if previous_value:
                self._secret_store.set(secret_ref, previous_value)
            else:
                self._secret_store.delete(secret_ref)
        except ProfileSecretStoreError as exc:
            raise ManagedSecretStoreError(
                "Secret metadata update failed and secure Secret rollback also failed"
            ) from exc

    def _restore_metadata(
        self,
        secret_id: str,
        previous: _SecretMetadata | None,
    ) -> None:
        if previous is None:
            self._secrets.pop(secret_id, None)
        else:
            self._secrets[secret_id] = previous

    @staticmethod
    def _new_secret_ref() -> str:
        # FileProfileSecretStore currently accepts this namespace.  The value is
        # random and never exposed through the public Secret API.
        return f"profile:{uuid.uuid4().hex}"

    @staticmethod
    def _mask(value: str) -> str:
        return f"****{value[-4:]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


class ManagedSecretCandidateOverlay:
    """Read-through Secret resolver with redacted, memory-only candidate values."""

    def __init__(
        self,
        base_store: ManagedSecretStore,
        values: Mapping[str, str],
    ) -> None:
        self._base_store = base_store
        self._values = {
            _validate_secret_id(secret_id): SecretRotateRequest.model_validate(
                {"value": value}
            ).value
            for secret_id, value in values.items()
        }

    @property
    def secret_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def get(self, secret_id: str) -> dict[str, Any] | None:
        safe_id = _validate_secret_id(secret_id)
        if safe_id in self._values:
            return {"id": safe_id, "configured": True}
        return self._base_store.get(safe_id)

    def resolve_value(self, secret_id: str) -> str:
        safe_id = _validate_secret_id(secret_id)
        if safe_id in self._values:
            return self._values[safe_id]
        return self._base_store.resolve_value(safe_id)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(secret_ids={self.secret_ids!r}, "
            "values=<redacted>)"
        )


__all__ = [
    "ManagedSecretCandidateOverlay",
    "ManagedSecretRollbackSnapshot",
    "ManagedSecretStore",
    "ManagedSecretStoreError",
    "SecretResponse",
    "SecretRotateRequest",
    "SecretUpsertRequest",
]
