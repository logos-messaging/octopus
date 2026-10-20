"""HTTP endpoint for operators: `/healthz`, `/readyz` and `/metrics`.

A separate listener from the webhook server so it can stay on a private interface (it binds
to localhost by default) while the webhook is published through the TLS proxy. GET only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

log = logging.getLogger(__name__)

METRICS_LISTEN_ENV = "OCTOPUS_METRICS_LISTEN"
DEFAULT_METRICS_LISTEN = "127.0.0.1:9100"
REQUEST_TIMEOUT_S = 10
CONTENT_TYPE_METRICS = "text/plain; version=0.0.4; charset=utf-8"

# Named checks; the coordinator is ready when every one is true.
Checks = Callable[[], Mapping[str, bool]]


def parse_metrics_listen(value: str | None) -> tuple[str, int] | None:
    """`host:port` from OCTOPUS_METRICS_LISTEN; None when it is `off`. Raises ValueError."""
    text = (value or DEFAULT_METRICS_LISTEN).strip()
    if text.lower() in ("off", "none", "disabled", "false", "0"):
        return None
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"invalid {METRICS_LISTEN_ENV}, expected HOST:PORT or off: {text}")
    return host.strip("[]"), int(port)


class OpsServer:
    def __init__(
        self,
        render_metrics: Callable[[], str],
        checks: Checks | None = None,
        host: str = "127.0.0.1",
        port: int = 9100,
    ) -> None:
        handler = type(
            "Handler",
            (_Handler,),
            {
                "render_metrics": staticmethod(render_metrics),
                "checks": staticmethod(checks) if checks is not None else None,
            },
        )
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is None:
            self._thread = Thread(target=self._httpd.serve_forever, name="ops", daemon=True)
            self._thread.start()
            log.info("ops endpoint listening on %s:%d", *self.address)

    def stop(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join()
            self._thread = None
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    render_metrics: Callable[[], str]
    checks: Checks | None
    timeout = REQUEST_TIMEOUT_S
    server_version = "octopus"
    sys_version = ""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._reply(200, "ok\n")
        elif path == "/readyz":
            self._ready()
        elif path == "/metrics":
            try:
                body = self.render_metrics()
            except Exception:
                log.exception("could not render metrics")
                self._reply(500, "could not render metrics\n")
                return
            self._reply(200, body, CONTENT_TYPE_METRICS)
        else:
            self._reply(404, "not found\n")

    def do_POST(self) -> None:
        self._reply(405, "use GET\n")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _ready(self) -> None:
        try:
            checks = dict(self.checks()) if self.checks is not None else {}
        except Exception:
            log.exception("could not run readiness checks")
            checks = {"checks": False}
        ready = all(checks.values())
        body = json.dumps({"ready": ready, "checks": checks}, sort_keys=True) + "\n"
        self._reply(200 if ready else 503, body, "application/json")

    def _reply(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), format % args)
