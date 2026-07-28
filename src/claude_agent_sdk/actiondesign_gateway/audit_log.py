from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_value


class AuditLogStore:
    """Bounded append-only system-change audit log."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()

    def record(
        self,
        *,
        action: str,
        success: bool,
        actor: str,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action[:128],
            "success": success,
            "actor": actor[:256],
            "requestId": request_id[:256],
            "details": redact_value(details or {}),
        }
        serialized = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        size = len(serialized.encode("utf-8")) + 1
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            self._rotate(size)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized + "\n")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return entry

    def list(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        entries: list[dict[str, Any]] = []
        with self._lock:
            paths = [self.path] + [
                self.path.with_name(f"{self.path.name}.{index}")
                for index in range(1, self.backup_count + 1)
            ]
            for path in paths:
                if not path.is_file():
                    continue
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict):
                        entries.append(value)
        entries.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return entries[offset : offset + limit], len(entries)

    def _rotate(self, incoming_bytes: int) -> None:
        current = self.path.stat().st_size if self.path.exists() else 0
        if not current or current + incoming_bytes <= self.max_bytes:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
