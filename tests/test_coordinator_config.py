import json
import os
import sqlite3

import pytest
from typer.testing import CliRunner

from octopus.cli import app
from octopus.coordinator.config import (
    ConfigError,
    CoordinatorConfig,
    coordinator_dir,
    merged_env,
    parse_listen,
)
from octopus.coordinator.inspect import Inspector, ago, render_jobs
from octopus.coordinator.jobs import SqliteJobStore, State
from octopus.coordinator.logs import LogStore
from octopus.proto import FailureReason, LogChunk

BASE = {
    "OCTOPUS_WEBHOOK_SECRET": "s3cret",
    "OCTOPUS_GITHUB_TOKEN": "github_pat_X",
    "OCTOPUS_GITHUB_REPO": "acme/app",
    "OCTOPUS_PUBLIC_IP": "203.0.113.7",
}


def config(**overrides):
    env = {**BASE, **overrides}
    return CoordinatorConfig.from_env({k: v for k, v in env.items() if v is not None})


def test_token_mode_from_the_setup_env_file():
    c = config()
    assert c.auth_mode == "token" and c.github_token.get_secret_value() == "github_pat_X"
    assert c.allowed_repos == ["acme/app"] and (c.listen_host, c.listen_port) == ("127.0.0.1", 8080)
    assert "s3cret" not in repr(c) and "github_pat_X" not in repr(c)
    assert c.node_config()["messagingOverrides"]["nat"] == "extip:203.0.113.7"


def test_app_mode(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("pem")
    c = config(
        OCTOPUS_GITHUB_TOKEN=None, OCTOPUS_GITHUB_APP_ID="123", OCTOPUS_GITHUB_APP_KEY_FILE=str(key)
    )
    assert c.auth_mode == "app" and c.app_key_file == key


def test_lists_and_rotation():
    c = config(
        OCTOPUS_ALLOWED_REPOS="acme/lib, acme/web",
        OCTOPUS_ALLOWED_ORGS="acme-labs",
        OCTOPUS_WEBHOOK_SECRET_PREVIOUS="old",
        OCTOPUS_ENTRY_NODES="/ip4/1/tcp/2",
    )
    assert c.allowed_repos == ["acme/app", "acme/lib", "acme/web"]
    assert c.allowed_orgs == ["acme-labs"] and c.entry_nodes == ["/ip4/1/tcp/2"]
    assert [s.get_secret_value() for s in c.webhook_secrets] == ["s3cret", "old"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OCTOPUS_WEBHOOK_SECRET": None}, "no webhook secret"),
        ({"OCTOPUS_GITHUB_TOKEN": None}, "no GitHub credentials"),
        ({"OCTOPUS_GITHUB_TOKEN": None, "OCTOPUS_GITHUB_APP_ID": "1"}, "KEY_FILE is not"),
        ({"OCTOPUS_GITHUB_APP_ID": "1", "OCTOPUS_GITHUB_APP_KEY_FILE": "/nope"}, "no such file"),
        ({"OCTOPUS_GITHUB_REPO": None}, "nothing is allowed"),
        ({"OCTOPUS_GITHUB_REPO": "acme"}, "owner/name"),
        ({"OCTOPUS_PUBLIC_IP": None}, "no public IP"),
        ({"OCTOPUS_WEBHOOK_LISTEN": "8080"}, "host:port"),
        ({"OCTOPUS_WEBHOOK_LISTEN": "0.0.0.0:http"}, "port"),
        ({"OCTOPUS_TCP_PORT": "x"}, "OCTOPUS_TCP_PORT"),
        ({"OCTOPUS_TCP_PORT": "0"}, "OCTOPUS_TCP_PORT"),
    ],
)
def test_invalid_settings(overrides, message):
    with pytest.raises(ConfigError, match=message):
        config(**overrides)


def test_errors_never_contain_secrets():
    with pytest.raises(ConfigError) as e:
        config(OCTOPUS_PUBLIC_IP=None)
    assert "s3cret" not in str(e.value) and "github_pat_X" not in str(e.value)


def test_parse_listen_ipv6():
    assert parse_listen("[::1]:9000") == ("::1", 9000)


def test_environment_overrides_the_env_file(tmp_path):
    env_file = tmp_path / "coordinator.env"
    env_file.write_text("# c\nOCTOPUS_GITHUB_REPO=acme/app\nOCTOPUS_WEBHOOK_SECRET=from-file\n")
    merged = merged_env(env_file, {"OCTOPUS_WEBHOOK_SECRET": "from-env", "HOME": "/root"})
    assert merged == {"OCTOPUS_GITHUB_REPO": "acme/app", "OCTOPUS_WEBHOOK_SECRET": "from-env"}


def test_unreadable_env_file(tmp_path):
    with pytest.raises(ConfigError, match="could not read"):
        merged_env(tmp_path)  # a directory


def test_coordinator_dir_is_apart_from_the_runner(tmp_path):
    assert coordinator_dir(tmp_path) == tmp_path / "coordinator"


# --- coordinator start: refusals before anything starts ---------------------------------


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in list(os.environ):
        if var.startswith("OCTOPUS_"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_start_without_env_file(clean_env):
    result = run("coordinator", "start", "--env-file", str(clean_env / "missing.env"))
    assert result.exit_code == 2 and "no such env file" in result.output


def test_start_with_bad_settings(clean_env):
    env_file = clean_env / "coordinator.env"
    env_file.write_text("OCTOPUS_GITHUB_REPO=acme/app\n")
    result = run("coordinator", "start", "--env-file", str(env_file))
    assert result.exit_code == 3 and "no webhook secret" in result.output


def test_start_refuses_a_second_coordinator(clean_env):
    env_file = clean_env / "coordinator.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    directory = clean_env / "data" / "coordinator"
    directory.mkdir(parents=True)
    (directory / "coordinator.state.json").write_text(json.dumps({"pid": os.getpid()}))
    result = run("coordinator", "start", "--env-file", str(env_file))
    assert result.exit_code == 1 and "already running" in result.output


def test_status_when_not_running(clean_env):
    result = run("coordinator", "status")
    assert result.exit_code == 5 and "not running" in result.output


# --- read-only inspection ---------------------------------------------------------------


@pytest.fixture
def stores(tmp_path):
    directory = tmp_path / "coordinator"
    directory.mkdir()
    jobs = SqliteJobStore(str(directory / "jobs.db"))
    logs = LogStore(str(directory / "logs.db"))
    yield directory, jobs, logs
    jobs.close()
    logs.close()


def test_readonly_stores_cannot_write(stores):
    directory, jobs, _ = stores
    jobs.create("j1", "acme/app")
    ro = SqliteJobStore(str(directory / "jobs.db"), readonly=True)
    assert ro.get("j1").state is State.QUEUED
    with pytest.raises(sqlite3.OperationalError):
        ro.create("j2", "acme/app")
    ro.close()


def test_readonly_store_needs_an_existing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        SqliteJobStore(str(tmp_path / "none.db"), readonly=True)


def test_reader_sees_what_the_coordinator_writes_later(stores):
    directory, jobs, _ = stores
    inspector = Inspector(directory)
    jobs.create("j1", "acme/app")
    assert [r["id"] for r in inspector.list_jobs()] == ["j1"]
    inspector.close()


def test_follow_ends_for_a_job_that_never_had_a_log(stores):
    directory, jobs, _ = stores
    jobs.create("j1", "acme/app")
    jobs.transition("j1", State.FAILED, actor="t", reason=FailureReason.POLICY_REJECTED)
    inspector = Inspector(directory)
    assert list(inspector.follow("j1", sleep=lambda s: None)) == []
    inspector.close()


def test_follow_ends_with_a_note_when_the_log_was_cut_off(stores):
    directory, jobs, logs = stores
    jobs.create("j1", "acme/app")
    jobs.transition("j1", State.FAILED, actor="t", reason=FailureReason.RUNNER_LOST)
    logs.put("r1", LogChunk(job_id="j1", seq=0, data="half a log\n"))
    inspector = Inspector(directory)
    out = list(inspector.follow("j1", sleep=lambda s: None, now=lambda: 10**10))
    assert out == ["half a log\n", "[octopus] log incomplete: the runner stopped sending\n"]
    inspector.close()


def test_follow_waits_for_gaps_and_announces_a_new_attempt(stores):
    directory, jobs, logs = stores
    jobs.create("j1", "acme/app")
    logs.put("r1", LogChunk(job_id="j1", seq=0, data="a\n"))
    logs.put("r1", LogChunk(job_id="j1", seq=2, data="c\n"))  # seq 1 still missing
    inspector = Inspector(directory)
    steps = []

    def sleep(_):
        steps.append(len(steps))
        if len(steps) == 1:
            logs.put("r1", LogChunk(job_id="j1", seq=1, data="b\n"))
        elif len(steps) == 2:
            logs._clock = lambda: 10**10  # the retry's stream is newer
            logs.put("r2", LogChunk(job_id="j1", seq=0, data="again\n", final=True))
            jobs.transition("j1", State.ASSIGNED, actor="t")
            jobs.transition("j1", State.FAILED, actor="t", reason=FailureReason.JOB_FAILED)

    out = list(inspector.follow("j1", sleep=sleep))
    assert out == ["a\n", "b\n", "c\n", "[octopus] job moved to runner r2\n", "again\n"]
    inspector.close()


def test_render_jobs():
    assert render_jobs([], 0) == "no jobs"
    row = {
        "id": "1",
        "repo": "acme/app",
        "state": "failed",
        "reason": "job-failed",
        "runner_id": "abcdefghijkl",
        "step": None,
        "updated_at": 0,
    }
    lines = render_jobs([row], 125).splitlines()
    assert lines[0].split() == ["JOB", "REPO", "STATE", "REASON", "RUNNER", "STEP", "UPDATED"]
    assert lines[1].split() == [
        "1",
        "acme/app",
        "failed",
        "job-failed",
        "abcdefgh",
        "-",
        "2m",
        "ago",
    ]


@pytest.mark.parametrize(
    ("seconds", "text"), [(5, "5s ago"), (90, "1m ago"), (7200, "2h ago"), (172800, "2d ago")]
)
def test_ago(seconds, text):
    assert ago(0, seconds) == text
