"""`octopus init`: the guided setup, driven through its prompts."""

import json

import pytest
from _fake_github_repo import REPO, TOKEN, FakeRepoGitHub
from typer.testing import CliRunner

from octopus import cli_init
from octopus.agent import service
from octopus.cli import app
from octopus.crypto import encode_public_key, generate_private_key
from octopus.github.setup import read_env_file

URL = "https://octopus.example.org/github/webhook"
KEY = encode_public_key(generate_private_key().public_key())


class FakeService:
    def __init__(self, installed=False):
        self.installed = installed
        self.specs = []

    def status(self):
        return self.installed, "active" if self.installed else "not installed"

    def install(self, spec):
        self.specs.append(spec)
        self.installed = True
        return service.ServiceResult(path=service.Path("/tmp/octopus-runner.service"))


@pytest.fixture
def svc(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(service, "status", fake.status)
    monkeypatch.setattr(service, "install", fake.install)
    return fake


def init(*args, input=""):
    return CliRunner().invoke(app, ["init", *args], input=input)


def lines(*answers):
    return "".join(f"{a}\n" for a in answers)


# runner


def test_runner_from_nothing(tmp_path, svc):
    config = tmp_path / "runner.json"
    # key type, key, entry node, max jobs, allow, end of allowlist, install the service
    answers = lines("coordinator", KEY, "", "2", "acme/app", "", "y")
    result = init("--role", "runner", "--config", str(config), input=answers)
    assert result.exit_code == 0, result.output
    assert "[changed] trust" in result.output
    assert "[changed] allowlist" in result.output
    assert "[changed] service" in result.output
    assert "next: octopus runner start" in result.output
    settings = json.loads((tmp_path / "runner.settings.json").read_text())
    assert settings["coordinators"] == [KEY] and settings["max_jobs"] == 2
    assert "acme/app" in json.loads(config.read_text())["repos"]
    assert svc.specs and "--config" in svc.specs[0].argv


def test_runner_unlimited_parallel_jobs(tmp_path, svc):
    config = tmp_path / "runner.json"
    answers = lines("coordinator", KEY, "", "unlimited", "acme/app", "", "n")
    result = init("--role", "runner", "--config", str(config), input=answers)
    assert result.exit_code == 0, result.output
    assert "[changed] max jobs  unlimited" in result.output
    assert json.loads((tmp_path / "runner.settings.json").read_text())["max_jobs"] == 0


def test_runner_bad_parallel_jobs_keeps_current(tmp_path, svc):
    config = tmp_path / "runner.json"
    answers = lines("coordinator", KEY, "", "lots", "acme/app", "", "n")
    result = init("--role", "runner", "--config", str(config), input=answers)
    assert result.exit_code == 0, result.output
    assert "max jobs  kept 1" in result.output
    assert json.loads((tmp_path / "runner.settings.json").read_text())["max_jobs"] == 1


def test_runner_rerun_changes_nothing(tmp_path, svc):
    config = tmp_path / "runner.json"
    init("--role", "runner", "--config", str(config),
         input=lines("coordinator", KEY, "", "", "acme/app", "", "y"))  # fmt: skip
    before = (tmp_path / "runner.settings.json").read_text(), config.read_text()
    # add another key: no, entry node, max jobs, end of allowlist
    result = init("--role", "runner", "--config", str(config), input=lines("n", "", "", ""))
    assert result.exit_code == 0, result.output
    assert "[changed]" not in result.output.replace("[changed] entry nodes", "")
    assert "[     ok] trust" in result.output and "[     ok] service" in result.output
    assert ((tmp_path / "runner.settings.json").read_text(), config.read_text()) == before


def test_runner_asks_again_after_a_bad_key_or_repo(tmp_path, svc):
    config = tmp_path / "runner.json"
    answers = lines(
        "coordinator", "not-a-key", "coordinator", KEY, "", "", "a/b/c", "acme/app", "", "n"
    )
    result = init("--role", "runner", "--config", str(config), input=answers)
    assert result.exit_code == 0, result.output
    assert result.output.count("error:") == 2
    assert "[skipped] service" in result.output


def test_runner_steps_can_be_skipped(tmp_path, svc):
    config = tmp_path / "runner.json"
    result = init("--role", "runner", "--config", str(config), input=lines("", "", "", "", "n"))
    assert result.exit_code == 0, result.output
    assert "[   warn] trust" in result.output and "[   warn] allowlist" in result.output
    assert "run octopus init again" in result.output


def test_role_is_asked(tmp_path, svc):
    config = tmp_path / "runner.json"
    result = init("--config", str(config), input=lines("pilot", "runner", "", "", "", "", "n"))
    assert result.exit_code == 0, result.output
    assert result.output.count("Role of this host") == 2


# coordinator


@pytest.fixture
def gh(monkeypatch):
    fake = FakeRepoGitHub()
    monkeypatch.setattr(cli_init, "_client", lambda token, api_url: fake.client())
    monkeypatch.setattr("octopus.github.setup.PING_POLL_S", 0.0)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return fake


def test_coordinator_sets_up_the_repo_and_asks_to_start(tmp_path, gh):
    env_file = tmp_path / "coordinator.env"
    data = tmp_path / "data"
    # repo, webhook URL, example PR, public IP
    answers = lines(REPO, URL, "y", "203.0.113.7")
    result = init("--role", "coordinator", "--env-file", str(env_file), "--data-dir", str(data),
                  input=answers)  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "[     ok] repo setup" in result.output
    assert "[changed] public IP" in result.output
    assert "deploy/Caddyfile" in result.output
    assert "[skipped] webhook health" in result.output
    assert "octopus coordinator start --env-file" in result.output
    env = read_env_file(env_file)
    assert env["OCTOPUS_GITHUB_REPO"] == REPO and env["OCTOPUS_PUBLIC_IP"] == "203.0.113.7"
    assert env["OCTOPUS_GITHUB_TOKEN"] == TOKEN  # GITHUB_TOKEN is the coordinator's
    assert TOKEN not in result.output


def test_coordinator_checks_the_webhook_when_it_runs(tmp_path, gh, monkeypatch):
    env_file = tmp_path / "coordinator.env"
    data = tmp_path / "data"
    init("--role", "coordinator", "--env-file", str(env_file), "--data-dir", str(data),
         input=lines(REPO, URL, "n", "203.0.113.7"))  # fmt: skip
    monkeypatch.setattr(
        "octopus.coordinator.service.read_state", lambda d: {"public_key": "PUBKEY"}
    )
    # repo and URL come from the env file: accept them, no example PR
    result = init("--role", "coordinator", "--env-file", str(env_file), "--data-dir", str(data),
                  input=lines("", "", "n"))  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "[     ok] public IP" in result.output
    assert "--coordinator PUBKEY" in result.output
    assert "[     ok] webhook health" in result.output
    assert "octopus explain <job>" in result.output


def test_coordinator_refuses_a_plain_http_url(tmp_path, gh):
    env_file = tmp_path / "coordinator.env"
    result = init("--role", "coordinator", "--env-file", str(env_file),
                  input=lines(REPO, "http://octopus.example.org/hook"))  # fmt: skip
    assert result.exit_code == 1
    assert "[   fail] repo setup" in result.output and not env_file.exists()
