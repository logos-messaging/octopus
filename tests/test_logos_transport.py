import threading
import time

import pytest
from conftest import node_ports

pytestmark = pytest.mark.needs_lib

TOPIC = "/octopus/1/test/proto"


def _overrides() -> dict:
    return {
        **node_ports(),
        "nat": "none",
        "cluster-id": 4242,
        "num-shards-in-network": 1,
    }


def test_two_nodes_exchange_message(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from octopus.transport.logos import LogosConfig, LogosTransport

    a = LogosTransport(
        LogosConfig(overrides=_overrides())
    )
    got = []
    a.start()
    try:
        tcp_addr = next(m for m in a.multiaddrs() if "/tcp/" in m)
        b = LogosTransport(
            LogosConfig(entry_nodes=[tcp_addr], overrides=_overrides())
        )
        b.start()
        try:
            b.subscribe(TOPIC, lambda t, p: got.append((p, threading.current_thread().name)))
            a.subscribe(TOPIC, lambda t, p: None)
            time.sleep(10)  # let the mesh form
            a.publish(TOPIC, b"hello")
            deadline = time.time() + 30
            while not got and time.time() < deadline:
                time.sleep(0.5)
            assert got == [(b"hello", "octopus-ld")]
        finally:
            b.stop()
    finally:
        a.stop()
