from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .models import ApiProfileModel, ApiProfileWriteRequest, MIMO_MODELS

_PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_REF = re.compile(r"^profile:[0-9a-f]{32}$")
_PROFILE_STORE_SCHEMA_VERSION = "3"
_KEYRING_SERVICE_NAME = "gxp.actiondesign.gateway.provider-profile.v1"
_SECURE_KEYRING_BACKEND_PREFIXES = (
    "keyring.backends.windows.",
    "keyring.backends.macos.",
    "keyring.backends.secretservice.",
    "keyring.backends.kwallet.",
    "keyring.backends.libsecret.",
)


class ProfileSecretStoreError(RuntimeError):
    """Raised when the secure provider-profile secret backend cannot be used."""


class ProfileSecretStore(Protocol):
    def get(self, secret_ref: str) -> str | None: ...

    def set(self, secret_ref: str, secret: str) -> None: ...

    def delete(self, secret_ref: str) -> None: ...


class KeyringProfileSecretStore:
    """Stores API keys in the operating-system credential backend via keyring."""

    def __init__(self) -> None:
        self._backend: Any | None = None
        try:
            import keyring
        except ImportError:
            return
        try:
            candidate = keyring.get_keyring()
            priority = float(getattr(candidate, "priority", 0))
            backend_name = (
                f"{type(candidate).__module__}.{type(candidate).__name__}".lower()
            )
        except Exception:
            return
        if priority > 0 and backend_name.startswith(
            _SECURE_KEYRING_BACKEND_PREFIXES
        ):
            self._backend = candidate

    def get(self, secret_ref: str) -> str | None:
        backend = self._require_backend()
        try:
            value = backend.get_password(_KEYRING_SERVICE_NAME, secret_ref)
        except Exception as exc:
            raise ProfileSecretStoreError(
                "secure profile secret storage is unavailable"
            ) from exc
        if value is not None and not isinstance(value, str):
            raise ProfileSecretStoreError(
                "secure profile secret storage returned an invalid value"
            )
        return value

    def set(self, secret_ref: str, secret: str) -> None:
        backend = self._require_backend()
        try:
            backend.set_password(_KEYRING_SERVICE_NAME, secret_ref, secret)
        except Exception as exc:
            raise ProfileSecretStoreError(
                "secure profile secret storage is unavailable"
            ) from exc

    def delete(self, secret_ref: str) -> None:
        backend = self._require_backend()
        if self.get(secret_ref) is None:
            return
        try:
            backend.delete_password(_KEYRING_SERVICE_NAME, secret_ref)
        except Exception as exc:
            raise ProfileSecretStoreError(
                "secure profile secret storage is unavailable"
            ) from exc

    def _require_backend(self) -> Any:
        if self._backend is None:
            raise ProfileSecretStoreError(
                "secure profile secret storage is unavailable"
            )
        return self._backend


class MemoryProfileSecretStore:
    """Explicit non-persistent secret store for tests or ephemeral deployments."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def get(self, secret_ref: str) -> str | None:
        return self._secrets.get(secret_ref)

    def set(self, secret_ref: str, secret: str) -> None:
        self._secrets[secret_ref] = secret

    def delete(self, secret_ref: str) -> None:
        self._secrets.pop(secret_ref, None)


class FileProfileSecretStore:
    """Permission-protected secret file for headless systemd deployments."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self, secret_ref: str) -> str | None:
        with self._lock:
            return self._load().get(secret_ref)

    def set(self, secret_ref: str, secret: str) -> None:
        if not _SECRET_REF.fullmatch(secret_ref) or not secret:
            raise ProfileSecretStoreError("invalid profile secret")
        with self._lock:
            values = self._load()
            values[secret_ref] = secret
            self._save(values)

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            values = self._load()
            if secret_ref in values:
                del values[secret_ref]
                self._save(values)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileSecretStoreError(
                "profile secret file is unavailable or invalid"
            ) from exc
        if not isinstance(payload, dict) or any(
            not isinstance(key, str)
            or not _SECRET_REF.fullmatch(key)
            or not isinstance(value, str)
            or not value
            for key, value in payload.items()
        ):
            raise ProfileSecretStoreError("profile secret file is invalid")
        return payload

    def _save(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(values, ensure_ascii=False, indent=2),
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
            raise ProfileSecretStoreError(
                "profile secret file is unavailable"
            ) from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


class _PersistedProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    protocol: Literal["openai", "claude"]
    base_url: str = Field(min_length=1, max_length=2_048)
    secret_ref: str | None = Field(default=None, max_length=64)
    default_model: str = Field(min_length=1, max_length=256)
    models: list[ApiProfileModel] = Field(min_length=1, max_length=256)
    auth_mode: Literal["api-key", "bearer"]
    timeout_seconds: float = Field(ge=1.0, le=600.0)
    enabled: bool
    source: Literal["managed"]

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("invalid persisted profile id")
        return value

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("invalid persisted provider id")
        return value

    @model_validator(mode="after")
    def _validate_models(self) -> "_PersistedProviderProfile":
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("persisted profile model ids must be unique")
        if self.default_model not in ids:
            raise ValueError("persisted default model must be present in models")
        return self

    @field_validator("secret_ref")
    @classmethod
    def _validate_secret_ref(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_REF.fullmatch(value):
            raise ValueError("invalid persisted profile secret reference")
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("persisted profile base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("persisted profile base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(
                "persisted profile base_url must not contain query parameters or fragments"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("persisted non-loopback profiles must use https")
        return value.rstrip("/")


class _PersistedProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["3"] = Field(alias="schemaVersion")
    profiles: list[_PersistedProviderProfile] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "_PersistedProfileFile":
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("persisted profile ids must be unique")
        refs = [profile.secret_ref for profile in self.profiles if profile.secret_ref]
        if len(refs) != len(set(refs)):
            raise ValueError("persisted profile secret references must be unique")
        return self


class _PersistedProviderProfileV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    provider: Literal["mimo", "claude-code"]
    base_url: str = Field(min_length=1, max_length=2_048)
    secret_ref: str | None = Field(default=None, max_length=64)
    default_model: str = Field(min_length=1, max_length=256)
    auth_mode: Literal["api-key", "bearer"]
    timeout_seconds: float = Field(ge=1.0, le=600.0)
    enabled: bool
    source: Literal["managed"]


class _PersistedProfileFileV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["2"] = Field(alias="schemaVersion")
    profiles: list[_PersistedProviderProfileV2] = Field(default_factory=list, max_length=100)


@dataclass
class ProviderProfile:
    id: str
    provider: str
    base_url: str
    api_key: str
    default_model: str
    auth_mode: str
    timeout_seconds: float
    enabled: bool
    source: str
    secret_ref: str | None = None
    display_name: str = ""
    protocol: str = "claude"
    models: list[ApiProfileModel] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "displayName": self.display_name or self.provider,
            "protocol": self.protocol,
            "baseUrl": self.base_url,
            "defaultModel": self.default_model,
            "models": [
                model.model_dump(mode="json", by_alias=True)
                for model in (
                    self.models
                    or [ApiProfileModel(id=self.default_model, name=self.default_model)]
                )
            ],
            "authMode": self.auth_mode,
            "timeoutSeconds": self.timeout_seconds,
            "enabled": self.enabled,
            "keyConfigured": bool(self.api_key),
            "source": self.source,
        }


class ApiProfileStore:
    def __init__(
        self,
        settings: Any,
        secret_store: ProfileSecretStore | None = None,
    ) -> None:
        self.settings = settings
        self.path = Path(settings.profile_store_path)
        self._secret_store = secret_store or self._default_secret_store(settings)
        self._lock = threading.RLock()
        self._needs_migration = False
        self._profiles = self._load()
        self._seed_environment_profiles()
        if self._needs_migration:
            self._save()

    @staticmethod
    def _default_secret_store(settings: Any) -> ProfileSecretStore:
        if getattr(settings, "profile_secret_store_backend", "keyring") == "file":
            return FileProfileSecretStore(Path(settings.profile_secret_store_path))
        return KeyringProfileSecretStore()

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                profile.public()
                for profile in sorted(self._profiles.values(), key=lambda item: item.id)
            ]

    def resolve(self, provider: str, profile_id: str = "") -> ProviderProfile:
        with self._lock:
            selected_id = profile_id or self._default_profile_id(provider)
            profile = self._profiles.get(selected_id)
            if not profile or profile.provider != provider or not profile.enabled:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "API_PROFILE_UNAVAILABLE",
                        "message": f"API profile is unavailable: {selected_id}",
                    },
                )
            if not profile.api_key:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "API_PROFILE_KEY_MISSING",
                        "message": f"API key is not configured for profile: {selected_id}",
                    },
                )
            return profile

    def default_profile_id(self, provider: str) -> str:
        with self._lock:
            return self._default_profile_id(provider)

    def _default_profile_id(self, provider: str) -> str:
        ordered = sorted(
            (
                profile
                for profile in self._profiles.values()
                if profile.provider == provider
            ),
            key=lambda item: (not item.id.startswith("default-"), item.id),
        )
        ready = [profile for profile in ordered if profile.enabled and profile.api_key]
        selected = ready[0] if ready else (ordered[0] if ordered else None)
        return selected.id if selected else f"default-{provider}"

    def upsert(self, request: ApiProfileWriteRequest) -> dict[str, Any]:
        with self._lock:
            self._validate(request.id, request.base_url)
            current = self._profiles.get(request.id)
            if current and current.source == "environment":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "ENVIRONMENT_PROFILE_READ_ONLY"},
                )
            if (
                current
                and not request.api_key
                and (
                    request.provider != current.provider
                    or request.protocol != current.protocol
                    or request.base_url.rstrip("/") != current.base_url
                    or request.auth_mode != current.auth_mode
                )
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "PROFILE_KEY_REQUIRED_FOR_ROUTE_CHANGE"},
                )

            api_key = request.api_key or (current.api_key if current else "")
            secret_ref = current.secret_ref if current else None
            previous_secret = current.api_key if current else ""
            key_updated = bool(request.api_key)
            if key_updated:
                secret_ref = secret_ref or self._new_secret_ref()
                self._set_secret(secret_ref, request.api_key)

            profile = ProviderProfile(
                id=request.id,
                provider=request.provider,
                display_name=request.display_name,
                protocol=request.protocol,
                base_url=request.base_url.rstrip("/"),
                api_key=api_key,
                default_model=request.default_model,
                models=list(request.models),
                auth_mode=request.auth_mode,
                timeout_seconds=max(1.0, min(request.timeout_seconds, 600.0)),
                enabled=request.enabled,
                source="managed",
                secret_ref=secret_ref if api_key else None,
            )
            self._profiles[profile.id] = profile
            try:
                self._save()
            except Exception as exc:
                if current is None:
                    self._profiles.pop(profile.id, None)
                else:
                    self._profiles[profile.id] = current
                if key_updated and secret_ref:
                    self._restore_secret(secret_ref, previous_secret)
                raise exc
            return profile.public()

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            profile = self._profiles.get(profile_id)
            if not profile or profile.source == "environment":
                return False
            if profile.secret_ref:
                self._delete_secret(profile.secret_ref)
            del self._profiles[profile_id]
            try:
                self._save()
            except Exception as exc:
                self._profiles[profile_id] = profile
                if profile.secret_ref and profile.api_key:
                    self._restore_secret(profile.secret_ref, profile.api_key)
                raise exc
            return True

    def _seed_environment_profiles(self) -> None:
        mimo_models = [
            ApiProfileModel.model_validate(
                {
                    "id": model_id,
                    "name": metadata.get("name") or model_id,
                    "supportsImages": bool(metadata.get("supportsImages", True)),
                    "contextWindowTokens": int(metadata.get("contextWindowTokens", 100_000)),
                    "maxOutputTokens": int(metadata.get("maxOutputTokens", 8_192)),
                    "supportsNativeTools": True,
                    "supportsThinking": False,
                }
            )
            for model_id, metadata in MIMO_MODELS.items()
        ]
        if self.settings.mimo_default_model not in {model.id for model in mimo_models}:
            mimo_models.insert(
                0,
                ApiProfileModel(
                    id=self.settings.mimo_default_model,
                    name=self.settings.mimo_default_model,
                ),
            )
        claude_model_ids = list(self.settings.claude_code_models)
        if self.settings.claude_code_default_model not in claude_model_ids:
            claude_model_ids.insert(0, self.settings.claude_code_default_model)
        defaults = [
            ProviderProfile(
                id="default-mimo",
                provider="mimo",
                display_name="MiMo",
                protocol="claude",
                base_url=str(self.settings.mimo_messages_url).rstrip("/"),
                api_key=str(self.settings.mimo_api_key),
                default_model=str(self.settings.mimo_default_model),
                models=mimo_models,
                auth_mode=str(self.settings.mimo_auth_mode),
                timeout_seconds=float(self.settings.mimo_timeout_seconds),
                enabled=True,
                source="environment",
            ),
            ProviderProfile(
                id="default-claude-code",
                provider="claude-code",
                display_name="Claude Code",
                protocol="claude",
                base_url=str(self.settings.claude_code_messages_url).rstrip("/"),
                api_key=str(self.settings.claude_code_api_key),
                default_model=str(self.settings.claude_code_default_model),
                models=[
                    ApiProfileModel(id=model_id, name=model_id)
                    for model_id in claude_model_ids
                ],
                auth_mode=str(self.settings.claude_code_auth_mode),
                timeout_seconds=float(self.settings.claude_code_timeout_seconds),
                enabled=True,
                source="environment",
            ),
        ]
        for profile in defaults:
            try:
                self._validate(profile.id, profile.base_url)
            except HTTPException as exc:
                code = str((exc.detail or {}).get("code") or "INVALID_PROFILE_URL")
                raise ValueError(
                    f"environment provider profile {profile.id} is invalid: {code}"
                ) from exc
            self._profiles.setdefault(profile.id, profile)

    def _validate(self, profile_id: str, base_url: str) -> None:
        if not _PROFILE_ID.fullmatch(profile_id):
            raise HTTPException(status_code=400, detail={"code": "INVALID_PROFILE_ID"})
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise HTTPException(status_code=400, detail={"code": "INVALID_PROFILE_URL"})
        if parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail={"code": "INVALID_PROFILE_URL"})
        if parsed.query or parsed.fragment:
            raise HTTPException(status_code=400, detail={"code": "INVALID_PROFILE_URL"})
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(status_code=400, detail={"code": "INSECURE_PROFILE_URL"})
        allowed = {host.lower() for host in self.settings.profile_allowed_hosts}
        if allowed and parsed.hostname.lower() not in allowed:
            raise HTTPException(status_code=400, detail={"code": "PROFILE_HOST_NOT_ALLOWED"})

    def _load(self) -> dict[str, ProviderProfile]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            schema_version = raw.get("schemaVersion") if isinstance(raw, dict) else None
            if schema_version == "2":
                legacy = _PersistedProfileFileV2.model_validate(raw)
                payload_items = [self._migrate_v2_profile(item) for item in legacy.profiles]
                self._needs_migration = True
            else:
                payload = _PersistedProfileFile.model_validate(raw)
                payload_items = list(payload.profiles)
            profiles: dict[str, ProviderProfile] = {}
            for item in payload_items:
                self._validate(item.id, item.base_url)
                api_key = ""
                if item.secret_ref:
                    api_key = self._secret_store.get(item.secret_ref) or ""
                    if not api_key:
                        raise ValueError(
                            f"managed profile secret is missing: {item.id}"
                        )
                profile = ProviderProfile(
                    id=item.id,
                    provider=item.provider,
                    display_name=item.display_name,
                    protocol=item.protocol,
                    base_url=item.base_url,
                    api_key=api_key,
                    default_model=item.default_model,
                    models=list(item.models),
                    auth_mode=item.auth_mode,
                    timeout_seconds=item.timeout_seconds,
                    enabled=item.enabled,
                    source=item.source,
                    secret_ref=item.secret_ref,
                )
                profiles[profile.id] = profile
            return profiles
        except (
            OSError,
            ValueError,
            TypeError,
            ValidationError,
            HTTPException,
            ProfileSecretStoreError,
        ) as exc:
            raise ValueError(
                f"managed profile store is invalid and was not loaded: {self.path}"
            ) from exc

    def _migrate_v2_profile(
        self,
        item: _PersistedProviderProfileV2,
    ) -> _PersistedProviderProfile:
        model_ids = [item.default_model]
        if item.provider == "mimo":
            model_ids = list(MIMO_MODELS)
            if item.default_model not in model_ids:
                model_ids.insert(0, item.default_model)
        return _PersistedProviderProfile.model_validate(
            {
                "id": item.id,
                "provider": item.provider,
                "display_name": "MiMo" if item.provider == "mimo" else "Claude Code",
                "protocol": "claude",
                "base_url": item.base_url,
                "secret_ref": item.secret_ref,
                "default_model": item.default_model,
                "models": [
                    {
                        "id": model_id,
                        "name": str(MIMO_MODELS.get(model_id, {}).get("name") or model_id),
                        "supportsImages": bool(MIMO_MODELS.get(model_id, {}).get("supportsImages", True)),
                        "contextWindowTokens": int(MIMO_MODELS.get(model_id, {}).get("contextWindowTokens", 100_000)),
                        "maxOutputTokens": int(MIMO_MODELS.get(model_id, {}).get("maxOutputTokens", 8_192)),
                        "supportsNativeTools": True,
                        "supportsThinking": False,
                    }
                    for model_id in model_ids
                ],
                "auth_mode": item.auth_mode,
                "timeout_seconds": item.timeout_seconds,
                "enabled": item.enabled,
                "source": "managed",
            }
        )

    def _save(self) -> None:
        managed = [
            {
                "id": profile.id,
                "provider": profile.provider,
                "display_name": profile.display_name,
                "protocol": profile.protocol,
                "base_url": profile.base_url,
                "secret_ref": profile.secret_ref,
                "default_model": profile.default_model,
                "models": [
                    model.model_dump(mode="json", by_alias=True)
                    for model in profile.models
                ],
                "auth_mode": profile.auth_mode,
                "timeout_seconds": profile.timeout_seconds,
                "enabled": profile.enabled,
                "source": "managed",
            }
            for profile in self._profiles.values()
            if profile.source == "managed"
        ]
        payload = _PersistedProfileFile.model_validate(
            {
                "schemaVersion": _PROFILE_STORE_SCHEMA_VERSION,
                "profiles": managed,
            }
        ).model_dump(mode="json", by_alias=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _new_secret_ref() -> str:
        return f"profile:{uuid.uuid4().hex}"

    def _set_secret(self, secret_ref: str, secret: str) -> None:
        try:
            self._secret_store.set(secret_ref, secret)
        except ProfileSecretStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PROFILE_SECRET_STORE_UNAVAILABLE",
                    "message": "Secure profile secret storage is unavailable",
                },
            ) from exc

    def _delete_secret(self, secret_ref: str) -> None:
        try:
            self._secret_store.delete(secret_ref)
        except ProfileSecretStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PROFILE_SECRET_STORE_UNAVAILABLE",
                    "message": "Secure profile secret storage is unavailable",
                },
            ) from exc

    def _restore_secret(self, secret_ref: str, secret: str) -> None:
        try:
            if secret:
                self._secret_store.set(secret_ref, secret)
            else:
                self._secret_store.delete(secret_ref)
        except ProfileSecretStoreError as exc:
            raise RuntimeError(
                "profile metadata update failed and secure secret rollback also failed"
            ) from exc
