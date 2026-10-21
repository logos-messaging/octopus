"""HTTP endpoint for GitHub webhooks, on the standard library's threading HTTP server.

Plain HTTP: put it behind a TLS-terminating reverse proxy (see docs/github-app.md). It binds
to localhost by default for that reason.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from ..github.webhooks import WebhookReceiver
from .pick import DEFAULT_PATH as PICK_PATH
from .pick import PickResponse

log = logging.getLogger(__name__)

DEFAULT_PATH = "/github/webhook"
# Seconds a client may take to send its request; stops slow clients from holding threads.
REQUEST_TIMEOUT_S = 10
# How long, and how much, to read of a refused request before closing (see _drain_request).
_DRAIN_SECONDS = 1.0
_DRAIN_MAX_BYTES = 1 << 20
_PICK_MAX_BODY = 4096


class WebhookServer:
    def __init__(
        self,
        receiver: WebhookReceiver,
        host: str = "127.0.0.1",
        port: int = 8080,
        path: str = DEFAULT_PATH,
        pick: Callable[[str | None], PickResponse] | None = None,
    ) -> None:
        handler = type(
            "Handler",
            (_Handler,),
            {"receiver": receiver, "path_": path, "pick": staticmethod(pick) if pick else None},
        )
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """Serve in a background thread."""
        if self._thread is None:
            self._thread = Thread(target=self._httpd.serve_forever, name="webhooks", daemon=True)
            self._thread.start()
            log.info("webhook server listening on %s:%d", *self.address)

    def stop(self) -> None:
        if self._thread is not None:
            # shutdown() waits for serve_forever to exit, so only call it when it runs.
            self._httpd.shutdown()
            self._thread.join()
            self._thread = None
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    receiver: WebhookReceiver
    path_: str
    pick: Callable[[str | None], PickResponse] | None = None
    timeout = REQUEST_TIMEOUT_S
    server_version = "octopus"
    sys_version = ""

    def do_POST(self) -> None:
        route = self.path.split("?", 1)[0]
        if self.pick is not None and route == PICK_PATH:
            self._pick()
            return
        if route != self.path_:
            self._reply(404, "not found")
            return
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            # GitHub always sends Content-Length; chunked bodies are not accepted.
            self._reply(411, "Content-Length required")
            return
        if int(length) > self.receiver.max_body_bytes:
            # Refuse before reading, so an oversized body never lands in memory.
            self._reply(413, "payload too large")
            return
        body = self.rfile.read(int(length))
        if len(body) != int(length):
            self._reply(400, "truncated body")
            return
        resp = self.receiver.handle(dict(self.headers.items()), body)
        self._reply(resp.status, resp.message)

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        known = route == self.path_ or (self.pick is not None and route == PICK_PATH)
        self._reply(405 if known else 404, "use POST")

    def _pick(self) -> None:
        # The request has no body; one that has is read (up to a limit) and ignored.
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit() or int(length) > _PICK_MAX_BODY:
            self._reply(413, "payload too large")
            return
        self.rfile.read(int(length))
        assert self.pick is not None
        resp = self.pick(self.headers.get("Authorization"))
        self._reply(resp.status, json.dumps(resp.body), "application/json", resp.headers)

    def _reply(
        self,
        status: int,
        message: str,
        content_type: str = "text/plain; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        if status >= 400:
            # The body may not have been read; do not reuse the connection.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)
        if status >= 400:
            self._drain_request()

    def _drain_request(self) -> None:
        """Read what the client is still sending before the socket closes.

        Closing with unread request bytes makes Windows reset the connection, and the reset can
        discard the response the client has not read yet (httpx.ReadError). The wait is bounded
        so a slow client cannot hold the thread.
        """
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + _DRAIN_SECONDS
            self.connection.settimeout(_DRAIN_SECONDS)
            drained = 0
            while drained < _DRAIN_MAX_BYTES and time.monotonic() < deadline:
                chunk = self.connection.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), format % args)
