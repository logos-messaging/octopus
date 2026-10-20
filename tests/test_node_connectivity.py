"""Two local nodes: runner dials the coordinator via entry-node and receives its message."""

import base64
import json
import socket
import time

import pytest

from octopus.transport import config

pytestmark = pytest.mark.needs_lib


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_runner_receives_coordinator_message(tmp_path):
    from octopus.transport.logos import _load_bindings

    ld = _load_bindings()
    topic = "/octopus/1/connectivity/proto"
    received = []

    def on_event(ret, msg):
        event = json.loads(msg)
        if event.get("eventType") == "message_received":
            received.append(base64.b64decode(event["message"]["payload"]))

    coord = ld.NodeWrapper.create_and_start(
        config.coordinator_config("127.0.0.1", tcp_port=_free_port())
    ).unwrap()
    runner = None
    try:
        addrs = coord.get_node_info("MyMultiaddresses").unwrap().split(",")
        entry = next(a for a in addrs if "/tcp/" in a)
        runner = ld.NodeWrapper.create_and_start(
            config.runner_config([entry], tcp_port=_free_port()), event_cb=on_event
        ).unwrap()
        runner.subscribe_content_topic(topic).unwrap()
        coord.subscribe_content_topic(topic).unwrap()
        time.sleep(8)
        coord.send_message(
            {"contentTopic": topic, "payload": base64.b64encode(b"hi").decode(), "ephemeral": False}
        ).unwrap()
        deadline = time.time() + 20
        while not received and time.time() < deadline:
            time.sleep(0.5)
        assert received == [b"hi"]
    finally:
        if runner is not None:
            runner.stop_and_destroy()
        coord.stop_and_destroy()
