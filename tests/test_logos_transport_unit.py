import base64
import json
import sys
import threading
import types
from typing import ClassVar

import pytest

from octopus.transport import logos
from octopus.transport.logos import LogosConfig, LogosLibraryError, LogosTransport

_real_load = logos._load_bindings


class Res:
    def __init__(self, value=None, err=None):
        self.ok_value, self._err = value, err

    def is_ok(self):
        return self._err is None

    def unwrap(self):
        if self._err:
            raise RuntimeError(self._err)
        return self.ok_value


class FakeNode:
    instances: ClassVar[list] = []

    def __init__(self, config, event_cb):
        self.config, self.event_cb = config, event_cb
        self.subscribed: list = []
        self.sent: list = []
        self.destroyed = 0
        self.status = "Connected"
        FakeNode.instances.append(self)

    @classmethod
    def create_and_start(cls, config, event_cb=None):
        return Res(cls(config, event_cb))

    def subscribe_content_topic(self, topic):
        self.subscribed.append(topic)
        return Res(0)

    def unsubscribe_content_topic(self, topic):
        self.subscribed.remove(topic)
        return Res(0)

    def send_message(self, message):
        self.sent.append(message)
        return Res("req-1")

    def get_connection_status(self):
        return Res(self.status)

    def stop_and_destroy(self):
        self.destroyed += 1
        return Res(0)

    def emit(self, event):
        self.event_cb(0, json.dumps(event).encode())


def received(topic, payload, mhash="0x1", source="live"):
    return {
        "eventType": "message_received",
        "messageHash": mhash,
        "message": {"contentTopic": topic, "payload": base64.b64encode(payload).decode()},
        "source": source,
    }


@pytest.fixture(autouse=True)
def fake_bindings(monkeypatch):
    FakeNode.instances = []
    monkeypatch.setattr(
        logos, "_load_bindings", lambda: types.SimpleNamespace(NodeWrapper=FakeNode)
    )


@pytest.fixture
def transport():
    t = LogosTransport({"mode": "Core"})
    t.start()
    yield t
    t.stop()


def wait(cond, timeout=10.0):
    ev = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        ev.wait(0.01)
    return False


def test_message_routed_to_handler_on_worker_thread(transport):
    got = []
    transport.subscribe("/t", lambda t, p: got.append((t, p, threading.current_thread())))
    node = FakeNode.instances[0]
    feeder = threading.current_thread()
    node.emit(received("/t", b"hi"))
    assert wait(lambda: got)
    topic, payload, thread = got[0]
    assert (topic, payload) == ("/t", b"hi")
    assert thread is not feeder and thread.name == "octopus-ld"


def test_blocking_handler_does_not_block_event_callback(transport):
    gate = threading.Event()
    transport.subscribe("/t", lambda t, p: gate.wait(5))
    node = FakeNode.instances[0]
    node.emit(received("/t", b"a", mhash="0xa"))
    done = threading.Event()
    threading.Thread(target=lambda: (node.emit(received("/t", b"b", mhash="0xb")), done.set())).start()
    assert done.wait(1)
    gate.set()


def test_ignored_and_malformed_events(transport):
    got = []
    transport.subscribe("/t", lambda t, p: got.append(p))
    node = FakeNode.instances[0]
    node.event_cb(0, b"not json")
    node.event_cb(0, b"[1]")
    node.emit({"eventType": "message_sent", "requestId": "x"})
    node.emit({"eventType": "message_received", "message": {"contentTopic": "/other", "payload": "AA=="}})
    node.emit(received("/t", b"ok", mhash="0xok"))
    assert wait(lambda: got == [b"ok"])
    assert transport.dropped_events == 2


def test_duplicate_message_hash_delivered_once(transport):
    got = []
    transport.subscribe("/t", lambda t, p: got.append(p))
    node = FakeNode.instances[0]
    node.emit(received("/t", b"x", mhash="0xd"))
    node.emit(received("/t", b"x", mhash="0xd", source="history"))
    node.emit(received("/t", b"y", mhash="0xe"))
    assert wait(lambda: got == [b"x", b"y"])


def test_publish_encodes_payload(transport):
    transport.publish("/t", b"\x00\x01")
    sent = FakeNode.instances[0].sent[0]
    assert sent["contentTopic"] == "/t" and base64.b64decode(sent["payload"]) == b"\x00\x01"


def test_lifecycle_is_idempotent():
    t = LogosTransport({})
    t.stop()  # before start
    t.start()
    t.start()
    assert len(FakeNode.instances) == 1
    t.stop()
    t.stop()
    assert FakeNode.instances[0].destroyed == 1
    with pytest.raises(RuntimeError):
        t.publish("/t", b"x")


def test_status_events_and_callbacks(transport):
    seen = []
    transport.on_status_change(seen.append)
    node = FakeNode.instances[0]
    assert transport.status == "Connected"
    node.emit({"eventType": "node_health_change", "status": "Disconnected"})
    assert wait(lambda: seen == ["Disconnected"])
    assert transport.status == "Disconnected"
    assert not transport.wait_connected(0.05)
    threading.Timer(
        0.05, lambda: node.emit({"eventType": "node_health_change", "status": "Connected"})
    ).start()
    assert transport.wait_connected(2)


def test_restart_resubscribes(transport):
    transport.subscribe("/a", lambda t, p: None)
    transport.subscribe("/b", lambda t, p: None)
    transport.restart()
    first, second = FakeNode.instances
    assert first.destroyed == 1
    assert second.subscribed == ["/a", "/b"]


def test_config_helper():
    cfg = LogosConfig(
        mode="Edge", entry_nodes=["/ip4/1.2.3.4/tcp/60000/p2p/x"], overrides={"tcp-port": 60001}
    ).to_node_config()
    assert cfg == {
        "mode": "Edge",
        "preset": "logos.test",
        "messagingOverrides": {
            "tcp-port": 60001,
            "entry-node": ["/ip4/1.2.3.4/tcp/60000/p2p/x"],
        },
    }
    assert LogosConfig().to_node_config() == {"mode": "Core", "preset": "logos.test"}


def test_missing_library_error(monkeypatch, tmp_path):
    monkeypatch.setattr(logos, "_load_bindings", _real_load)
    monkeypatch.setenv("OCTOPUS_BINDINGS", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "logosdelivery", raising=False)
    with pytest.raises(LogosLibraryError, match="lib/liblogosdelivery.so"):
        logos._load_bindings()


def test_failed_restart_can_be_retried(transport, monkeypatch):
    transport.subscribe("/a", lambda t, p: None)
    real = FakeNode.create_and_start.__func__
    monkeypatch.setattr(
        FakeNode, "create_and_start", classmethod(lambda cls, c, event_cb=None: Res(err="boom"))
    )
    with pytest.raises(logos.LogosError, match="could not start logos node"):
        transport.restart()
    monkeypatch.setattr(FakeNode, "create_and_start", classmethod(real))
    transport.restart()
    assert FakeNode.instances[-1].subscribed == ["/a"]
    transport.publish("/a", b"x")


def test_failed_subscribe_is_not_recorded(transport, monkeypatch):
    node = FakeNode.instances[0]
    monkeypatch.setattr(node, "subscribe_content_topic", lambda t: Res(err="bad topic"))
    with pytest.raises(logos.LogosError, match="could not subscribe to topic: /bad"):
        transport.subscribe("/bad", lambda t, p: None)
    assert "/bad" not in transport._topic_handlers
    transport.restart()  # replay must not trip over the failed topic


def test_events_from_old_node_ignored_after_restart(transport):
    got = []
    transport.subscribe("/t", lambda t, p: got.append(p))
    old = FakeNode.instances[0]
    transport.restart()
    old.emit(received("/t", b"stale", mhash="0xs"))
    old.emit({"eventType": "node_health_change", "status": "Disconnected"})
    FakeNode.instances[1].emit(received("/t", b"fresh", mhash="0xf"))
    assert wait(lambda: got == [b"fresh"])
    assert transport.status == "Connected"


def test_events_after_stop_ignored():
    t = LogosTransport({})
    t.start()
    node = FakeNode.instances[0]
    t.stop()
    node.emit({"eventType": "node_health_change", "status": "Connected"})
    assert t.status == "Disconnected"


def test_failed_handler_message_can_be_redelivered(transport):
    calls = []

    def handler(t, p):
        calls.append(p)
        if len(calls) == 1:
            raise ValueError("transient")

    transport.subscribe("/t", handler)
    node = FakeNode.instances[0]
    node.emit(received("/t", b"x", mhash="0xr"))
    node.emit(received("/t", b"x", mhash="0xr", source="history"))
    assert wait(lambda: len(calls) == 2)


def test_handler_can_subscribe_while_stopping():
    t = LogosTransport({})
    t.start()
    started, done = threading.Event(), threading.Event()

    def handler(topic, payload):
        started.set()
        threading.Event().wait(0.3)
        try:
            t.subscribe("/other", lambda *_: None)
        except RuntimeError:
            pass
        done.set()

    t.subscribe("/t", handler)
    FakeNode.instances[0].emit(received("/t", b"x"))
    assert started.wait(2)
    t.stop()  # must not deadlock against the handler
    assert done.wait(2)
