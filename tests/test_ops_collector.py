import re

import pytest

from octopus.coordinator.jobs import SqliteJobStore, State
from octopus.coordinator.ops import CoordinatorMetrics, webhook_outcome
from octopus.proto import FailureReason, Presence


class FakeRunners:
    def __init__(self, presences=()):
        self.presences = list(presences)

    def online(self):
        return self.presences


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def now(self):
        return self.t


def presence(runner_id, capacity=1, busy=0):
    return Presence(
        runner_id=runner_id, os="linux", arch="x64", capacity=capacity, busy=busy, sent_at=1.0
    )


def sample(text, name, **labels):
    pairs = ",".join(f'{k}="{v}"' for k, v in labels.items())
    pattern = rf"^{re.escape(name)}{re.escape('{' + pairs + '}') if pairs else ''} (\S+)$"
    m = re.search(pattern, text, re.MULTILINE)
    return float(m.group(1)) if m else None


@pytest.fixture
def store():
    s = SqliteJobStore()
    yield s
    s.close()


def test_gauges_come_from_registry_and_store(store):
    runners = FakeRunners([presence("a", capacity=2, busy=1), presence("b")])
    metrics = CoordinatorMetrics(runners, store)
    store.create("j1", "acme/app")
    store.create("j2", "acme/app")
    store.transition("j2", State.ASSIGNED, actor="test")
    out = metrics.render()
    assert sample(out, "octopus_runners_online") == 2
    assert sample(out, "octopus_runner_slots", kind="capacity") == 3
    assert sample(out, "octopus_runner_slots", kind="busy") == 1
    assert sample(out, "octopus_jobs", state="queued") == 1
    assert sample(out, "octopus_jobs", state="assigned") == 1
    assert sample(out, "octopus_jobs", state="running") == 0
    assert 'octopus_coordinator_info{version="' in out


def test_gauges_follow_changes_without_hooks(store):
    runners = FakeRunners()
    metrics = CoordinatorMetrics(runners, store)
    assert sample(metrics.render(), "octopus_runners_online") == 0
    runners.presences.append(presence("a"))
    assert sample(metrics.render(), "octopus_runners_online") == 1


def test_job_hooks_count_transitions_and_failures(store):
    metrics = CoordinatorMetrics(FakeRunners(), store)
    metrics.job_state(State.ASSIGNED)
    metrics.job_state(State.FAILED, FailureReason.OFFER_TIMEOUT)
    metrics.job_state(State.FAILED, FailureReason.OFFER_TIMEOUT)
    metrics.job_state(State.FAILED)
    out = metrics.render()
    assert sample(out, "octopus_job_transitions_total", state="failed") == 3
    assert sample(out, "octopus_job_failures_total", reason="offer-timeout") == 2
    assert sample(out, "octopus_job_failures_total", reason="unknown") == 1
    # every known reason is present from the start, as zero
    assert sample(out, "octopus_job_failures_total", reason="policy-rejected") == 0


def test_offer_latency_and_outcomes(store):
    clock = FakeClock()
    metrics = CoordinatorMetrics(FakeRunners(), store, clock=clock)
    metrics.offer_started("j1")
    clock.t += 2
    metrics.offer_finished("j1", "accepted")
    metrics.offer_finished("never-started", "timeout")
    out = metrics.render()
    assert sample(out, "octopus_offers_total", outcome="accepted") == 1
    assert sample(out, "octopus_offers_total", outcome="timeout") == 1
    assert sample(out, "octopus_offer_latency_seconds_count") == 1
    assert sample(out, "octopus_offer_latency_seconds_sum") == 2
    assert sample(out, "octopus_offer_latency_seconds_bucket", le="2.5") == 1
    assert sample(out, "octopus_offer_latency_seconds_bucket", le="1") == 0


def test_stale_offers_are_forgotten(store):
    clock = FakeClock()
    metrics = CoordinatorMetrics(FakeRunners(), store, clock=clock)
    metrics.offer_started("old")
    clock.t += 7200
    metrics.offer_started("new")
    assert list(metrics._offers) == ["new"]


def test_webhook_counter(store):
    metrics = CoordinatorMetrics(FakeRunners(), store)
    metrics.webhook("workflow_job", "accepted")
    metrics.webhook("", "bad-signature")
    out = metrics.render()
    assert (
        sample(out, "octopus_webhook_events_total", event="workflow_job", outcome="accepted") == 1
    )
    assert (
        sample(out, "octopus_webhook_events_total", event="unknown", outcome="bad-signature") == 1
    )


@pytest.mark.parametrize(
    "status, message, outcome",
    [
        (202, "accepted", "accepted"),
        (202, "ignored action: opened", "ignored"),
        (200, "duplicate delivery", "duplicate"),
        (200, "pong", "ok"),
        (401, "bad signature", "bad-signature"),
        (400, "malformed", "bad-request"),
        (413, "payload too large", "bad-request"),
        (500, "boom", "error"),
    ],
)
def test_webhook_outcome(status, message, outcome):
    assert webhook_outcome(status, message) == outcome
