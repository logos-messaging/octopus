import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import node_ports

from octopus.crypto import PairCipher, derive_channel_key, generate_private_key

pytestmark = pytest.mark.needs_lib

TOPIC = "/octopus/1/test-chan/proto"
CHANNEL = "octopus-test-chan"


def _overrides() -> dict:
    return {
        **node_ports(),
        "nat": "none",
        "cluster-id": 4242,
        "num-shards-in-network": 1,
    }


def _expect(peer, line: str) -> None:
    # The library logs to stdout too; skip until our marker line shows up.
    for out in peer.stdout:
        if out.strip() == line:
            return
    raise AssertionError(f"peer exited before printing: {line}")


def test_encrypted_channel_between_two_nodes(monkeypatch, tmp_path):
    # Node state (sds.db) lives in the working dir, so node B gets its own process and dir.
    from octopus.transport.logos import LogosConfig, LogosTransport

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    monkeypatch.chdir(dir_a)

    ka, kb = generate_private_key(), generate_private_key()
    key = derive_channel_key(ka, kb.public_key(), CHANNEL)
    assert key == derive_channel_key(kb, ka.public_key(), CHANNEL)

    a = LogosTransport(LogosConfig(overrides=_overrides()))
    a.start()
    peer = None
    try:
        addr = next(m for m in a.multiaddrs() if "/tcp/" in m)
        args = {
            "entry": addr,
            "overrides": _overrides(),
            "key": base64.b64encode(key).decode(),
            "channel": CHANNEL,
            "topic": TOPIC,
            "lifetime": 90,
        }
        peer = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_channel_peer.py")), json.dumps(args)],
            cwd=dir_b,
            stdout=subprocess.PIPE,
            text=True,
        )
        _expect(peer, "READY")
        a.open_channel(CHANNEL, TOPIC, "alice", PairCipher(key), lambda s, p: None)
        time.sleep(10)  # let the mesh form
        # The channel's own sent-event needs a Store confirmation, absent here; JobAck is the app-level confirmation.
        a.send_on_channel(CHANNEL, b"jit-secret")
        _expect(peer, "GOT alice jit-secret")
    finally:
        if peer is not None:
            peer.kill()
            peer.wait()
        a.stop()
