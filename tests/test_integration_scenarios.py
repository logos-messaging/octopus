"""End-to-end CI coordination scenarios on an in-process cluster (see tests/harness).

Every scenario asserts the final job state *and* the failure reason recorded in the job
timeline, which is what `octopus explain` prints.
"""

from collections import Counter

import pytest
from harness import REPO, Behavior, Cluster, LossyTransport, wait_until
from pydantic import SecretStr

from octopus.coordinator import ClusterConfig, OfferOutcome, OfferSender, SchedulerConfig
from octopus.coordinator.jobs import State
from octopus.crypto import generate_private_key
from octopus.proto import FailureReason
from octopus.transport import MemoryTransport


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path)
    yield c
    c.close()


def finished(c, job_id):
    return lambda: c.coordinator.job(job_id).state in (State.FINISHED, State.FAILED)


def test_each_job_runs_on_exactly_one_runner(cluster):
    """3 jobs, 2 runners of capacity 1: the third waits, then runs; nothing runs twice."""
    a = cluster.add_runner("a", Behavior(outcome="hang"))
    b = cluster.add_runner("b", Behavior(outcome="hang"))
    for job_id in (1, 2, 3):
        assert cluster.coordinator.deliver(job_id).status == 202
    cluster.tick()
    wait_until(lambda: len(a.runs) + len(b.runs) == 2, what="two jobs started")
    # No free slot: job 3 is not even offered (not just refused by a busy runner).
    assert cluster.coordinator.states(3) == [State.QUEUED]
    assert len(cluster.coordinator.github.requests) == 2

    a.release()
    b.release()
    wait_until(
        lambda: finished(cluster, 1)() and finished(cluster, 2)(), what="first two done"
    )
    cluster.tick()
    wait_until(finished(cluster, 3), what="third job done")

    ran = Counter(a.runs + b.runs)
    assert ran == {"1": 1, "2": 1, "3": 1}
    assert {cluster.coordinator.job(j).state for j in (1, 2, 3)} == {State.FINISHED}


def test_duplicate_webhook_is_processed_once(cluster):
    node = cluster.add_runner("a")
    assert cluster.coordinator.deliver(7, delivery_id="d-1").message == "accepted"
    assert cluster.coordinator.deliver(7, delivery_id="d-1").message == "duplicate delivery"
    cluster.tick()
    wait_until(finished(cluster, 7), what="job done")
    assert node.runs == ["7"]
    assert len(cluster.coordinator.store.list()) == 1


def test_lost_ack_times_out_and_the_job_moves_to_another_runner(tmp_path):
    cluster = Cluster(tmp_path, scheduler=SchedulerConfig(offer_ttl_s=0.3))
    try:
        deaf = LossyTransport(cluster.bus)
        deaf.mute = True  # receives offers, but its ack and statuses never arrive
        a = cluster.add_runner("deaf", transport=deaf)
        b = cluster.add_runner("ok")
        cluster.coordinator.deliver(1)
        cluster.tick()  # offer to the deaf runner times out, the job is requeued
        cluster.tick()  # ... and goes to the runner that has not failed it yet
        wait_until(finished(cluster, 1), what="job done")
        job = cluster.coordinator.job(1)
        assert job.state is State.FINISHED and job.runner_id == b.runner_id
        timeline = cluster.coordinator.store.timeline("1")
        assert any("timeout" in e.detail for e in timeline), [e.detail for e in timeline]
        assert a.runs in ([], ["1"])  # at-least-once: the deaf runner may have started it
    finally:
        cluster.close()


def test_offer_timeout_gives_up_after_max_attempts(tmp_path):
    cluster = Cluster(tmp_path, scheduler=SchedulerConfig(offer_ttl_s=0.2, max_attempts=2))
    try:
        deaf = LossyTransport(cluster.bus)
        deaf.mute = True
        cluster.add_runner("deaf", transport=deaf)
        cluster.coordinator.deliver(1)
        cluster.tick()
        cluster.tick()
        job = cluster.coordinator.job(1)
        assert job.state is State.FAILED and job.reason is FailureReason.OFFER_TIMEOUT
    finally:
        cluster.close()


def test_runner_vanishing_mid_job_requeues_the_job(cluster):
    a = cluster.add_runner("a", Behavior(outcome="hang"))
    cluster.coordinator.deliver(1)
    cluster.tick()
    wait_until(lambda: a.runs == ["1"], what="job started")

    cluster.clock.advance(3600)  # no heartbeat: the runner is gone
    b = cluster.add_runner("b")
    cluster.tick()
    wait_until(finished(cluster, 1), what="job done on the replacement")
    job = cluster.coordinator.job(1)
    assert job.state is State.FINISHED and job.runner_id == b.runner_id
    assert any(
        e.reason is FailureReason.RUNNER_LOST or "runner lost" in e.detail
        for e in cluster.coordinator.store.timeline("1")
    )


def test_runner_lost_fails_the_job_when_attempts_run_out(tmp_path):
    cluster = Cluster(tmp_path, scheduler=SchedulerConfig(offer_ttl_s=2.0, max_attempts=1))
    try:
        a = cluster.add_runner("a", Behavior(outcome="hang"))
        cluster.coordinator.deliver(1)
        cluster.tick()
        wait_until(lambda: a.runs == ["1"], what="job started")
        cluster.clock.advance(3600)
        cluster.tick()
        job = cluster.coordinator.job(1)
        assert job.state is State.FAILED and job.reason is FailureReason.RUNNER_LOST
    finally:
        cluster.close()


def test_failing_job_records_its_reason_and_timeline(cluster):
    cluster.add_runner("a", Behavior(outcome="fail"))
    cluster.coordinator.deliver(1)
    cluster.tick()
    wait_until(finished(cluster, 1), what="job done")
    job = cluster.coordinator.job(1)
    assert job.state is State.FAILED and job.reason is FailureReason.JOB_FAILED
    assert cluster.coordinator.states(1) == [
        State.QUEUED,
        State.ASSIGNED,
        State.OFFERED,
        State.ACKED,
        State.RUNNER_STARTED,
        State.FAILED,
    ]
    assert cluster.coordinator.store.timeline("1")[-1].detail == "exit code 1"  # the runner's


def test_jit_config_failure_is_reported_and_no_offer_is_sent(cluster):
    node = cluster.add_runner("a")
    cluster.coordinator.github.fail_with = "GitHub said no"
    cluster.coordinator.deliver(1)
    cluster.tick()
    job = cluster.coordinator.job(1)
    assert job.state is State.FAILED and job.reason is FailureReason.JITCONFIG_ERROR
    assert node.runs == []


def test_no_runner_fails_with_no_idle_runner(tmp_path):
    cluster = Cluster(tmp_path, scheduler=SchedulerConfig(queue_timeout_s=60))
    try:
        cluster.coordinator.deliver(1)
        cluster.tick()
        assert cluster.coordinator.job(1).state is State.QUEUED
        cluster.clock.advance(61)
        cluster.tick()
        job = cluster.coordinator.job(1)
        assert job.state is State.FAILED and job.reason is FailureReason.NO_IDLE_RUNNER
    finally:
        cluster.close()


@pytest.mark.parametrize(
    "ctx",
    [
        {"head_repo": "stranger/app"},  # fork PR
        {"sender_is_org_member": False},
        {"event": "pull_request_target"},
    ],
    ids=["fork", "non-member", "unsafe-event"],
)
def test_untrusted_jobs_are_rejected_before_any_offer(cluster, ctx):
    node = cluster.add_runner("a")
    cluster.coordinator.deliver(1, **ctx)
    cluster.tick()
    job = cluster.coordinator.job(1)
    assert job.state is State.FAILED and job.reason is FailureReason.POLICY_REJECTED
    assert node.runs == [] and cluster.coordinator.github.requests == []


def test_runner_enforces_its_own_allowlist(tmp_path):
    """The coordinator is permissive here; the lender's allowlist still says no."""
    cluster = Cluster(
        tmp_path,
        policy=None,
        scheduler=SchedulerConfig(offer_ttl_s=2.0, max_attempts=1),
    )
    cluster.coordinators[0].scheduler._policy = None  # no coordinator-side check
    try:
        node = cluster.add_runner("picky", allow=("other-org/other",))
        cluster.coordinator.deliver(1)
        cluster.tick()
        job = cluster.coordinator.job(1)
        assert job.state is State.FAILED and job.reason is FailureReason.POLICY_REJECTED
        assert node.runs == []
    finally:
        cluster.close()


def test_forged_offer_from_an_untrusted_coordinator_is_rejected(cluster):
    node = cluster.add_runner("a")
    runner = cluster.coordinator.registry.online()[0]
    rogue = OfferSender(MemoryTransport(cluster.bus), generate_private_key(), cluster.clock)
    result = rogue.send_offer(runner, "evil", REPO, SecretStr("stolen"), ttl_s=0.3)
    assert result.outcome is not OfferOutcome.ACCEPTED
    assert node.runs == []


def test_runner_accepts_offers_from_any_pinned_coordinator(tmp_path):
    cluster = Cluster(tmp_path, coordinators=2)
    try:
        node = cluster.add_runner("a")
        job = owned_by(cluster, 1)
        cluster.coordinators[1].deliver(job)  # the first coordinator never ticks
        cluster.tick(1)
        wait_until(finished_on(cluster, 1, job), what="job done via coordinator 2")
        assert node.runs == [str(job)]
    finally:
        cluster.close()


def finished_on(c, idx, job_id):
    return lambda: c.coordinators[idx].job(job_id).state in (State.FINISHED, State.FAILED)


def owned_by(c, idx):
    """A job id that rendezvous hashing assigns to coordinator `idx` while both are alive."""
    want = c.coordinators[idx].sender.coordinator_id
    return next(j for j in range(1, 1000) if c.coordinators[0].cluster.owner(str(j)) == want)


def two_coordinators(tmp_path):
    return Cluster(tmp_path, coordinators=2, cluster=ClusterConfig(dead_after_s=2.0))


def test_coordinator_failover_completes_a_queued_job(tmp_path):
    """The owner queues a job and dies before offering it: the survivor takes it over."""
    cluster = two_coordinators(tmp_path)
    try:
        node = cluster.add_runner("a")
        job = owned_by(cluster, 0)
        cluster.coordinators[0].deliver(job)  # coordinator 1 owns and queues it, then dies
        cluster.clock.advance(3)  # past dead_after_s: coordinator 2 stops seeing it
        cluster.tick(1)
        wait_until(lambda: node.runs == [str(job)], timeout_s=2, what="takeover")
        wait_until(finished_on(cluster, 1, job), what="job done via coordinator 2")
        assert cluster.coordinators[1].job(job).state is State.FINISHED
    finally:
        cluster.close()


def test_job_received_by_a_non_owner_runs_on_the_owner_only(tmp_path):
    cluster = two_coordinators(tmp_path)
    try:
        node = cluster.add_runner("a")
        job = owned_by(cluster, 1)
        cluster.coordinators[0].deliver(job)  # the webhook lands on the non-owner
        cluster.tick(0)
        cluster.tick(1)
        wait_until(finished_on(cluster, 1, job), what="job done on the owner")
        assert node.runs == [str(job)]
        assert cluster.coordinators[0].store.get(str(job)) is None
        assert [len(c.github.requests) for c in cluster.coordinators] == [0, 1]
    finally:
        cluster.close()


def test_live_coordinators_never_both_offer_a_job(tmp_path):
    cluster = two_coordinators(tmp_path)
    try:
        node = cluster.add_runner("a")
        jobs = [owned_by(cluster, 0), owned_by(cluster, 1)]
        for job in jobs:
            cluster.coordinators[0].deliver(job)
        # One after the other: the runner has a single slot, and two coordinators offering at
        # the same moment would have one refused and offered again, which is not what this
        # scenario is about.
        for idx, job in enumerate(jobs):
            cluster.tick(idx)
            wait_until(lambda job=job: str(job) in node.runs, what=f"job {job} started")
            wait_until(
                lambda idx=idx, job=job: cluster.coordinators[idx].job(job).state is State.FINISHED,
                what=f"job {job} done",
            )
        offers = [r for c in cluster.coordinators for r in c.github.requests]
        assert sorted(offers) == sorted(map(str, jobs))
    finally:
        cluster.close()


def test_a_job_a_runner_already_accepted_is_not_taken_over(tmp_path):
    cluster = two_coordinators(tmp_path)
    try:
        node = cluster.add_runner("a", Behavior(outcome="hang"))
        job = owned_by(cluster, 0)
        cluster.coordinators[0].deliver(job)
        cluster.tick(0)
        wait_until(lambda: node.runs == [str(job)], what="job running")
        wait_until(lambda: cluster.coordinators[1].cluster.pending() == [], what="peer forgot it")
        cluster.clock.advance(3)  # the owner dies while the job runs
        cluster.tick(1)
        assert cluster.coordinators[1].store.get(str(job)) is None
        assert node.runs == [str(job)]
    finally:
        cluster.close()


def test_job_log_arrives_complete_and_in_order_over_a_lossy_link(tmp_path):
    cluster = Cluster(tmp_path)
    try:
        lines = tuple(f"line {i:03d}" for i in range(200))
        cluster.add_runner(
            "a", Behavior(log_lines=lines, stream_logs=True, log_transport_drop=0.3)
        )
        cluster.coordinator.deliver(1)
        cluster.tick()
        wait_until(finished(cluster, 1), timeout_s=15, what="job done")
        store = cluster.coordinator.log_store
        wait_until(
            lambda: (s := store.state("1")) is not None and s.complete,
            timeout_s=15,
            what="log complete",
        )
        assert store.gaps("1") == []
        assert store.tail("1", 1000) == list(lines)
    finally:
        cluster.close()
