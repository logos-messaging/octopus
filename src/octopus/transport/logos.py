from __future__ import annotations

import base64
import json
import logging
import queue
import signal
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from ..process import mark_native_node_left
from . import library
from .base import ChannelHandler, ChannelTransport, Cipher, Handler, SendResult

log = logging.getLogger(__name__)

DISCONNECTED = "Disconnected"
_DEDUP_SIZE = 4096

StatusCallback = Callable[[str], None]


class LogosLibraryError(RuntimeError):
    """liblogosdelivery or its Python bindings could not be loaded."""


class LogosError(RuntimeError):
    """A liblogosdelivery call failed."""


def _unwrap(res, what: str):
    """Unwrap a bindings Result, naming what failed."""
    try:
        return res.unwrap()
    except Exception as e:
        raise LogosError(f"{what}: {e}") from e


def _load_bindings():
    """The bindings load lib/liblogosdelivery.so relative to where their package lives."""
    root = library.bindings_root()
    if root is not None and (root / "logosdelivery").is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import logosdelivery
    except (ImportError, OSError) as e:
        expected = library.library_path(root) if root is not None else "lib/liblogosdelivery.so"
        raise LogosLibraryError(
            f"could not load logos-delivery bindings from: {root}; expected "
            f"{expected} (run: octopus lib install, or set OCTOPUS_BINDINGS "
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


class LogosTransport(ChannelTransport):
    """Transport over liblogosdelivery's messaging API (logos-delivery-python-bindings).

    The library invokes our event callback on its own thread. That callback only
    parses and enqueues; a single worker thread runs the handlers, so they may
    block and never run on the library thread.
    """

    def __init__(
        self,
        config: LogosConfig | dict,
        *,
        fast_stop: bool = False,
    ) -> None:
        # With `fast_stop`, stop() leaves the native node running instead of destroying it
        # (teardown takes ~23 s and can crash). The process must then end with
        # `octopus.process.leave`, which exits with os._exit.
        self.fast_stop = fast_stop
        self._config = config.to_node_config() if isinstance(config, LogosConfig) else config
        self._topic_handlers: dict[str, Handler] = {}
        self._status_callbacks: list[StatusCallback] = []
        self._node = None
        self._running = False
        # Bumped per node so events from a destroyed node are ignored.
        self._node_generation = 0
        self._event_queue: queue.Queue = queue.Queue()
        # Runs the subscribe() handlers, so they may block without stalling the
        # library's event thread. Kept so stop() can join it.
        self._handler_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._status_changed = threading.Condition()
        self._status = DISCONNECTED
        self._delivered_hashes: OrderedDict[str, None] = OrderedDict()
        self._channels: dict[str, _Channel] = {}
        # request_id -> callback, plus results that arrived before their callback was set.
        self._send_waiters: dict[str, SendResult | None] = {}
        self._send_results: dict[str, tuple[bool, str | None]] = {}
        self.dropped_events = 0

    # lifecycle

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            self._running = True
            self._event_queue = queue.Queue()
            self._handler_thread = threading.Thread(
                target=self._run_handlers, name="octopus-ld", daemon=True
            )
            self._handler_thread.start()
            try:
                self._node = self._create_node()
            except Exception:
                self._teardown()
                raise
            self._refresh_status()

    def stop(self) -> None:
        # Handlers take self._lifecycle_lock, so it must not be held while joining their thread.
        with self._lifecycle_lock:
            if not self._running:
                return
            node, handler_thread = self._detach()
        if node is not None:
            if self.fast_stop:
                mark_native_node_left()
            else:
                node.stop_and_destroy()
        if handler_thread is not None and handler_thread is not threading.current_thread():
            handler_thread.join(timeout=5)
        self._set_status(DISCONNECTED, notify=False)

    def restart(self) -> None:
        """Recreate the node and replay every recorded subscription.

        On failure the transport stays running with no node, so a later
        restart() can try again.
        """
        with self._lifecycle_lock:
            if not self._running:
                raise RuntimeError("could not restart: LogosTransport is not started")
            old, self._node = self._node, None
            if old is not None:
                old.stop_and_destroy()
            self._node = self._create_node()
            self._refresh_status()

    def __enter__(self) -> LogosTransport:  # noqa: PYI034 (typing.Self needs Python 3.11)
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
        with self._lifecycle_lock:
            node = self._require_node()
            previous = self._topic_handlers.get(topic)
            self._topic_handlers[topic] = handler
            try:
                _unwrap(
                    node.subscribe_content_topic(topic),
                    f"could not subscribe to topic: {topic}",
                )
            except LogosError:
                if previous is None:
                    del self._topic_handlers[topic]
                else:
                    self._topic_handlers[topic] = previous
                raise

    def unsubscribe(self, topic: str) -> None:
        with self._lifecycle_lock:
            node = self._require_node()
            _unwrap(
                node.unsubscribe_content_topic(topic),
                f"could not unsubscribe from topic: {topic}",
            )
            self._topic_handlers.pop(topic, None)

    def publish(self, topic: str, payload: bytes) -> None:
        message = {
            "contentTopic": topic,
            "payload": base64.b64encode(payload).decode(),
            "ephemeral": False,
        }
        # Under the lock so a publish during restart() waits for the new node.
        with self._lifecycle_lock:
            _unwrap(
                self._require_node().send_message(message),
                f"could not publish to topic: {topic}",
            )

    # reliable encrypted channels

    def open_channel(
        self,
        channel_id: str,
        topic: str,
        sender_id: str,
        cipher: Cipher,
        on_message: ChannelHandler,
    ) -> None:
        with self._lifecycle_lock:
            node = self._require_node()
            channel = _Channel(channel_id, topic, sender_id, cipher, on_message)
            channel.create(node)
            self._channels[channel_id] = channel

    def send_on_channel(
        self, channel_id: str, payload: bytes, on_result: SendResult | None = None
    ) -> None:
        message = {"payload": base64.b64encode(payload).decode(), "ephemeral": False}
        with self._lifecycle_lock:
            request_id = _unwrap(
                self._require_node().channel_send(channel_id, message),
                f"could not send on channel: {channel_id}",
            )
        self._register_send(str(request_id), on_result)

    def close_channel(self, channel_id: str) -> None:
        with self._lifecycle_lock:
            self._channels.pop(channel_id, None)
            if self._node is not None:
                _unwrap(
                    self._node.channel_close(channel_id),
                    f"could not close channel: {channel_id}",
                )

    def _register_send(self, request_id: str, on_result: SendResult | None) -> None:
        if on_result is None:
            return
        with self._lifecycle_lock:
            early = self._send_results.pop(request_id, None)
            if early is None:
                self._send_waiters[request_id] = on_result
                return
        on_result(*early)

    def _finish_send(self, request_id: str, delivered: bool, reason: str | None) -> None:
        with self._lifecycle_lock:
            waiter = self._send_waiters.pop(request_id, None)
            if waiter is None:
                self._send_results[request_id] = (delivered, reason)
                if len(self._send_results) > _DEDUP_SIZE:
                    self._send_results.pop(next(iter(self._send_results)))
                return
        waiter(delivered, reason)

    def multiaddrs(self) -> list[str]:
        """Addresses other nodes can use as `entry_nodes` to reach this one."""
        with self._lifecycle_lock:
            raw = _unwrap(
                self._require_node().get_node_info("MyMultiaddresses"),
                "could not get node multiaddresses",
            )
        return [a for a in str(raw).strip('"').split(",") if a]

    # health

    @property
    def status(self) -> str:
        with self._status_changed:
            return self._status

    def on_status_change(self, cb: StatusCallback) -> None:
        self._status_callbacks.append(cb)

    def wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._status_changed:
            while self._status == DISCONNECTED:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._status_changed.wait(remaining)
            return True

    # internals

    def _require_node(self):
        if self._node is None:
            raise RuntimeError("could not use LogosTransport: it is not started")
        return self._node

    def _detach(self):
        """Mark stopped and take ownership of the node and handler thread. Lock held."""
        self._running = False
        self._node_generation += 1
        node, self._node = self._node, None
        handler_thread, self._handler_thread = self._handler_thread, None
        if handler_thread is not None:
            self._event_queue.put(None)
        return node, handler_thread

    def _teardown(self) -> None:
        _, handler_thread = self._detach()
        if handler_thread is not None:
            handler_thread.join(timeout=5)

    def _create_node(self):
        ld = _load_bindings()
        self._node_generation += 1
        generation = self._node_generation
        node = _unwrap(
            ld.NodeWrapper.create_and_start(
                self._config, event_cb=lambda ret, msg: self._on_event(generation, msg)
            ),
            "could not start logos node",
        )
        try:
            for topic in self._topic_handlers:
                _unwrap(
                    node.subscribe_content_topic(topic),
                    f"could not re-subscribe to topic: {topic}",
                )
            for channel in self._channels.values():
                channel.create(node)
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
        with self._status_changed:
            changed = status != self._status
            self._status = status
            self._status_changed.notify_all()
        if changed and notify:
            self._event_queue.put(("status", status))

    def _on_event(self, generation: int, msg: bytes) -> None:
        # Library event thread: parse, filter, enqueue. Nothing else.
        if generation != self._node_generation:
            return
        try:
            event = json.loads(msg)
            kind = event.get("eventType")
        except (ValueError, AttributeError):
            self.dropped_events += 1
            return
        if kind == "message_received":
            self._event_queue.put(("msg", event))
        elif kind in _CHANNEL_EVENTS:
            self._event_queue.put(("chan", event))
        elif kind == "node_health_change":
            status = event.get("status")
            if isinstance(status, str):
                self._set_status(status)

    def _run_handlers(self) -> None:
        q = self._event_queue
        while True:
            item = q.get()
            if item is None:
                return
            kind, data = item
            try:
                if kind == "msg":
                    self._deliver(data)
                elif kind == "chan":
                    self._deliver_channel_event(data)
                else:
                    for cb in list(self._status_callbacks):
                        cb(data)
            except Exception:
                log.exception("could not dispatch logos event")

    def _deliver(self, event: dict) -> None:
        message = event.get("message") or {}
        topic = message.get("contentTopic")
        payload = message.get("payload")
        handler = self._topic_handlers.get(topic) if topic else None
        if handler is None or payload is None:
            return
        mhash = event.get("messageHash")
        if mhash and mhash in self._delivered_hashes:
            return
        handler(topic, base64.b64decode(payload))
        # Only after the handler succeeded, so a failed message can be retried
        # when Store recovery delivers it again.
        if mhash:
            self._delivered_hashes[mhash] = None
            if len(self._delivered_hashes) > _DEDUP_SIZE:
                self._delivered_hashes.popitem(last=False)

    def _deliver_channel_event(self, event: dict) -> None:
        kind = event.get("eventType")
        channel_id = event.get("channelId")
        if kind == "channel_message_received":
            channel = self._channels.get(channel_id)
            payload = event.get("payload")
            if channel is not None and payload is not None:
                channel.on_message(str(event.get("senderId", "")), base64.b64decode(payload))
        elif kind == "channel_message_sent":
            self._finish_send(str(event.get("requestId")), True, None)
        elif kind == "channel_message_error":
            self._finish_send(str(event.get("requestId")), False, str(event.get("error")))
        elif kind == "channel_message_lost":
            log.warning("channel message lost: %s: %s", channel_id, event.get("reason"))


_CHANNEL_EVENTS = {
    "channel_message_received",
    "channel_message_sent",
    "channel_message_error",
    "channel_message_lost",
}


class _Channel:
    """One channel and the cffi cipher callbacks the library keeps calling until destroy.

    The callbacks run inline on the library's event loop. They only transform bytes and
    never log, so no plaintext or key reaches a log line.
    """

    def __init__(
        self,
        channel_id: str,
        topic: str,
        sender_id: str,
        cipher: Cipher,
        on_message: ChannelHandler,
    ) -> None:
        import cffi

        self.channel_id = channel_id
        self.topic = topic
        self.sender_id = sender_id
        self.on_message = on_message
        self._ffi = cffi.FFI()
        self._keep: list = []
        self._enc = self._make_callback(cipher.encrypt)
        self._dec = self._make_callback(cipher.decrypt)

    def _make_callback(self, fn):
        ffi = self._ffi
        holder: dict = {}

        @ffi.callback("int(void*, const uint8_t*, size_t, const uint8_t**, size_t*)")
        def cb(_user, inp, in_len, out, out_len):
            try:
                data = ffi.buffer(inp, in_len)[:] if in_len else b""
                result = fn(data)
                # ffi.new("uint8_t[]", bytes) would add a trailing NUL byte.
                buf = ffi.new("uint8_t[]", max(len(result), 1))
                ffi.memmove(buf, result, len(result))
                holder["buf"] = buf  # must outlive the call; the library copies it on return
                out[0] = buf
                out_len[0] = len(result)
                return 0
            except Exception:  # noqa: BLE001  never let an exception cross into C
                return 1

        self._keep.append((cb, holder))
        return cb

    def create(self, node) -> None:
        ffi = self._ffi
        _unwrap(
            node.channel_create(
                self.channel_id,
                self.topic,
                self.sender_id,
                encrypt_fn=int(ffi.cast("uintptr_t", self._enc)),
                decrypt_fn=int(ffi.cast("uintptr_t", self._dec)),
                crypto_user_data=0,
            ),
            f"could not create channel: {self.channel_id}",
        )
