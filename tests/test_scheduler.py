import random
from concurrent.futures import Future

import pytest
from pydantic import SecretStr

from octopus.coordinator import (
    JitConfigError,
    OfferOutcome,
    OfferResult,
    Scheduler,
    SchedulerConfig,
)
from octopus.coordinator.jobs import SqliteJobStore, State
from octopus.policy import JobContext, TrustPolicy
from octopus.proto import FailureReason, JobStatus, Presence

ACCEPT = OfferResult(OfferOutcome.ACCEPTED)
TIMEOUT = OfferResult(OfferOutcome.TIMEOUT)


def reject(reason=FailureReason.POLICY_REJECTED):
    return OfferResult(OfferOutcome.REJECTED, reason)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class InlineExecutor:
    def submit(self, fn, *args):
        fut: Future = Future()
        fut.set_result(fn(*args))
        return fut


class FakeRegistry:
    def __init__(self) -> None:
        self.runners: dict[str, Presence] = {}

    def add(self, runner_id, os="linux", arch="x64", labels=(), capacity=1, busy=0):
        self.runners[runner_id] = Presence(
            runner_id=runner_id, os=os, arch=arch, labels=list(labels),
            capacity=capacity, busy=busy, sent_at=0,
        )

    def remove(self, runner_id):
        self.runners.pop(runner_id, None)

    def online(self):
        return list(self.runners.values())


class FakeOffers:
    """Answers each send_offer from `script` (a callable or a list), default ACCEPT."""

    def __init__(self, script=None) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []
        self.contexts: dict = {}

    def send_offer(self, runner, job_id, repo, jit_config, labels=None, ttl_s=30, context=None):
        self.calls.append((job_id, runner.runner_id))
        self.contexts[job_id] = context
        if callable(self.script):
            return self.script(job_id, runner)
        if isinstance(self.script, list) and self.script:
            return self.script.pop(0)
        return ACCEPT


class Env:
    def __init__(self, offers=None, jit=None, policy=None, **config) -> None:
        self.clock = FakeClock()
        self.registry = FakeRegistry()
        self.store = SqliteJobStore(":memory:", clock=self.clock.now)
        self.offers = offers or FakeOffers()
        self.jit_calls: list[str] = []

        def default_jit(job, runner):
            self.jit_calls.append(job.job_id)
            return SecretStr("jit")

        self.scheduler = Scheduler(
            self.registry, self.store, self.offers, jit or default_jit, self.clock,
            SchedulerConfig(**config), InlineExecutor(), policy,
        )

    def state(self, job_id):
        return self.store.get(job_id).state

    def states(self, job_id):
        return [e.state for e in self.store.timeline(job_id)]


def test_happy_path_timeline():
    env = Env()
    env.registry.add("r1")
    assert env.scheduler.submit("j1", "acme/app", ["self-hosted", "octopus"])
    env.scheduler.tick()
    assert env.states("j1") == [State.QUEUED, State.ASSIGNED, State.OFFERED, State.ACKED]
    assert env.store.get("j1").runner_id == "r1"
    assert env.offers.calls == [("j1", "r1")]


def test_duplicate_submit_ignored():
    env = Env()
    env.registry.add("r1")
    assert env.scheduler.submit("j1", "acme/app")
    assert not env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.scheduler.tick()
    assert env.offers.calls == [("j1", "r1")]


def test_matches_os_arch_and_labels():
    env = Env()
    env.registry.add("lin", os="linux", arch="x64")
    env.registry.add("mac", os="macos", arch="arm64", labels=["gpu"])
    env.scheduler.submit("j1", "acme/app", ["self-hosted", "octopus", "macOS", "arm64", "gpu"])
    env.scheduler.tick()
    assert env.store.get("j1").runner_id == "mac"


def test_no_match_stays_queued():
    env = Env()
    env.registry.add("lin", os="linux")
    env.scheduler.submit("j1", "acme/app", ["windows"])
    env.scheduler.tick()
    assert env.state("j1") is State.QUEUED
    assert env.offers.calls == []


def test_least_recently_used_runner_first():
    env = Env()
    env.registry.add("a", capacity=1)
    env.registry.add("b", capacity=1)
    picked = []
    for i in range(4):
        env.scheduler.submit(f"j{i}", "acme/app")
        env.scheduler.tick()
        env.clock.advance(1)
        picked.append(env.store.get(f"j{i}").runner_id)
        env.scheduler.on_status(JobStatus(job_id=f"j{i}", runner_id=picked[-1], state="finished"))
    assert picked[:2] != [picked[0], picked[0]]
    assert picked[0] == picked[2] and picked[1] == picked[3]


def test_capacity_limit_then_free_slot():
    env = Env()
    env.registry.add("r1", capacity=1)
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.submit("j2", "acme/app")
    env.scheduler.tick()
    assert env.state("j1") is State.ACKED
    assert env.state("j2") is State.QUEUED
    env.scheduler.on_status(JobStatus(job_id="j1", runner_id="r1", state="finished"))
    env.scheduler.tick()
    assert env.state("j1") is State.FINISHED
    assert env.state("j2") is State.ACKED


def test_reject_then_next_runner():
    env = Env(offers=FakeOffers([reject(), ACCEPT]))
    env.registry.add("a")
    env.registry.add("b")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    assert env.state("j1") is State.QUEUED
    env.scheduler.tick()
    assert env.state("j1") is State.ACKED
    first, second = (r for _, r in env.offers.calls)
    assert first != second


def test_timeout_then_retry_same_runner_if_only_one():
    env = Env(offers=FakeOffers([TIMEOUT, ACCEPT]))
    env.registry.add("only")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.scheduler.tick()
    assert env.state("j1") is State.ACKED


def test_max_attempts_exhausted_fails_with_last_reason():
    env = Env(offers=FakeOffers(lambda j, r: reject(FailureReason.POLICY_REJECTED)), max_attempts=2)
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.scheduler.tick()
    job = env.store.get("j1")
    assert job.state is State.FAILED and job.reason is FailureReason.POLICY_REJECTED
    assert len(env.offers.calls) == 2


def test_timeout_exhausted_reports_offer_timeout():
    env = Env(offers=FakeOffers(lambda j, r: TIMEOUT), max_attempts=1)
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    assert env.store.get("j1").reason is FailureReason.OFFER_TIMEOUT


def test_jit_error_fails_job_and_frees_runner():
    def jit(job, runner):
        raise JitConfigError("boom")

    env = Env(jit=jit)
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    job = env.store.get("j1")
    assert job.state is State.FAILED and job.reason is FailureReason.JITCONFIG_ERROR
    assert env.offers.calls == []
    env.scheduler._jit = lambda j, r: SecretStr("ok")
    env.scheduler.submit("j2", "acme/app")
    env.scheduler.tick()
    assert env.state("j2") is State.ACKED


def test_queue_timeout_fails_no_idle_runner():
    env = Env(queue_timeout_s=60)
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.clock.advance(59)
    env.scheduler.tick()
    assert env.state("j1") is State.QUEUED
    env.clock.advance(2)
    env.scheduler.tick()
    job = env.store.get("j1")
    assert job.state is State.FAILED and job.reason is FailureReason.NO_IDLE_RUNNER


def test_runner_lost_requeues_then_runner_lost_at_cap():
    env = Env(max_attempts=2)
    env.registry.add("a")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.registry.remove("a")
    env.registry.add("b")
    env.scheduler.tick()  # a lost -> requeued -> offered to b
    assert env.store.get("j1").runner_id == "b" and env.state("j1") is State.ACKED
    env.registry.remove("b")
    env.scheduler.tick()  # second loss hits the cap
    job = env.store.get("j1")
    assert job.state is State.FAILED and job.reason is FailureReason.RUNNER_LOST


def test_status_flow_to_finished_and_release():
    env = Env()
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    s = env.scheduler.on_status
    s(JobStatus(job_id="j1", runner_id="r1", state="runner-started"))
    assert env.state("j1") is State.RUNNER_STARTED
    s(JobStatus(job_id="j1", runner_id="r1", state="step", step="build"))
    assert env.state("j1") is State.RUNNING and env.store.get("j1").step == "build"
    s(JobStatus(job_id="j1", runner_id="r1", state="finished", exit_code=0))
    assert env.state("j1") is State.FINISHED


def test_failed_job_status():
    env = Env()
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    env.scheduler.on_status(JobStatus(job_id="j1", runner_id="r1", state="finished", exit_code=1))
    job = env.store.get("j1")
    assert job.state is State.FAILED and job.reason is FailureReason.JOB_FAILED


def test_status_during_offer_is_applied_after_ack():
    env = Env()
    env.registry.add("r1")

    def offer(job_id, runner):
        env.scheduler.on_status(JobStatus(job_id=job_id, runner_id=runner.runner_id, state="runner-started"))
        return ACCEPT

    env.offers.script = offer
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    assert env.state("j1") is State.RUNNER_STARTED


def test_job_secret_not_logged(caplog):
    env = Env()
    env.registry.add("r1")
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    assert "jit" not in caplog.text


@pytest.mark.parametrize("seed", range(8))
def test_stress_invariants(seed):
    rng = random.Random(seed)
    runner_ids = [f"r{i}" for i in range(4)]
    env = Env(max_attempts=3, queue_timeout_s=40)
    active: dict[str, int] = {}  # runner -> accepted jobs in flight, checked against capacity
    assigned_to: dict[str, list[str]] = {}

    def offer(job_id, runner):
        roll = rng.random()
        if roll < 0.2:
            return TIMEOUT
        if roll < 0.35:
            return reject()
        assigned_to.setdefault(job_id, []).append(runner.runner_id)
        active[runner.runner_id] = active.get(runner.runner_id, 0) + 1
        assert active[runner.runner_id] <= 2, "runner over capacity"
        return ACCEPT

    env.offers.script = offer
    for rid in runner_ids:
        env.registry.add(rid, capacity=2)
    jobs = [f"j{i}" for i in range(30)]
    for step in range(400):
        if step < len(jobs):
            env.scheduler.submit(jobs[step], "acme/app")
        if rng.random() < 0.1:
            rid = rng.choice(runner_ids)
            if rid in env.registry.runners:
                env.registry.remove(rid)
                active[rid] = 0
            else:
                env.registry.add(rid, capacity=2)
        env.scheduler.tick()
        for jid, entry in list(env.scheduler._active.items()):
            if rng.random() < 0.3:
                _, rid = entry
                env.scheduler.on_status(JobStatus(
                    job_id=jid, runner_id=rid, state="finished", exit_code=rng.choice([0, 0, 1])))
                active[rid] = max(0, active.get(rid, 1) - 1)
        env.clock.advance(1)
    for rid in runner_ids:
        env.registry.add(rid, capacity=2)
    for _ in range(400):
        env.scheduler.tick()
        for jid, (_, rid) in list(env.scheduler._active.items()):
            env.scheduler.on_status(JobStatus(job_id=jid, runner_id=rid, state="finished", exit_code=0))
            active[rid] = max(0, active.get(rid, 1) - 1)
        env.clock.advance(1)

    terminal = {State.FINISHED, State.FAILED}
    for jid in jobs:
        job = env.store.get(jid)
        assert job.state in terminal, f"{jid} stuck in {job.state}"
        states = env.states(jid)
        assert sum(1 for s in states if s in terminal) == 1
        if job.state is State.FAILED:
            assert job.reason is not None
    assert not env.scheduler._pending and not env.scheduler._inflight
    assert not env.scheduler._active and not env.scheduler._reserved


def test_with_real_offer_sender_and_status_callback():
    from test_offers import World

    w = World()
    env = Env(offers=w.sender)
    w.sender._on_status = env.scheduler.on_status
    env.registry.runners[w.runner.runner_id] = w.runner
    env.scheduler._config = SchedulerConfig(offer_ttl_s=1)
    env.scheduler.submit("j1", "acme/app")
    env.scheduler.tick()
    assert env.state("j1") is State.ACKED
    env.scheduler._offers._dispatch_status(
        JobStatus(job_id="j1", runner_id=w.runner.runner_id, state="runner-started")
        .model_dump_json().encode()
    )
    assert env.state("j1") is State.RUNNER_STARTED


# trust policy

TRUST = TrustPolicy(allowed_repos=frozenset({"acme/app"}))


def ctx(**kw):
    base = {"repo": "acme/app", "head_repo": "acme/app", "sender_is_org_member": True}
    return JobContext(**{**base, **kw})


def test_trusted_job_is_offered_with_its_context():
    env = Env(policy=TRUST)
    env.registry.add("r1")
    context = ctx(event="pull_request")
    assert env.scheduler.submit("j1", "acme/app", [], context)
    env.scheduler.tick()
    assert env.state("j1") is State.ACKED
    assert env.offers.contexts["j1"] == context


@pytest.mark.parametrize(
    "context, why",
    [
        (ctx(head_repo="stranger/app"), "fork PR rejected"),
        (ctx(sender_is_org_member=False), "not an org member"),
        (ctx(event="pull_request_target"), "event not allowed"),
        (ctx(repo="evil/app", head_repo="evil/app"), "repo not allowed"),
        (None, "no trust context"),
    ],
)
def test_untrusted_job_fails_policy_rejected_and_is_never_offered(context, why):
    env = Env(policy=TRUST)
    env.registry.add("r1")
    assert env.scheduler.submit("j1", "acme/app", [], context)
    env.scheduler.tick()
    job = env.store.get("j1")
    assert job.state is State.FAILED
    assert job.reason is FailureReason.POLICY_REJECTED
    assert why in env.store.timeline("j1")[-1].detail
    assert env.offers.calls == [] and env.jit_calls == []


def test_runner_side_rejection_detail_reaches_the_timeline():
    offers = FakeOffers(
        [OfferResult(OfferOutcome.REJECTED, FailureReason.POLICY_REJECTED, "repo not allowed: acme/app")]
        * 3
    )
    env = Env(offers=offers)
    env.registry.add("r1")
    env.registry.add("r2")
    env.registry.add("r3")
    env.scheduler.submit("j1", "acme/app")
    for _ in range(3):
        env.scheduler.tick()
    details = [e.detail for e in env.store.timeline("j1") if e.detail]
    assert any("repo not allowed: acme/app" in d for d in details)
    assert env.state("j1") is State.FAILED
