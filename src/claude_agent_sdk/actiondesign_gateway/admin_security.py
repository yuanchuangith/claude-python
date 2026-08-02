from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AdminSessionStore:
    """Short-lived, in-memory administrator sessions."""

    def __init__(
        self,
        password: str,
        *,
        ttl_seconds: int = 3600,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._password = password
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.RLock()

    def login(
        self,
        password: str,
        *,
        actor: str = "unknown",
    ) -> tuple[dict[str, Any] | None, str]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            if self._blocked_until.get(actor, 0) > now:
                secrets.compare_digest(password, self._password or "disabled")
                return None, "blocked"
            if not self._password or not secrets.compare_digest(
                password,
                self._password,
            ):
                failures = [
                    item for item in self._failures.get(actor, []) if now - item < 1800
                ]
                failures.append(now)
                self._failures[actor] = failures
                if len(failures) >= 5:
                    self._blocked_until[actor] = now + 1800
                return None, "invalid"
            self._failures.pop(actor, None)
            self._blocked_until.pop(actor, None)
            token = secrets.token_urlsafe(32)
            expires_at = now + self._ttl_seconds
            self._sessions[token] = expires_at
        return {
            "sessionToken": token,
            "expiresAt": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "expiresInSeconds": self._ttl_seconds,
        }, "ok"

    def verify(self, token: str) -> bool:
        if not token:
            return False
        now = self._clock()
        with self._lock:
            self._prune(now)
            expires_at = self._sessions.get(token)
            return bool(expires_at and expires_at > now)

    def _prune(self, now: float) -> None:
        for token in [
            item for item, expires_at in self._sessions.items() if expires_at <= now
        ]:
            del self._sessions[token]
        for actor, blocked_until in list(self._blocked_until.items()):
            if blocked_until <= now:
                del self._blocked_until[actor]


class BrowserApiKeyStore:
    """Stores only a salted digest and display suffix for browser API keys."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def public(self) -> dict[str, Any]:
        with self._lock:
            payload = self._load()
        return {
            "configured": payload is not None,
            "masked": str(payload.get("masked") or "") if payload else "",
        }

    def rotate(self, api_key: str = "") -> dict[str, Any]:
        value = api_key.strip() or f"adk_{secrets.token_urlsafe(32)}"
        if len(value) < 16 or len(value) > 8192:
            raise ValueError("browser API key length is invalid")
        salt = secrets.token_hex(16)
        masked = f"****{value[-4:]}"
        payload = {
            "schemaVersion": "1",
            "salt": salt,
            "digest": _digest(salt, value),
            "masked": masked,
        }
        with self._lock:
            self._save(payload)
        return {"configured": True, "masked": masked, "apiKey": value}

    def verify(self, api_key: str) -> bool:
        if not api_key:
            return False
        with self._lock:
            payload = self._load()
        if payload is None:
            return False
        actual = _digest(str(payload["salt"]), api_key)
        return secrets.compare_digest(actual, str(payload["digest"]))

    def _load(self) -> dict[str, str] | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schemaVersion",
            "salt",
            "digest",
            "masked",
        }:
            raise ValueError("browser API key store is invalid")
        if payload.get("schemaVersion") != "1":
            raise ValueError("browser API key store schema is invalid")
        for key in ("salt", "digest", "masked"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise ValueError("browser API key store is invalid")
        return payload

    def _save(self, payload: dict[str, str]) -> None:
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
        finally:
            temp_path.unlink(missing_ok=True)


def _digest(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
