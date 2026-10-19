import os
import time

import pytest

pytestmark = pytest.mark.needs_lib


def test_two_nodes_exchange_message():
    from octopus.transport.logos import LogosTransport

    cfg = {"mode": "Core", "clusterId": 198, "numShardsInNetwork": 1}
    a, b = LogosTransport(cfg), LogosTransport(cfg)
    got = []
    a.start()
    b.start()
    try:
        b.subscribe("/octopus/1/test/proto", lambda t, p: got.append(p))
        time.sleep(5)
        a.publish("/octopus/1/test/proto", b"hello")
        deadline = time.time() + 30
        while not got and time.time() < deadline:
            time.sleep(0.5)
        assert got == [b"hello"]
    finally:
        a.stop()
        b.stop()
