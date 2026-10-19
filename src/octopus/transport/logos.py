from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from .base import Handler, Transport

_BINDINGS = Path(__file__).resolve().parents[3] / "vendor" / "logos-delivery-python-bindings"


def _load_bindings():
    """The bindings load lib/liblogosdelivery.so relative to their checkout."""
    root = os.environ.get("OCTOPUS_BINDINGS", str(_BINDINGS))
    if root not in sys.path:
        sys.path.insert(0, root)
    import logosdelivery  # noqa: PLC0415

    return logosdelivery


class LogosTransport(Transport):
    """Transport over liblogosdelivery's messaging API (logos-delivery-python-bindings)."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._handlers: dict[str, Handler] = {}
        self._node = None

    def start(self) -> None:
        ld = _load_bindings()
        self._node = ld.NodeWrapper.create_and_start(
            self._config, event_cb=self._on_event
        ).unwrap()

    def stop(self) -> None:
        if self._node is not None:
            self._node.stop_and_destroy()
            self._node = None

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers[topic] = handler
        self._require_node().subscribe_content_topic(topic).unwrap()

    def unsubscribe(self, topic: str) -> None:
        self._handlers.pop(topic, None)
        self._require_node().unsubscribe_content_topic(topic).unwrap()

    def publish(self, topic: str, payload: bytes) -> None:
        message = {
            "contentTopic": topic,
            "payload": base64.b64encode(payload).decode(),
            "ephemeral": False,
        }
        self._require_node().send_message(message).unwrap()

    def _require_node(self):
        if self._node is None:
            raise RuntimeError("LogosTransport is not started")
        return self._node

    def _on_event(self, ret: int, msg: bytes) -> None:
        # Runs on the library's event thread: keep it short.
        try:
            event = json.loads(msg)
        except ValueError:
            return
        # TODO(issue: LogosTransport): confirm the onMessageReceived event schema.
        topic = event.get("contentTopic")
        payload = event.get("payload")
        handler = self._handlers.get(topic) if topic else None
        if handler is not None and payload is not None:
            handler(topic, base64.b64decode(payload))
