"""Coordinator (scheduler, offers, log collector) and RunnerAgent together, in memory.

Only `actions/runner` is faked: everything between a queued job and its stored log is real.
"""

import threading
import time

from pydantic import SecretStr

from octopus.agent.config import RunnerConfig
from octopus.agent.runner import RunResult
from octopus.agent.runner_agent import RunnerAgent
from octopus.agent.trust import Allowlist
from octopus.coordinator import LogCollector, LogStore, OfferSender, Scheduler
from octopus.coordinator.jobs import SqliteJobStore, State
from octopus.crypto import encode_public_key, generate_private_key
from octopus.policy import JobContext, TrustPolicy
from octopus.proto import FailureReason, Presence
from octopus.transport import MemoryBus, MemoryTransport

JIT = "jit-config-for-the-job"
CONTEXT = JobContext(repo="acme/app", head_repo="acme/app", sender_is_org_member=True, event="push")


class FakeRunner:
    """Stands in for actions/runner: prints like Runner.Listener, then exits."""

    def __init__(self, lines, result=None):
        self.lines = lines
        self.result = result or RunResult(0, None, "", 0.1)

    def run_job(self, job_id, jit_config, on_output=None):
        for line in self.lines:
            on_output(line)
        return self.result

    def stop(self):
        pass


class OneRunner:
    def __init__(self):
        self.presence: Presence | None = None

    def online(self):
        return [self.presence] if self.presence else []


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def run_flow(tmp_path, runner: FakeRunner):
    bus = MemoryBus()
    coord_key = generate_private_key()
    jobs = SqliteJobStore()
    logs = LogStore()
    collector = LogCollector(MemoryTransport(bus), coord_key, logs)
    registry = OneRunner()
    scheduler_box: list[Scheduler] = []
    sender = OfferSender(
        MemoryTransport(bus), coord_key, on_status=lambda s: scheduler_box[0].on_status(s)
    )
    scheduler = Scheduler(
        registry,
        jobs,
        sender,
        lambda job, r: SecretStr(JIT),
        policy=TrustPolicy(allowed_repos=frozenset({"acme/app"})),
        logs=collector,
    )
    scheduler_box.append(scheduler)

    allowlist = Allowlist()
    allowlist.add("acme/app")
    agent = RunnerAgent(
        RunnerConfig(coordinators=[sender.public_key]),
        allowlist,
        MemoryTransport(bus),
        data_dir=tmp_path / "data",
        manager_factory=lambda: runner,
        log_drain_s=5,
    )
    agent.start()
    registry.presence = Presence(
        runner_id=agent.runner_id,
        os="linux",
        arch="x64",
        sent_at=0,
        pubkey=encode_public_key(agent._key.public_key()),
    )
    assert scheduler.submit("job-1", "acme/app", ["self-hosted", "octopus"], CONTEXT)
    scheduler.tick()
    return agent, jobs, logs, collector


def test_job_runs_and_its_log_reaches_the_coordinator(tmp_path):
    lines = [
        "2026-10-20 10:00:00Z: Running job: build",
        f"using {JIT}",
        "2026-10-20 10:00:30Z: Job build completed with result: Succeeded",
    ]
    agent, jobs, logs, collector = run_flow(tmp_path, FakeRunner(lines))
    assert wait_for(lambda: jobs.get("job-1").state is State.FINISHED)
    assert wait_for(lambda: (st := logs.state("job-1")) is not None and st.complete)
    states = [e.state for e in jobs.timeline("job-1")]
    assert states == [
        State.QUEUED,
        State.ASSIGNED,
        State.OFFERED,
        State.ACKED,
        State.RUNNER_STARTED,
        State.RUNNING,
        State.FINISHED,
    ]
    assert jobs.get("job-1").step == "build"
    assert logs.tail("job-1", 3) == [lines[0], "using ***", lines[2]]
    assert logs.state("job-1").runner_id == agent.runner_id
    agent.stop()
    collector.stop()


def test_failed_job_keeps_its_log_for_explain(tmp_path):
    lines = ["2026-10-20 10:00:00Z: Running job: test", "assertion failed: 1 != 2"]
    failed = RunResult(1, FailureReason.JOB_FAILED, "runner exited with code: 1", 0.1)
    agent, jobs, logs, collector = run_flow(tmp_path, FakeRunner(lines, failed))
    assert wait_for(lambda: jobs.get("job-1").state is State.FAILED)
    assert jobs.get("job-1").reason is FailureReason.JOB_FAILED
    assert wait_for(lambda: (st := logs.state("job-1")) is not None and st.complete)
    assert logs.tail("job-1", 1) == ["assertion failed: 1 != 2"]
    agent.stop()
    collector.stop()


def test_threads_are_cleaned_up(tmp_path):
    before = threading.active_count()
    agent, _jobs, logs, collector = run_flow(tmp_path, FakeRunner(["x"]))
    assert wait_for(lambda: (st := logs.state("job-1")) is not None and st.complete)
    agent.stop()
    collector.stop()
    assert wait_for(lambda: threading.active_count() <= before + 1)
