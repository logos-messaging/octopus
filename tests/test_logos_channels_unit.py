import base64

import pytest
from test_logos_transport_unit import (  # noqa: F401  (fake_bindings fixture)
    FakeNode,
    Res,
    fake_bindings,
    wait,
)

from octopus.crypto import PairCipher
from octopus.transport import logos
from octopus.transport.logos import LogosTransport


class ChanNode(FakeNode):
    def __init__(self, config, event_cb):
        super().__init__(config, event_cb)
        self.created: list = []
        self.closed: list = []
        self.channel_sent: list = []

    def channel_create(self, channel_id, topic, sender_id, **kw):
        self.created.append((channel_id, topic, sender_id, kw))
        return Res(channel_id)

    def channel_send(self, channel_id, message):
        self.channel_sent.append((channel_id, message))
        return Res("req-9")

    def channel_close(self, channel_id):
        self.closed.append(channel_id)
        return Res(0)


@pytest.fixture
def transport(monkeypatch):
    import types

    monkeypatch.setattr(
        logos, "_load_bindings", lambda: types.SimpleNamespace(NodeWrapper=ChanNode)
    )
    FakeNode.instances = []
    t = LogosTransport({"mode": "Core"})
    t.start()
    yield t
    t.stop()


def cipher():
    return PairCipher(b"k" * 32)


def test_open_channel_passes_cipher_pointers(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    node = FakeNode.instances[0]
    _, topic, sender, kw = node.created[0]
    assert (topic, sender) == ("/t", "me")
    assert kw["encrypt_fn"] and kw["decrypt_fn"] and kw["encrypt_fn"] != kw["decrypt_fn"]


def test_received_event_reaches_handler(transport):
    got = []
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: got.append((s, p)))
    FakeNode.instances[0].emit(
        {
            "eventType": "channel_message_received",
            "channelId": "c1",
            "senderId": "peer",
            "payload": base64.b64encode(b"data").decode(),
        }
    )
    assert wait(lambda: got == [("peer", b"data")])


def test_send_result_reported_after_sent_event(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    results = []
    transport.send_on_channel("c1", b"x", lambda ok, reason: results.append((ok, reason)))
    node = FakeNode.instances[0]
    assert node.channel_sent[0][0] == "c1"
    node.emit({"eventType": "channel_message_sent", "channelId": "c1", "requestId": "req-9"})
    assert wait(lambda: results == [(True, None)])


def test_send_failure_event_reported(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    results = []
    transport.send_on_channel("c1", b"x", lambda ok, reason: results.append((ok, reason)))
    FakeNode.instances[0].emit(
        {"eventType": "channel_message_error", "channelId": "c1", "requestId": "req-9", "error": "boom"}
    )
    assert wait(lambda: results == [(False, "boom")])


def test_result_arriving_before_callback_is_not_lost(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    transport._finish_send("early", True, None)
    results = []
    transport._register_send("early", lambda ok, reason: results.append(ok))
    assert results == [True]


def test_close_channel(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    transport.close_channel("c1")
    assert FakeNode.instances[0].closed == ["c1"]


def test_restart_recreates_channels(transport):
    transport.open_channel("c1", "/t", "me", cipher(), lambda s, p: None)
    transport.restart()
    assert [c[0] for c in FakeNode.instances[1].created] == ["c1"]
