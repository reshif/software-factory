"""Tiny stdlib HTTP service exposing the Store over JSON.

The golden-path sample app: http.server + json only, no third-party
dependencies, so a new product cell has something real to point CI, the
holdout runner and the factory guidance at (final draft §12.4).

Run directly:  python -m app.server [port]
"""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import NotFound, Store

ITEM_PATH = re.compile(r"^/items/(\d+)$")


def make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to `store` (one per server instance)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "backend-service/0.1"

        def _send_json(self, status: int, payload) -> None:
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if self.path == "/items":
                self._send_json(200, store.list())
                return
            match = ITEM_PATH.match(self.path)
            if match:
                try:
                    self._send_json(200, store.get(int(match.group(1))))
                except NotFound:
                    self._send_json(404, {"error": "not found"})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/items":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
                item = store.add(data.get("name", ""))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(201, item)

        def do_DELETE(self) -> None:
            match = ITEM_PATH.match(self.path)
            if not match:
                self._send_json(404, {"error": "not found"})
                return
            try:
                store.delete(int(match.group(1)))
            except NotFound:
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(204, None)

        def log_message(self, fmt: str, *args) -> None:  # keep test/CI output quiet
            pass

    return Handler


def serve(port: int = 8080) -> None:
    handler = make_handler(Store())
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"backend-service listening on 127.0.0.1:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
