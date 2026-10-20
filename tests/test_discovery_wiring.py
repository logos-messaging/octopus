import pytest
from typer.testing import CliRunner

from octopus.agent.config import ConfigError, RunnerConfig
from octopus.agent.offers import OfferReceiver
from octopus.cli import app
from octopus.crypto import (
    encode_public_key,
    encode_signing_public,
    generate_private_key,
    generate_signing_key,
    node_id,
)
from octopus.transport import MemoryBus, MemoryTransport


def test_offer_receiver_trusts_a_coordinator_added_later():
    transport = MemoryTransport(MemoryBus())
    receiver = OfferReceiver(transport, generate_private_key(), {}, lambda offer: None)
    receiver.start()
    coordinator = generate_private_key().public_key()
    receiver.add_coordinator(node_id(coordinator), encode_public_key(coordinator))
    assert len(transport._channel_ids) == 1
    receiver.add_coordinator(node_id(coordinator), encode_public_key(coordinator))
    assert len(transport._channel_ids) == 1


def test_offer_receiver_rejects_a_mismatched_id():
    receiver = OfferReceiver(
        MemoryTransport(MemoryBus()), generate_private_key(), {}, lambda offer: None
    )
    other = generate_private_key().public_key()
    with pytest.raises(ValueError):
        receiver.add_coordinator("wrongid", encode_public_key(other))


def test_runner_config_validates_root_key(tmp_path):
    with pytest.raises(ConfigError):
        RunnerConfig(root_key="not-a-key").validate()
    key = encode_signing_public(generate_signing_key().public_key())
    assert RunnerConfig().merged({"OCTOPUS_ROOT_KEY": key}).root_key == key
    assert RunnerConfig(root_key=key).merged({}).root_key == key
    other = encode_signing_public(generate_signing_key().public_key())
    assert RunnerConfig(root_key=key).merged({}, root_key=other).root_key == other
    path = tmp_path / "s.json"
    RunnerConfig(root_key=key).save(path)
    assert RunnerConfig.load(path).root_key == key


def test_keygen_authorize_round_trip(tmp_path):
    runner = CliRunner()
    root_file = tmp_path / "root.key"
    out = runner.invoke(app, ["root", "keygen", "--out", str(root_file)])
    assert out.exit_code == 0 and "root public key:" in out.output
    again = runner.invoke(app, ["root", "keygen", "--out", str(root_file)])
    assert again.exit_code != 0

    out = runner.invoke(app, ["coordinator", "keygen", "--data-dir", str(tmp_path / "c")])
    assert out.exit_code == 0
    fields = dict(line.split(":", 1) for line in out.output.splitlines() if ":" in line)
    enc, sign = fields["enc public key"].strip(), fields["sign public key"].strip()
    cert_file = tmp_path / "cert.json"
    out = runner.invoke(
        app,
        [
            "root",
            "authorize",
            "--enc-pub",
            enc,
            "--sign-pub",
            sign,
            "--root-key-file",
            str(root_file),
            "--out",
            str(cert_file),
        ],
    )
    assert out.exit_code == 0, out.output
    assert fields["coordinator id"].strip() in cert_file.read_text()
