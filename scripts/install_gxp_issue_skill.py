"""Install the local GXP skill for Codex and/or Claude Code, preserving backups."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "skills" / "gxp-issue-root-cause"
NAME = "gxp-issue-root-cause"


def install(client: str, client_home: Path) -> dict[str, str]:
    client_home = client_home.expanduser().resolve()
    destination = client_home / "skills" / NAME
    files = {
        "SKILL.md": SOURCE
        / ("SKILL.md" if client == "codex" else "clients/claude/SKILL.md"),
        "references/business-rules.md": SOURCE / "references/business-rules.md",
        "references/acceptance-cases.md": SOURCE / "references/acceptance-cases.md",
    }
    if client == "codex":
        files["agents/openai.yaml"] = SOURCE / "agents/openai.yaml"
    payloads = {name: source.read_bytes() for name, source in files.items()}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = client_home / "skill-backups" / f"{NAME}-{stamp}"
    results = {}
    for name, payload in payloads.items():
        target = destination / name
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Skill file escapes its destination: {name}")
        if target.exists() and target.read_bytes() == payload:
            results[name] = "unchanged"
            continue
        if target.exists():
            backup_target = backup / name
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".gxp-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            temporary.replace(target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        if (
            hashlib.sha256(target.read_bytes()).digest()
            != hashlib.sha256(payload).digest()
        ):
            raise RuntimeError(f"Installed file verification failed: {name}")
        results[name] = "installed"
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude", "both"), default="both")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
    )
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"),
    )
    args = parser.parse_args()
    clients = ("codex", "claude") if args.client == "both" else (args.client,)
    for client in clients:
        client_home = getattr(args, f"{client}_home")
        results = install(client, client_home)
        print(f"{client}: {client_home / 'skills' / NAME}")
        for name, status in results.items():
            print(f"  {status}: {name}")


if __name__ == "__main__":
    main()
