from __future__ import annotations

import argparse
import json
import os
import selectors
import socket
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--job-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    socket_path = Path(
        os.environ.get(
            "ACTIONDESIGN_MCP_STDIO_BROKER_SOCKET",
            "/run/actiondesign-mcp-deployer/stdio.sock",
        )
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.sendall(
        json.dumps(
            {
                "deploymentId": args.deployment_id,
                "revision": args.revision,
                "jobId": args.job_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, "socket")
    selector.register(sys.stdin.buffer, selectors.EVENT_READ, "stdin")
    try:
        while True:
            for key, _mask in selector.select():
                if key.data == "stdin":
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        client.shutdown(socket.SHUT_WR)
                        selector.unregister(sys.stdin.buffer)
                        continue
                    client.sendall(chunk)
                else:
                    chunk = client.recv(65536)
                    if not chunk:
                        return 0
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
    finally:
        selector.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
