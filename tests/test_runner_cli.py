import json
import os
import threading

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from octopus.agent.config import ConfigError, RunnerConfig, settings_path
from octopus.agent.runner import RunResult
from octopus.agent.runner_agent import RunnerAgent, read_state, state_path
from octopus.agent.trust import Allowlist
from octopus.cli import app
from octopus.coordinator import OfferOutcome, OfferSender
from octopus.crypto import decode_public_key, encode_public_key, generate_private_key, node_id
from octopus.policy import JobContext
from octopus.proto import FailureReason, JobStatus, Presence
from octopus.transport import MemoryBus, MemoryTransport

cli = CliRunner()
SECRET = "jit-config-secret"
OK = JobContext(repo="acme/app", head_repo="acme/app", sender_is_org_member=True, event="push")


def pubkey() -> str:
    return encode_public_key(generate_private_key().public_key())


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    for var in ("OCTOPUS_ENTRY_NODES", "OCTOPUS_TCP_PORT", "OCTOPUS_NAT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# config


def test_config_round_trip_and_permissions(tmp_path):
    path = tmp_path / "s.json"
    cfg = RunnerConfig(coordinators=[pubkey()], labels=["gpu"], max_jobs=2)
    cfg.save(path)
    assert RunnerConfig.load(path) == cfg
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_config_missing_file_is_default(tmp_path):
    assert RunnerConfig.load(tmp_path / "none.json") == RunnerConfig()


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"max_jobs": 0}', '{"bogus": 1}', '{"coordinators": ["x"]}']
)
def test_config_invalid_is_rejected(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(ConfigError):
        RunnerConfig.load(path)


def test_config_precedence_flags_over_env_over_file():
    saved = RunnerConfig(entry_nodes=["/file"], tcp_port=1111, max_jobs=3)
    env = {"OCTOPUS_ENTRY_NODES": "/env1,/env2", "OCTOPUS_TCP_PORT": "2222"}
    assert saved.merged(env).entry_nodes == ["/env1", "/env2"]
    assert saved.merged(env).tcp_port == 2222
    assert saved.merged(env, entry_nodes=["/flag"]).entry_nodes == ["/flag"]
    assert saved.merged({}).entry_nodes == ["/file"]
    assert saved.merged({}, max_jobs=1).max_jobs == 1


def test_config_bad_port_env():
    with pytest.raises(ConfigError):
        RunnerConfig().merged({"OCTOPUS_TCP_PORT": "abc"})


def test_settings_path_is_next_to_allowlist(tmp_path):
    assert settings_path(tmp_path / "runner.json") == tmp_path / "runner.settings.json"


# agent


class FakeManager:
    def __init__(
        self,
        gate: threading.Event | None = None,
        result: RunResult | None = None,
        output: list[str] | None = None,
    ):
        self.gate = gate
        self.output = output or []
        self.result = result or RunResult(0, None, "", 0.1)
        self.jobs: list[tuple[str, str]] = []
        self.stopped = False

    def run_job(self, job_id, jit_config, on_output=None):
        self.jobs.append((job_id, jit_config))
        for line in self.output:
            if on_output is not None:
                on_output(line)
        if self.gate is not None:
            self.gate.wait(5)
        return self.result

    def stop(self):
        self.stopped = True
        if self.gate is not None:
            self.gate.set()


class World:
    def __init__(
        self, tmp_path, *, max_jobs=1, allow=("acme/app",), manager=None, log_drain_s=0.0
    ):
        self.bus = MemoryBus()
        self.coord_key = generate_private_key()
        self.statuses: list[JobStatus] = []
        self.sender = OfferSender(
            MemoryTransport(self.bus), self.coord_key, on_status=self.statuses.append
        )
        self.managers: list[FakeManager] = []
        self.manager = manager

        def factory():
            m = self.manager or FakeManager()
            self.managers.append(m)
            return m

        allowlist = Allowlist()
        for target in allow:
            allowlist.add(target)
        self.config = RunnerConfig(coordinators=[self.sender.public_key], max_jobs=max_jobs)
        self.agent = RunnerAgent(
            self.config,
            allowlist,
            MemoryTransport(self.bus),
            data_dir=tmp_path / "data",
            manager_factory=factory,
            log_drain_s=log_drain_s,  # no collector here unless a test adds one
        )
        self.agent.start()
        key = self.agent._receiver._key
        self.presence = Presence(
            runner_id=self.agent.runner_id,
            os="linux",
            arch="x64",
            sent_at=0,
            pubkey=encode_public_key(key.public_key()),
        )

    def offer(self, job_id="j1", repo="acme/app", context=OK, ttl_s=2):
        return self.sender.send_offer(
            self.presence, job_id, repo, SecretStr(SECRET), ttl_s=ttl_s, context=context
        )


def wait_for(cond, timeout=3):
    import time

    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_agent_runs_allowed_job_and_reports_status(tmp_path):
    w = World(tmp_path)
    assert w.offer().outcome is OfferOutcome.ACCEPTED
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses))
    assert w.managers[0].jobs == [("j1", SECRET)]
    assert [s.state for s in w.statuses] == ["runner-started", "finished"]
    assert w.statuses[-1].exit_code == 0 and w.statuses[-1].reason is None
    assert wait_for(lambda: w.agent.busy == 0)
    w.agent.stop()


def test_agent_reports_failure_reason(tmp_path):
    result = RunResult(1, FailureReason.JOB_FAILED, "step failed", 0.1)
    w = World(tmp_path, manager=FakeManager(result=result))
    w.offer()
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses))
    assert w.statuses[-1].reason is FailureReason.JOB_FAILED
    w.agent.stop()


def test_agent_rejects_repo_not_allowlisted(tmp_path):
    w = World(tmp_path, allow=("other/repo",))
    result = w.offer()
    assert result.outcome is OfferOutcome.REJECTED
    assert result.reason is FailureReason.POLICY_REJECTED
    assert w.managers == []
    w.agent.stop()


def test_agent_rejects_fork_even_if_repo_allowed(tmp_path):
    w = World(tmp_path)
    fork = JobContext(
        repo="acme/app", head_repo="evil/app", sender_is_org_member=True, event="pull_request"
    )
    assert w.offer(context=fork).outcome is OfferOutcome.REJECTED
    assert w.managers == []
    w.agent.stop()


def test_agent_respects_max_jobs(tmp_path):
    gate = threading.Event()
    w = World(tmp_path, max_jobs=1, manager=FakeManager(gate=gate))
    assert w.offer("a").outcome is OfferOutcome.ACCEPTED
    assert w.agent.busy == 1
    state = read_state(tmp_path / "data")
    assert state and state["busy"] == 1 and state["pid"] == os.getpid()
    busy = w.offer("b")
    assert busy.outcome is OfferOutcome.REJECTED
    assert busy.reason is FailureReason.NO_IDLE_RUNNER
    gate.set()
    assert wait_for(lambda: w.agent.busy == 0)
    assert w.offer("c").outcome is OfferOutcome.ACCEPTED
    w.agent.stop()


def test_agent_stop_stops_running_job_and_clears_state(tmp_path):
    gate = threading.Event()
    manager = FakeManager(gate=gate)
    w = World(tmp_path, manager=manager)
    w.offer()
    w.agent.stop()
    assert manager.stopped
    assert w.agent._stopped.is_set()
    assert not state_path(tmp_path / "data").exists()


def test_agent_never_publishes_jit_config(tmp_path):
    w = World(tmp_path)
    w.offer()
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses))
    assert all(SECRET.encode() not in frame for frame in w.bus.wire)
    w.agent.stop()


def test_presence_announces_capacity_and_pubkey(tmp_path):
    bus_seen: list[Presence] = []
    w = World(tmp_path, max_jobs=3)
    watcher = MemoryTransport(w.bus)
    from octopus.proto import PRESENCE_TOPIC

    watcher.subscribe(PRESENCE_TOPIC, lambda t, p: bus_seen.append(Presence.model_validate_json(p)))
    w.agent._presence.tick()
    assert bus_seen[-1].capacity == 3
    assert node_id(decode_public_key(bus_seen[-1].pubkey)) == w.agent.runner_id
    w.agent.stop()


# CLI


def test_read_state_ignores_stale_pid(env):
    path = state_path(env / "data")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 2**22 + 12345, "runner_id": "x"}))
    assert read_state(env / "data") is None


def test_status_and_stop_when_not_running(env):
    assert cli.invoke(app, ["runner", "status"]).exit_code == 5
    assert cli.invoke(app, ["runner", "stop"]).exit_code == 5


def test_setup_saves_and_merges(env):
    cfg = env / "runner.json"
    key = pubkey()
    r = cli.invoke(
        app, ["runner", "setup", "--config", str(cfg), "--coordinator", key, "--max-jobs", "2"]
    )
    assert r.exit_code == 0, r.output
    saved = RunnerConfig.load(settings_path(cfg))
    assert saved.coordinators == [key] and saved.max_jobs == 2
    r = cli.invoke(app, ["runner", "setup", "--config", str(cfg), "--labels", "gpu"])
    assert RunnerConfig.load(settings_path(cfg)).coordinators == [key]
    assert RunnerConfig.load(settings_path(cfg)).labels == ["gpu"]


def test_setup_rejects_bad_coordinator_key(env):
    r = cli.invoke(
        app, ["runner", "setup", "--config", str(env / "r.json"), "--coordinator", "nope"]
    )
    assert r.exit_code == 3
    assert "coordinator" in r.output


def test_start_requires_trusted_coordinator(env):
    r = cli.invoke(app, ["runner", "start", "--config", str(env / "r.json")])
    assert r.exit_code == 3
    assert "octopus runner setup --coordinator" in r.output


def test_start_refuses_empty_allowlist(env):
    cfg = env / "r.json"
    cli.invoke(app, ["runner", "setup", "--config", str(cfg), "--coordinator", pubkey()])
    r = cli.invoke(app, ["runner", "start", "--config", str(cfg)])
    assert r.exit_code == 3
    assert "octopus runner allow" in r.output


def test_start_reports_missing_library(env, monkeypatch):
    from octopus.transport import logos

    cfg = env / "r.json"
    cli.invoke(app, ["runner", "setup", "--config", str(cfg), "--coordinator", pubkey()])
    cli.invoke(app, ["runner", "allow", "acme/app", "--config", str(cfg)])
    monkeypatch.setenv("OCTOPUS_BINDINGS", str(env / "missing"))
    monkeypatch.setattr(
        logos, "_load_bindings", lambda: (_ for _ in ()).throw(logos.LogosLibraryError("no lib"))
    )
    r = cli.invoke(app, ["runner", "start", "--config", str(cfg)])
    assert r.exit_code == 4
    assert "liblogosdelivery" in r.output


# job logs


def watch_logs(w, job_id="j1"):
    """What the scheduler does before an offer: the coordinator listens for the job's log."""
    from octopus.coordinator import LogCollector, LogStore

    store = LogStore()
    collector = LogCollector(MemoryTransport(w.bus), w.coord_key, store)
    collector.watch(job_id, w.presence)
    return store


def log_text(store, job_id="j1"):
    return "".join(c.data for c in store.read(job_id))


def test_agent_streams_job_output_to_the_coordinator(tmp_path):
    output = [
        "2026-10-20 10:00:00Z: Running job: build",
        f"token {SECRET} should never leave",
        "2026-10-20 10:00:09Z: Job build completed with result: Succeeded",
    ]
    w = World(tmp_path, manager=FakeManager(output=output), log_drain_s=5)
    store = watch_logs(w)
    assert w.offer().outcome is OfferOutcome.ACCEPTED
    assert wait_for(lambda: (st := store.state("j1")) is not None and st.complete)
    text = log_text(store)
    assert SECRET not in text
    assert text.splitlines() == [output[0], "token *** should never leave", output[2]]
    assert all(SECRET.encode() not in frame for frame in w.bus.wire)
    w.agent.stop()


def test_runner_markers_become_step_statuses(tmp_path):
    output = ["2026-10-20 10:00:00Z: Running job: build", "compiling"]
    w = World(tmp_path, manager=FakeManager(output=output), log_drain_s=5)
    watch_logs(w)
    w.offer()
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses))
    assert [(s.state, s.step) for s in w.statuses] == [
        ("runner-started", None),
        ("step", "build"),
        ("finished", None),
    ]
    w.agent.stop()


def test_slot_is_freed_before_the_log_drains(tmp_path):
    w = World(tmp_path, manager=FakeManager(output=["x"]), log_drain_s=2)
    # Nobody collects this log, so the drain runs to its limit.
    w.offer()
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses), timeout=1)
    assert w.agent.busy == 0
    assert any(t.is_alive() for t in w.agent._workers)  # still draining
    w.agent.stop()


def test_job_runs_without_logs_if_the_stream_cannot_open(tmp_path, monkeypatch):
    from octopus.agent import logs

    def broken(self, background=True):
        raise RuntimeError("could not open channel")

    monkeypatch.setattr(logs.LogStreamer, "start", broken)
    w = World(tmp_path, manager=FakeManager(output=["x"]))
    w.offer()
    assert wait_for(lambda: any(s.state == "finished" for s in w.statuses))
    assert w.statuses[-1].reason is None
    w.agent.stop()


def test_leftover_spools_are_removed_on_start(tmp_path):
    spool = tmp_path / "data" / "logs" / "log-old-abc.spool"
    spool.parent.mkdir(parents=True)
    spool.write_text("masked output from a crashed run")
    w = World(tmp_path)
    assert not spool.exists()
    w.agent.stop()
