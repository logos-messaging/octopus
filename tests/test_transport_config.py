import pytest

from octopus.transport import config


def test_coordinator_advertises_public_ip():
    cfg = config.coordinator_config("203.0.113.7", entry_nodes=["/ip4/1.1.1.1/tcp/60000/p2p/Qm"])
    o = cfg["messagingOverrides"]
    assert cfg["mode"] == "Core"
    assert o["nat"] == "extip:203.0.113.7"
    assert o["entry-node"] == ["/ip4/1.1.1.1/tcp/60000/p2p/Qm"]
    assert o["cluster-id"] == config.CLUSTER_ID


def test_coordinator_requires_public_ip():
    with pytest.raises(ValueError, match="public_ip"):
        config.coordinator_config("")


def test_runner_needs_an_entry_node():
    with pytest.raises(ValueError, match="entry node"):
        config.runner_config([])


def test_runner_defaults():
    o = config.runner_config(["/dns4/c.example/tcp/60000/p2p/Qm"])["messagingOverrides"]
    assert "nat" not in o and o["tcp-port"] == config.DEFAULT_TCP_PORT


def test_from_env():
    env = {"OCTOPUS_ENTRY_NODES": " /a , /b ", "OCTOPUS_NAT": "any", "OCTOPUS_TCP_PORT": "61000"}
    o = config.from_env("runner", env)["messagingOverrides"]
    assert o["entry-node"] == ["/a", "/b"] and o["nat"] == "any" and o["tcp-port"] == 61000
    c = config.from_env("coordinator", {"OCTOPUS_PUBLIC_IP": "198.51.100.1"})
    assert c["messagingOverrides"]["nat"] == "extip:198.51.100.1"
    with pytest.raises(ValueError, match="role"):
        config.from_env("nope", {})
