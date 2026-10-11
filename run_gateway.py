"""Console runner for the ActionDesign thin gateway."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    config_path = project_root / ".env.local.json"
    if config_path.exists():
        with config_path.open(encoding="utf-8-sig") as handle:
            config = json.load(handle)
        for name, value in config.items():
            os.environ[name] = str(value)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8888")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    runtime_root = project_root / "logs" / "local"
    runtime_root.mkdir(parents=True, exist_ok=True)
    pid_file = runtime_root / "gateway.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    import uvicorn

    from claude_agent_sdk.actiondesign_gateway.app import app

    print(f"Admin: http://127.0.0.1:{args.port}/admin/", flush=True)
    print(
        "HTTP requests and agent events are printed here. Press Ctrl+C to stop.",
        flush=True,
    )
    try:
        uvicorn.run(
            app, host="127.0.0.1", port=args.port, log_level="info", access_log=True
        )
    finally:
        if pid_file.exists() and pid_file.read_text(encoding="utf-8") == str(
            os.getpid()
        ):
            pid_file.unlink()


if __name__ == "__main__":
    main()
