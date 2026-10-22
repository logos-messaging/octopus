import os

import pytest

from octopus.transport import config


def test_coordinator_advertises_public_ip():
    cfg = config.coordinator_config("203.0.113.7", entry_nodes=["/ip4/1.1.1.1/tcp/60000/p2p/Qm"])
    o = cfg["messagingOverrides"]
    assert cfg["mode"] == "Core"
    assert o["nat"] == "extip:203.0.113.7"
    assert cfg["preset"] == "logos.test"
    assert o["entry-node"] == ["/ip4/1.1.1.1/tcp/60000/p2p/Qm"]


def test_coordinator_requires_public_ip():
    with pytest.raises(ValueError, match="public_ip"):
        config.coordinator_config("")


def test_runner_defaults_use_the_preset():
    o = config.runner_config()["messagingOverrides"]
    assert "entry-node" not in o and o["tcp-port"] == config.DEFAULT_TCP_PORT


def test_runner_has_no_port_mapping_unless_asked():
    # The library's default is "any"; its port mapping can crash the node at stop (#64).
    assert config.runner_config()["messagingOverrides"]["nat"] == "none"
    assert config.runner_config(nat="any")["messagingOverrides"]["nat"] == "any"
    assert config.from_env("runner", {})["messagingOverrides"]["nat"] == "none"


def test_from_env():
    env = {"OCTOPUS_ENTRY_NODES": " /a , /b ", "OCTOPUS_NAT": "any", "OCTOPUS_TCP_PORT": "61000"}
    o = config.from_env("runner", env)["messagingOverrides"]
    assert o["entry-node"] == ["/a", "/b"] and o["nat"] == "any" and o["tcp-port"] == 61000
    c = config.from_env("coordinator", {"OCTOPUS_PUBLIC_IP": "198.51.100.1"})
    assert c["messagingOverrides"]["nat"] == "extip:198.51.100.1"
    with pytest.raises(ValueError, match="role"):
        config.from_env("nope", {})


def test_storage_path_is_set_only_when_given(tmp_path):
    assert "local-storage-path" not in config.runner_config()["messagingOverrides"]
    o = config.runner_config(storage_path=tmp_path / "node")["messagingOverrides"]
    assert o["local-storage-path"] == str(tmp_path / "node")
    c = config.coordinator_config("203.0.113.7", storage_path=tmp_path / "c")
    assert c["messagingOverrides"]["local-storage-path"] == str(tmp_path / "c")


def _capture_node_config(monkeypatch) -> list[dict]:
    """Replace LogosTransport: record the node config, then fail the start."""
    from octopus.transport import logos

    seen: list[dict] = []

    class FakeTransport:
        def __init__(self, cfg, *, fast_stop=False):
            assert fast_stop  # a long-running command must not wait for the node teardown
            seen.append(cfg)

    def fail_start(self, *a, **kw):
        raise logos.LogosLibraryError("no library in this test")

    monkeypatch.setattr(logos, "LogosTransport", FakeTransport)
    monkeypatch.setattr("octopus.agent.runner_agent.RunnerAgent.start", fail_start)
    monkeypatch.setattr("octopus.coordinator.service.Coordinator.start", fail_start)
    return seen


def test_runner_start_keeps_node_state_in_its_data_dir(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app
    from octopus.crypto import encode_public_key, generate_private_key

    seen = _capture_node_config(monkeypatch)
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    cfg = str(tmp_path / "runner.json")
    key = encode_public_key(generate_private_key().public_key())
    cli = CliRunner()
    assert (
        cli.invoke(app, ["runner", "setup", "--config", cfg, "--coordinator", key]).exit_code == 0
    )
    assert cli.invoke(app, ["runner", "allow", "acme/app", "--config", cfg]).exit_code == 0
    result = cli.invoke(app, ["runner", "start", "--config", cfg])
    assert result.exit_code == 4, result.output
    path = seen[0]["messagingOverrides"]["local-storage-path"]
    assert path == str(tmp_path / "data" / "node")


def test_coordinator_start_keeps_node_state_in_its_data_dir(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    seen = _capture_node_config(monkeypatch)
    env_file = tmp_path / "coordinator.env"
    env_file.write_text(
        "OCTOPUS_WEBHOOK_SECRET=s\nOCTOPUS_GITHUB_TOKEN=t\nOCTOPUS_GITHUB_REPO=acme/app\n"
        "OCTOPUS_PUBLIC_IP=203.0.113.7\n"
    )
    for k in [k for k in os.environ if k.startswith("OCTOPUS_")]:
        monkeypatch.delenv(k)
    data = tmp_path / "data"
    result = CliRunner().invoke(
        app, ["coordinator", "start", "--env-file", str(env_file), "--data-dir", str(data)]
    )
    assert result.exit_code == 4, result.output
    path = seen[0]["messagingOverrides"]["local-storage-path"]
    assert path == str(data / "coordinator" / "node")
