"""Tests for the job lifecycle store: state machine, failure reasons, filters,
persistence across restarts and retention.
"""

import pytest

from octopus.coordinator.jobs import IllegalTransition, SqliteJobStore, State, UnknownJob
from octopus.proto import FailureReason

HAPPY = [
    State.ASSIGNED,
    State.OFFERED,
    State.ACKED,
    State.RUNNER_STARTED,
    State.RUNNING,
    State.FINISHED,
]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return SqliteJobStore(clock=clock)


def run_happy(store, job_id="j1"):
    store.create(job_id, "acme/app")
    for s in HAPPY:
        store.transition(job_id, s, actor="test", runner_id="r1" if s == State.ASSIGNED else None)


def test_happy_path_and_timeline(store):
    run_happy(store)
    job = store.get("j1")
    assert job.state == State.FINISHED and job.runner_id == "r1"
    events = store.timeline("j1")
    assert [e.state for e in events] == [State.QUEUED, *HAPPY]
    assert all(e.actor for e in events)


def test_running_records_each_step(store):
    store.create("j", "acme/app")
    for s in HAPPY[:4]:
        store.transition("j", s, actor="t")
    store.transition("j", State.RUNNING, actor="runner", step="build")
    job = store.transition("j", State.RUNNING, actor="runner", step="test")
    assert job.step == "test"
    assert [e.state for e in store.timeline("j")].count(State.RUNNING) == 2


@pytest.mark.parametrize("bad", [State.RUNNING, State.FINISHED, State.OFFERED])
def test_illegal_from_queued(store, bad):
    store.create("j", "acme/app")
    with pytest.raises(IllegalTransition):
        store.transition("j", bad, actor="t")
    assert store.get("j").state == State.QUEUED
    assert len(store.timeline("j")) == 1


@pytest.mark.parametrize("terminal", ["finished", "failed"])
def test_terminal_is_final(store, terminal):
    store.create("j", "acme/app")
    if terminal == "finished":
        for s in HAPPY:
            store.transition("j", s, actor="t")
    else:
        store.transition("j", State.FAILED, actor="t", reason=FailureReason.NO_IDLE_RUNNER)
    for s in State:
        with pytest.raises(IllegalTransition):
            store.transition("j", s, actor="t", reason=FailureReason.JOB_FAILED if s == State.FAILED else None)


def test_failed_needs_reason_and_reason_only_on_failed(store):
    store.create("j", "acme/app")
    with pytest.raises(IllegalTransition):
        store.transition("j", State.FAILED, actor="t")
    with pytest.raises(IllegalTransition):
        store.transition("j", State.ASSIGNED, actor="t", reason=FailureReason.JOB_FAILED)


def test_unknown_and_duplicate_job(store):
    with pytest.raises(UnknownJob):
        store.transition("nope", State.ASSIGNED, actor="t")
    store.create("j", "acme/app")
    with pytest.raises(ValueError):
        store.create("j", "acme/app")


@pytest.mark.parametrize("reason", list(FailureReason))
def test_every_failure_reason_round_trips(store, reason):
    store.create("j", "acme/app")
    store.transition("j", State.FAILED, actor="t", reason=reason, detail="why")
    job = store.get("j")
    assert job.state == State.FAILED and job.reason == reason
    last = store.timeline("j")[-1]
    assert last.reason == reason and last.detail == "why"


def test_filters(store, clock):
    run_happy(store, "done")
    store.create("fail", "acme/other")
    store.transition("fail", State.FAILED, actor="t", reason=FailureReason.OFFER_TIMEOUT)
    store.create("queued", "acme/app")
    store.create("live", "acme/app")
    for s in HAPPY[:5]:
        store.transition("live", s, actor="t")
    assert {j.id for j in store.list(failed=True)} == {"fail"}
    assert {j.id for j in store.list(running=True)} == {"live"}
    assert {j.id for j in store.list(repo="acme/other")} == {"fail"}
    assert {j.id for j in store.list(state=State.QUEUED)} == {"queued"}
    assert len(store.list(limit=2)) == 2


def test_persistence_across_restart(tmp_path, clock):
    path = str(tmp_path / "jobs.db")
    s1 = SqliteJobStore(path, clock=clock)
    run_happy(s1)
    s1.create("bad", "acme/app")
    s1.transition("bad", State.FAILED, actor="sched", reason=FailureReason.RUNNER_LOST, detail="gone")
    s1.close()

    s2 = SqliteJobStore(path, clock=clock)
    assert s2.get("j1").state == State.FINISHED
    assert s2.get("bad").reason == FailureReason.RUNNER_LOST
    assert [e.state for e in s2.timeline("j1")] == [State.QUEUED, *HAPPY]
    assert s2.timeline("bad")[-1].detail == "gone"
    s2.close()


def test_retention_purges_old_terminal_jobs_only(tmp_path, clock):
    path = str(tmp_path / "jobs.db")
    s1 = SqliteJobStore(path, clock=clock, retention_s=100)
    run_happy(s1, "old")
    s1.create("active", "acme/app")
    s1.close()

    clock.now += 101
    s2 = SqliteJobStore(path, clock=clock, retention_s=100)
    assert s2.get("old") is None and s2.timeline("old") == []
    assert s2.get("active") is not None
    s2.close()


@pytest.mark.parametrize("state", [State.ASSIGNED, State.OFFERED, State.ACKED])
def test_requeue_edge(store, state):
    store.create("rq", "acme/app")
    path = [State.ASSIGNED, State.OFFERED, State.ACKED]
    for s in path[: path.index(state) + 1]:
        store.transition("rq", s, actor="t")
    store.transition("rq", State.QUEUED, actor="t", detail="retry")
    assert store.get("rq").state == State.QUEUED
