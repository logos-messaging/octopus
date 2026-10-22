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


def test_backfill_is_on_unless_a_node_turns_it_off():
    # Left on, a node with no earlier run replays 24 hours of every topic from the Store.
    assert "backfill-enabled" not in config.runner_config()["messagingOverrides"]
    assert config.runner_config(backfill=False)["messagingOverrides"]["backfill-enabled"] is False
    assert "backfill-enabled" not in config.coordinator_config("1.2.3.4")["messagingOverrides"]


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


# --- library log level (#93) ------------------------------------------------------------


@pytest.fixture
def no_log_level(monkeypatch):
    monkeypatch.delenv("OCTOPUS_LOG_LEVEL", raising=False)


def test_log_level_defaults_to_info(no_log_level):
    assert config.runner_config()["messagingOverrides"]["log-level"] == "INFO"
    assert config.coordinator_config("203.0.113.7")["messagingOverrides"]["log-level"] == "INFO"
    assert config.from_env("runner", {})["messagingOverrides"]["log-level"] == "INFO"


def test_log_level_from_the_environment(monkeypatch):
    monkeypatch.setenv("OCTOPUS_LOG_LEVEL", " debug ")
    assert config.runner_config()["messagingOverrides"]["log-level"] == "DEBUG"
    assert config.runner_config(log_level="WARN")["messagingOverrides"]["log-level"] == "WARN"
    env = {"OCTOPUS_LOG_LEVEL": "trace", "OCTOPUS_PUBLIC_IP": "198.51.100.1"}
    assert config.from_env("coordinator", env)["messagingOverrides"]["log-level"] == "TRACE"


@pytest.mark.parametrize("value", ["BOGUS", "verbose", "5"])
def test_an_invalid_log_level_is_refused_before_the_node(monkeypatch, value):
    # The library refuses to start a node with an unknown level ("failed parseLog...").
    with pytest.raises(ValueError, match="OCTOPUS_LOG_LEVEL"):
        config.env_log_level({"OCTOPUS_LOG_LEVEL": value})
    monkeypatch.setenv("OCTOPUS_LOG_LEVEL", value)
    with pytest.raises(ValueError, match="OCTOPUS_LOG_LEVEL"):
        config.runner_config()


def test_log_levels_are_the_chronicles_levels():
    assert config.LOG_LEVELS == (
        "NONE", "TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "ERROR", "FATAL"
    )


def _set_up_runner(cli, app, tmp_path):
    from octopus.crypto import encode_public_key, generate_private_key

    cfg = str(tmp_path / "runner.json")
    key = encode_public_key(generate_private_key().public_key())
    assert cli.invoke(app, ["runner", "setup", "--config", cfg, "--coordinator", key]).exit_code == 0
    assert cli.invoke(app, ["runner", "allow", "acme/app", "--config", cfg]).exit_code == 0
    return cfg


def test_runner_start_passes_the_log_level(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    seen = _capture_node_config(monkeypatch)
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OCTOPUS_LOG_LEVEL", "notice")
    cli = CliRunner()
    cfg = _set_up_runner(cli, app, tmp_path)
    result = cli.invoke(app, ["runner", "start", "--config", cfg])
    assert result.exit_code == 4, result.output
    assert seen[0]["messagingOverrides"]["log-level"] == "NOTICE"


def test_runner_start_refuses_an_invalid_log_level(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    seen = _capture_node_config(monkeypatch)
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    cli = CliRunner()
    cfg = _set_up_runner(cli, app, tmp_path)
    monkeypatch.setenv("OCTOPUS_LOG_LEVEL", "loud")
    result = cli.invoke(app, ["runner", "start", "--config", cfg])
    assert result.exit_code == 3, result.output
    assert "OCTOPUS_LOG_LEVEL" in result.output and seen == []


def _coordinator_env(tmp_path, extra=""):
    env_file = tmp_path / "coordinator.env"
    env_file.write_text(
        "OCTOPUS_WEBHOOK_SECRET=s\nOCTOPUS_GITHUB_TOKEN=t\nOCTOPUS_GITHUB_REPO=acme/app\n"
        "OCTOPUS_PUBLIC_IP=203.0.113.7\n" + extra
    )
    return env_file


def test_coordinator_start_reads_the_log_level_from_its_env_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    seen = _capture_node_config(monkeypatch)
    for k in [k for k in os.environ if k.startswith("OCTOPUS_")]:
        monkeypatch.delenv(k)
    env_file = _coordinator_env(tmp_path, "OCTOPUS_LOG_LEVEL=warn\n")
    result = CliRunner().invoke(
        app,
        ["coordinator", "start", "--env-file", str(env_file), "--data-dir", str(tmp_path / "d")],
    )
    assert result.exit_code == 4, result.output
    assert seen[0]["messagingOverrides"]["log-level"] == "WARN"


def test_coordinator_start_refuses_an_invalid_log_level(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    seen = _capture_node_config(monkeypatch)
    for k in [k for k in os.environ if k.startswith("OCTOPUS_")]:
        monkeypatch.delenv(k)
    env_file = _coordinator_env(tmp_path, "OCTOPUS_LOG_LEVEL=chatty\n")
    result = CliRunner().invoke(
        app,
        ["coordinator", "start", "--env-file", str(env_file), "--data-dir", str(tmp_path / "d")],
    )
    assert result.exit_code == 3, result.output
    assert "OCTOPUS_LOG_LEVEL" in result.output and seen == []


def test_ci_node_refuses_an_invalid_log_level(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app
    from octopus.transport import logos

    def never(cfg):
        raise AssertionError("no node may start with an invalid log level")

    monkeypatch.setattr(logos, "LogosTransport", never)
    monkeypatch.setenv("HOME", str(tmp_path))  # no saved settings of this machine
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OCTOPUS_LOG_LEVEL", "noisy")
    result = CliRunner().invoke(app, ["ci", "node", "--data-dir", str(tmp_path / "data")])
    assert result.exit_code == 3, result.output
    assert "OCTOPUS_LOG_LEVEL" in result.output


def test_every_library_build_has_runtime_filtering():
    """CI, the E2E test and the lib-latest release build through one action (#93)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / ".github"
    action = (root / "actions/liblogosdelivery/action.yml").read_text()
    assert 'params="-d:chronicles_runtime_filtering=on"' in action
    # Flags reach make through the environment: on its command line they would replace the
    # Makefile's own NIM_PARAMS (e.g. -d:disable_rln).
    assert 'NIM_PARAMS="$LIB_NIM_PARAMS" make DISABLE_RLN=true liblogosdelivery' in action
    assert "make NIM_PARAMS" not in action
    assert "${{ steps.flags.outputs.key }}" in action  # a flag change rebuilds the library
    release = (root / "workflows/release-lib.yml").read_text()
    assert "uses: ./.github/actions/liblogosdelivery" in release and "make " not in release
