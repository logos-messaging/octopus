from __future__ import annotations

import base64
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .base import Handler, Transport

log = logging.getLogger(__name__)

_BINDINGS = Path(__file__).resolve().parents[3] / "vendor" / "logos-delivery-python-bindings"

CONNECTED = "Connected"
DISCONNECTED = "Disconnected"
_DEDUP_SIZE = 4096

StatusCallback = Callable[[str], None]


class LogosLibraryError(RuntimeError):
    """liblogosdelivery or its Python bindings could not be loaded."""


def _load_bindings():
    """The bindings load lib/liblogosdelivery.so relative to their checkout."""
    root = os.environ.get("OCTOPUS_BINDINGS", str(_BINDINGS))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import logosdelivery  # noqa: PLC0415
    except (ImportError, OSError) as e:
        raise LogosLibraryError(
            f"could not load logos-delivery bindings from: {root}; expected "
            f"{root}/lib/liblogosdelivery.so (see README, set OCTOPUS_BINDINGS "
            f"to relocate): {e}"
        ) from e
    return logosdelivery


PRESET = "logos.test"


@dataclass
class LogosConfig:
    """Octopus-level settings: the network comes from the `logos.test` preset."""

    mode: str = "Core"  # "Core" (relay, for coordinators) or "Edge" (light client)
    entry_nodes: list[str] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)  # raw `messagingOverrides`

    def to_node_config(self) -> dict:
        ov = dict(self.overrides)
        if self.entry_nodes:
            ov["entry-node"] = list(self.entry_nodes)
        cfg: dict = {"mode": self.mode, "preset": PRESET}
        if ov:
            cfg["messagingOverrides"] = ov
        return cfg


class LogosTransport(Transport):
    """Transport over liblogosdelivery's messaging API (logos-delivery-python-bindings).

    The library invokes our event callback on its own thread. That callback only
    parses and enqueues; a single worker thread runs the handlers, so they may
    block and never run on the library thread.
    """

    def __init__(
        self,
        config: LogosConfig | dict,
        *,
        auto_restart_after_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config.to_node_config() if isinstance(config, LogosConfig) else config
        self._auto_restart_after_s = auto_restart_after_s
        self._clock = clock
        self._handlers: dict[str, Handler] = {}
        self._status_cbs: list[StatusCallback] = []
        self._node = None
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()
        self._status_cond = threading.Condition()
        self._status = DISCONNECTED
        self._disconnected_since: float | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.dropped_events = 0

    # lifecycle

    def start(self) -> None:
        with self._lock:
            if self._node is not None:
                return
            self._stop_evt.clear()
            self._queue = queue.Queue()
            self._worker = threading.Thread(target=self._dispatch, name="octopus-ld", daemon=True)
            self._worker.start()
            try:
                self._node = self._create_node()
            except Exception:
                self._shutdown_worker()
                raise
            self._refresh_status()
            if self._auto_restart_after_s is not None:
                self._supervisor = threading.Thread(
                    target=self._supervise, name="octopus-ld-sup", daemon=True
                )
                self._supervisor.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_evt.set()
            node, self._node = self._node, None
            if node is not None:
                node.stop_and_destroy()
            self._shutdown_worker()
            self._set_status(DISCONNECTED, notify=False)

    def restart(self) -> None:
        """Recreate the node and replay every recorded subscription."""
        with self._lock:
            if self._node is None:
                raise RuntimeError("could not restart: LogosTransport is not started")
            old, self._node = self._node, None
            old.stop_and_destroy()
            with self._status_cond:
                self._disconnected_since = None
            self._node = self._create_node()
            self._refresh_status()

    def __enter__(self) -> LogosTransport:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def install_signal_handlers(self) -> None:
        """Stop the transport on SIGINT/SIGTERM. Main thread only."""

        def _handler(signum, frame):
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    # Transport API

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            node = self._require_node()
            self._handlers[topic] = handler
            node.subscribe_content_topic(topic).unwrap()

    def unsubscribe(self, topic: str) -> None:
        with self._lock:
            node = self._require_node()
            self._handlers.pop(topic, None)
            node.unsubscribe_content_topic(topic).unwrap()

    def publish(self, topic: str, payload: bytes) -> None:
        message = {
            "contentTopic": topic,
            "payload": base64.b64encode(payload).decode(),
            "ephemeral": False,
        }
        self._require_node().send_message(message).unwrap()

    def multiaddrs(self) -> list[str]:
        """Addresses other nodes can use as `entry_nodes` to reach this one."""
        raw = self._require_node().get_node_info("MyMultiaddresses").unwrap()
        return [a for a in str(raw).strip('"').split(",") if a]

    # health

    @property
    def status(self) -> str:
        with self._status_cond:
            return self._status

    def on_status_change(self, cb: StatusCallback) -> None:
        self._status_cbs.append(cb)

    def wait_connected(self, timeout: float) -> bool:
        deadline = self._clock() + timeout
        with self._status_cond:
            while self._status == DISCONNECTED:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._status_cond.wait(remaining)
            return True

    # internals

    def _require_node(self):
        if self._node is None:
            raise RuntimeError("could not use LogosTransport: it is not started")
        return self._node

    def _create_node(self):
        ld = _load_bindings()
        node = ld.NodeWrapper.create_and_start(self._config, event_cb=self._on_event).unwrap()
        try:
            for topic in self._handlers:
                node.subscribe_content_topic(topic).unwrap()
        except Exception:
            node.stop_and_destroy()
            raise
        return node

    def _refresh_status(self) -> None:
        node = self._node
        if node is None:
            return
        res = node.get_connection_status()
        if res.is_ok():
            self._set_status(str(res.ok_value).strip('"'))

    def _set_status(self, status: str, notify: bool = True) -> None:
        with self._status_cond:
            changed = status != self._status
            self._status = status
            if status == DISCONNECTED:
                if self._disconnected_since is None:
                    self._disconnected_since = self._clock()
            else:
                self._disconnected_since = None
            self._status_cond.notify_all()
        if changed and notify:
            self._queue.put(("status", status))

    def _on_event(self, ret: int, msg: bytes) -> None:
        # Library event thread: parse, filter, enqueue. Nothing else.
        try:
            event = json.loads(msg)
            kind = event.get("eventType")
        except (ValueError, AttributeError):
            self.dropped_events += 1
            return
        if kind == "message_received":
            self._queue.put(("msg", event))
        elif kind == "node_health_change":
            status = event.get("status")
            if isinstance(status, str):
                self._set_status(status)

    def _dispatch(self) -> None:
        q = self._queue
        while True:
            item = q.get()
            if item is None:
                return
            kind, data = item
            try:
                if kind == "msg":
                    self._deliver(data)
                else:
                    for cb in list(self._status_cbs):
                        cb(data)
            except Exception:
                log.exception("could not dispatch logos event")

    def _deliver(self, event: dict) -> None:
        message = event.get("message") or {}
        topic = message.get("contentTopic")
        payload = message.get("payload")
        handler = self._handlers.get(topic) if topic else None
        if handler is None or payload is None:
            return
        mhash = event.get("messageHash")
        if mhash:
            if mhash in self._seen:
                return
            self._seen[mhash] = None
            if len(self._seen) > _DEDUP_SIZE:
                self._seen.popitem(last=False)
        handler(topic, base64.b64decode(payload))

    def _shutdown_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            self._queue.put(None)
            if worker is not threading.current_thread():
                worker.join(timeout=5)

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop_evt.wait(1.0):
            if self._should_restart():
                try:
                    self.restart()
                    backoff = 1.0
                except Exception:
                    log.exception("could not restart logos node")
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 60.0)

    def _should_restart(self) -> bool:
        with self._status_cond:
            since = self._disconnected_since
        return (
            self._auto_restart_after_s is not None
            and since is not None
            and self._clock() - since >= self._auto_restart_after_s
        )
