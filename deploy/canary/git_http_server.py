from __future__ import annotations

import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class GitHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self._run_backend()

    def do_POST(self) -> None:  # noqa: N802
        self._run_backend()

    def _run_backend(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length)
        environment = {
            **os.environ,
            "GIT_PROJECT_ROOT": "/var/www",
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_ADDR": self.client_address[0],
            "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
        }
        completed = subprocess.run(
            ["/usr/lib/git-core/git-http-backend"],
            input=request_body,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            check=False,
        )
        header_block, _, body = completed.stdout.partition(b"\r\n\r\n")
        status = 200
        response_headers: list[tuple[str, str]] = []
        for raw_line in header_block.split(b"\r\n"):
            if not raw_line:
                continue
            name, _, value = raw_line.decode("latin-1").partition(":")
            if name.lower() == "status":
                status = int(value.strip().split(" ", 1)[0])
            elif name and value:
                response_headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in response_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18081), GitHandler).serve_forever()
