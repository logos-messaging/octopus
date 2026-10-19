import threading
import time

import pytest

pytestmark = pytest.mark.needs_lib

TOPIC = "/octopus/1/test/proto"


def _ports(base: int) -> dict:
    return {
        "tcp-port": base,
        "discv5-udp-port": base + 1,
        "quic-port": base + 2,
        "nat": "none",
    }


def test_two_nodes_exchange_message():
    from octopus.transport.logos import LogosConfig, LogosTransport

    a = LogosTransport(
        LogosConfig(mode="Core", cluster_id=198, num_shards=1, overrides=_ports(60110))
    )
    got = []
    a.start()
    try:
        tcp_addr = next(m for m in a.multiaddrs() if "/tcp/" in m)
        b = LogosTransport(
            LogosConfig(
                mode="Core",
                cluster_id=198,
                num_shards=1,
                entry_nodes=[tcp_addr],
                overrides=_ports(60120),
            )
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
