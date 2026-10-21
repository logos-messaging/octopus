"""The coordinator process end to end: signed webhook in, job run by a RunnerAgent, and what
`octopus jobs`, `explain` and `logs` show. GitHub's API and actions/runner are faked; the
coordinator, scheduler, offers, log channel and runner agent are real, over MemoryTransport.
"""

import json
import re
import threading
import time

import httpx
import pytest
from conftest import octopus_job
from typer.testing import CliRunner

from octopus.agent.config import RunnerConfig
from octopus.agent.presence import PresencePublisher, RunnerInfo
from octopus.agent.runner import RunResult
from octopus.agent.runner_agent import RunnerAgent
from octopus.agent.trust import Allowlist
from octopus.cli import app
from octopus.coordinator import SchedulerConfig
from octopus.coordinator.config import CoordinatorConfig
from octopus.coordinator.inspect import REASON_HINTS, Inspector
from octopus.coordinator.jobs import State
from octopus.coordinator.service import Coordinator, read_state
from octopus.crypto import encode_public_key, generate_private_key, node_id
from octopus.github.token import TokenClient
from octopus.github.webhooks import sign
from octopus.proto import FailureReason
from octopus.transport import MemoryBus, MemoryTransport

SECRET = "webhook-secret-for-tests"
REPO = "acme/app"
RUN_ID = 2202229078
LOG = [
    "2026-10-20 10:00:00Z: Running job: build",
    "compiling...",
    "2026-10-20 10:00:30Z: Job build completed with result: Succeeded",
]


class FakeGitHub:
    """The API calls a token-mode coordinator makes."""

    def __init__(self):
        self.head = REPO
        self.event = "pull_request"
        self.permission = "write"
        self.runs_status = 200
        self.jit_status = 201
        self.registered = 0
        self.runner_names = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/actions/runs/{RUN_ID}":
            if self.runs_status != 200:
                return httpx.Response(self.runs_status, json={"message": "Server Error"})
            return httpx.Response(
                200, json={"event": self.event, "head_repository": {"full_name": self.head}}
            )
        if re.fullmatch(rf"/repos/{REPO}/collaborators/[^/]+/permission", path):
            return httpx.Response(200, json={"permission": self.permission})
        if path == f"/repos/{REPO}/actions/runners/generate-jitconfig":
            if self.jit_status != 201:
                return httpx.Response(self.jit_status, json={"message": "Resource not accessible"})
            self.registered += 1
            self.runner_names.append(json.loads(request.content)["name"])
            return httpx.Response(
                201,
                json={"runner": {"id": self.registered}, "encoded_jit_config": "JIT-CONFIG-xyz"},
            )
        if path == f"/repos/{REPO}/actions/runners":
            return httpx.Response(200, json={"total_count": 0, "runners": []})
        return httpx.Response(404, json={"message": f"fake: no route {path}"})


class FakeRunner:
    def __init__(self, result=None, lines=LOG):
        self.result = result or RunResult(0, None, "", 0.1)
        self.lines = lines

    def run_job(self, job_id, jit_config, on_output=None):
        for line in self.lines:
            on_output(line)
        return self.result

    def stop(self):
        pass


class System:
    def __init__(self, tmp_path, *, runner_result=None, start_runner=True, **scheduler):
        scheduler.setdefault("tick_s", 0.02)
        scheduler.setdefault("offer_ttl_s", 2.0)
        self.tmp_path = tmp_path
        self.bus = MemoryBus()
        self.github = FakeGitHub()
        config = CoordinatorConfig.from_env(
            {
                "OCTOPUS_WEBHOOK_SECRET": SECRET,
                "OCTOPUS_GITHUB_TOKEN": "github_pat_FAKE",
                "OCTOPUS_GITHUB_REPO": REPO,
                "OCTOPUS_PUBLIC_IP": "203.0.113.7",
                "OCTOPUS_WEBHOOK_LISTEN": "127.0.0.1:0",
            }
        )
        client = TokenClient(
            "github_pat_FAKE",
            client=httpx.Client(transport=httpx.MockTransport(self.github.handler)),
        )
        self.dir = tmp_path / "data" / "coordinator"
        self.coordinator = Coordinator(
            config,
            MemoryTransport(self.bus),
            client,
            self.dir,
            scheduler_config=SchedulerConfig(**scheduler),
            retry_delay_s=0.0,
        )
        self.coordinator.start()
        self.agent = None
        if start_runner:
            allowlist = Allowlist()
            allowlist.add(REPO)
            self.agent = RunnerAgent(
                RunnerConfig(coordinators=[self.coordinator.public_key]),
                allowlist,
                MemoryTransport(self.bus),
                data_dir=tmp_path / "runner",
                manager_factory=lambda: FakeRunner(runner_result),
                log_drain_s=5,
            )
            self.agent.start()
            assert wait_for(lambda: len(self.coordinator.registry.online()) == 1)

    def deliver(self, job_id=1001, action="queued", delivery=None, **fields):
        payload = octopus_job()
        payload["action"] = action
        payload["workflow_job"].update(id=job_id, run_id=RUN_ID, **fields)
        payload["repository"]["full_name"] = REPO
        payload["sender"] = {"login": "alice", "type": "User"}
        body = json.dumps(payload).encode()
        host, port = self.coordinator.webhook_address
        return httpx.post(
            f"http://{host}:{port}/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": delivery or f"d-{job_id}-{action}",
                "X-Hub-Signature-256": sign(SECRET.encode(), body),
            },
        )

    def job(self, job_id="1001"):
        return self.coordinator.jobs.get(job_id)

    def wait_state(self, state, job_id="1001", timeout=8):
        ok = wait_for(lambda: (j := self.job(job_id)) is not None and j.state is state, timeout)
        assert ok, f"job {job_id}: {self.job(job_id)}"

    def stop(self):
        if self.agent is not None:
            self.agent.stop()
        self.coordinator.stop()


def wait_for(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def system(tmp_path):
    systems = []

    def make(**kw):
        s = System(tmp_path / f"s{len(systems)}", **kw)
        systems.append(s)
        return s

    yield make
    for s in systems:
        s.stop()


def cli(*args):
    return CliRunner().invoke(app, list(args))


# --- the happy path ---------------------------------------------------------------------


def test_queued_webhook_to_finished_job_with_log(system):
    s = system()
    assert s.deliver().status_code == 202
    s.wait_state(State.FINISHED)
    assert wait_for(
        lambda: (
            s.coordinator.logs.state("1001") is not None
            and s.coordinator.logs.state("1001").complete
        )
    )
    states = [e.state for e in s.coordinator.jobs.timeline("1001")]
    assert states == [
        State.QUEUED,
        State.ASSIGNED,
        State.OFFERED,
        State.ACKED,
        State.RUNNER_STARTED,
        State.RUNNING,
        State.FINISHED,
    ]
    assert s.job().step == "build" and s.job().runner_id == s.agent.runner_id
    assert s.github.registered == 1


def test_cli_inspects_a_running_coordinator(system):
    s = system()
    s.deliver()
    s.wait_state(State.FINISHED)
    assert wait_for(lambda: (st := s.coordinator.logs.state("1001")) is not None and st.complete)
    data_dir = str(s.tmp_path / "data")

    rows = json.loads(cli("jobs", "--json", "--data-dir", data_dir).output)
    assert [(r["id"], r["state"], r["step"]) for r in rows] == [("1001", "finished", "build")]
    table = cli("jobs", "--data-dir", data_dir).output
    assert "1001" in table and "finished" in table and "acme/app" in table

    result = cli("logs", "1001", "--data-dir", data_dir)
    assert result.exit_code == 0 and result.output.splitlines() == LOG
    followed = cli("logs", "1001", "-f", "--data-dir", data_dir)
    assert followed.exit_code == 0 and followed.output.splitlines() == LOG
    chunks = [
        json.loads(line)
        for line in cli("logs", "1001", "--json", "--data-dir", data_dir).output.splitlines()
    ]
    assert chunks[-1]["final"] and "".join(c["data"] for c in chunks).splitlines() == LOG

    text = cli("explain", "1001", "--data-dir", data_dir).output
    assert "state    finished" in text and "step: build" in text
    assert "log      runner" in text and "complete" in text and "| compiling..." in text

    s.coordinator.maintain()  # what the maintenance thread does every few seconds
    status = json.loads(cli("coordinator", "status", "--json", "--data-dir", data_dir).output)
    assert status["running"] and status["state"]["public_key"] == s.coordinator.public_key
    assert status["state"]["runners_online"] == 1 and status["jobs"] == {"finished": 1}
    plain = cli("coordinator", "status", "--data-dir", data_dir)
    assert plain.exit_code == 0 and f"--coordinator {s.coordinator.public_key}" in plain.output


def test_job_that_ran_on_another_jobs_runner_keeps_its_own_records(system):
    """GitHub gives a job to any idle runner, not the one made for it (issue #58)."""
    s = system()
    s.deliver()
    s.wait_state(State.FINISHED)
    assert wait_for(lambda: (st := s.coordinator.logs.state("1001")) is not None and st.complete)
    made_for_1001 = s.github.runner_names[0]
    data_dir = str(s.tmp_path / "data")

    # A runner that was made for no job of ours, or for the job itself, changes nothing.
    assert s.deliver(1001, "in_progress", runner_name=made_for_1001).status_code == 202
    assert s.deliver(3003, "in_progress", runner_name="GitHub Actions 5").status_code == 202
    assert s.coordinator.jobs.resolve("1001") == "1001"
    assert s.coordinator.jobs.resolve("3003") == "3003"

    # GitHub job 2002 ran on the runner that Octopus made for job 1001.
    assert s.deliver(2002, "in_progress", runner_name=made_for_1001).status_code == 202
    assert wait_for(lambda: s.coordinator.jobs.resolve("2002") == "1001")

    inspector = Inspector(s.tmp_path / "data" / "coordinator")
    try:
        data = inspector.explain("2002")
        assert data["id"] == "2002" and data["state"] == "finished" and data["step"] == "build"
        assert [r["id"] for r in inspector.list_jobs()] == ["2002"]
        assert "".join(c.data for c in inspector.log_chunks("2002")).splitlines() == LOG
    finally:
        inspector.close()
    assert cli("logs", "2002", "--data-dir", data_dir).output.splitlines() == LOG
    assert "state    finished" in cli("explain", "2002", "--data-dir", data_dir).output


def test_duplicate_and_unsigned_webhooks(system):
    s = system()
    assert s.deliver(delivery="same").status_code == 202
    assert s.deliver(delivery="same").status_code == 200  # a GitHub redelivery
    s.wait_state(State.FINISHED)
    assert s.github.registered == 1
    host, port = s.coordinator.webhook_address
    forged = httpx.post(
        f"http://{host}:{port}/github/webhook",
        content=b"{}",
        headers={
            "X-GitHub-Event": "workflow_job",
            "X-GitHub-Delivery": "x",
            "X-Hub-Signature-256": "sha256=00",
        },
    )
    assert forged.status_code == 401


def test_stop_removes_the_state_file(system):
    s = system()
    assert read_state(s.dir) is not None
    s.stop()
    assert read_state(s.dir) is None and not (s.dir / "coordinator.state.json").exists()


# --- explain shows the right reason for every failure -----------------------------------


def explain(s, job_id="1001"):
    inspector = Inspector(s.dir)
    try:
        return inspector.explain(job_id)
    finally:
        inspector.close()


def assert_explained(s, reason, detail_part, job_id="1001"):
    s.wait_state(State.FAILED, job_id)
    data = explain(s, job_id)
    assert data["reason"] == reason.value
    assert data["failure"]["reason"] == reason.value
    assert detail_part in data["failure"]["detail"], data["failure"]
    assert data["failure"]["hint"] == REASON_HINTS[reason]
    text = cli("explain", job_id, "--data-dir", str(s.tmp_path / "data")).output
    assert f"failed ({reason.value})" in text and f"why      {reason.value}:" in text
    return data


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        (FailureReason.JOB_FAILED, "runner exited with code: 1"),
        (FailureReason.RUNNER_DOWNLOAD_FAILED, "download"),
        (FailureReason.RUNNER_START_FAILED, "could not start runner"),
    ],
)
def test_runner_side_failures(system, reason, detail):
    s = system(runner_result=RunResult(1, reason, detail, 0.1))
    s.deliver()
    assert_explained(s, reason, detail)  # the runner's detail, not just the exit code
    assert wait_for(lambda: (st := s.coordinator.logs.state("1001")) is not None and st.complete)
    assert explain(s)["log"]["tail"] == LOG  # the log of the failed job is kept


def test_fork_pr_is_policy_rejected(system):
    s = system()
    s.github.head = "mallory/app"
    s.deliver()
    assert_explained(s, FailureReason.POLICY_REJECTED, "fork PR rejected: mallory/app")
    assert s.github.registered == 0


def test_read_only_sender_is_policy_rejected(system):
    s = system()
    s.github.permission = "read"
    s.deliver()
    assert_explained(s, FailureReason.POLICY_REJECTED, "sender is not trusted")


def test_unsafe_trigger_is_policy_rejected(system):
    s = system()
    s.github.event = "pull_request_target"
    s.deliver()
    assert_explained(s, FailureReason.POLICY_REJECTED, "event not allowed: pull_request_target")


def test_trust_lookup_failure_fails_closed(system):
    s = system()
    s.github.runs_status = 500
    s.deliver()
    data = assert_explained(s, FailureReason.POLICY_REJECTED, "could not check trust")
    assert "500" in data["failure"]["detail"]


def test_jit_config_error(system):
    s = system(max_attempts=1)
    s.github.jit_status = 403
    s.deliver()
    assert_explained(s, FailureReason.JITCONFIG_ERROR, "could not create JIT config")


def test_no_idle_runner(system):
    s = system(start_runner=False, queue_timeout_s=0.3)
    s.deliver()
    data = assert_explained(s, FailureReason.NO_IDLE_RUNNER, "queue timeout")
    assert data["log"] is None


def test_offer_timeout(system):
    s = system(start_runner=False, offer_ttl_s=0.2, max_attempts=1)
    # A runner that announces itself but never answers offers.
    key = generate_private_key()
    PresencePublisher(
        MemoryTransport(s.bus),
        RunnerInfo(
            runner_id=node_id(key.public_key()),
            os="linux",
            arch="x64",
            pubkey=encode_public_key(key.public_key()),
        ),
    ).start()
    assert wait_for(lambda: len(s.coordinator.registry.online()) == 1)
    s.deliver()
    assert_explained(s, FailureReason.OFFER_TIMEOUT, "offer timeout")


def test_cancelled_on_github_while_queued(system):
    s = system(start_runner=False, queue_timeout_s=60)
    s.deliver()
    s.wait_state(State.QUEUED)
    assert s.deliver(action="completed", conclusion="cancelled").status_code == 202
    assert_explained(s, FailureReason.CANCELLED, "cancelled")


def test_runner_lost(tmp_path):
    """Runner loss needs presence timeouts, so it runs on the scheduler with a fake clock."""
    from test_scheduler import Env

    from octopus.coordinator.jobs import SqliteJobStore

    env = Env(max_attempts=1)
    directory = tmp_path / "coordinator"
    directory.mkdir()
    env.store = SqliteJobStore(str(directory / "jobs.db"), clock=env.clock.now)
    env.scheduler._store = env.store
    env.registry.add("r1")
    env.scheduler.submit("1001", REPO)
    env.scheduler.tick()
    env.registry.remove("r1")
    env.scheduler.tick()
    inspector = Inspector(directory)
    data = inspector.explain("1001")
    inspector.close()
    assert data["reason"] == "runner-lost"
    assert "runner lost: r1" in data["failure"]["detail"]
    assert data["failure"]["hint"] == REASON_HINTS[FailureReason.RUNNER_LOST]


def test_every_reason_has_a_hint():
    assert set(REASON_HINTS) == set(FailureReason)


def test_secrets_stay_out_of_state_and_output(system):
    s = system()
    s.deliver()
    s.wait_state(State.FINISHED)
    state_text = (s.dir / "coordinator.state.json").read_text()
    for secret in (SECRET, "github_pat_FAKE", "JIT-CONFIG-xyz"):
        assert secret not in state_text
    out = cli("explain", "1001", "--json", "--data-dir", str(s.tmp_path / "data")).output
    assert "JIT-CONFIG-xyz" not in out and SECRET not in out
    assert all(b"JIT-CONFIG-xyz" not in frame for frame in s.bus.wire)
    assert threading.active_count() < 100
