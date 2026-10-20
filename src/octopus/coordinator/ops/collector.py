"""The coordinator's metrics: what to count, and the hooks the coordinator calls.

Gauges are computed at scrape time from the runner registry and the job store, so they cannot
drift. Counters and the offer latency histogram are fed by the `job_state`, `offer_*` and
`webhook` hooks, which the coordinator service calls where those things happen.
"""

from __future__ import annotations

import threading
from typing import Protocol

from ... import __version__
from ...clock import Clock, SystemClock
from ...proto import FailureReason, Presence
from ..jobs import JobStore, State
from ..jobs.model import TERMINAL
from .metrics import Gauge, MetricsRegistry

# Jobs listed per state when counting; far above any realistic queue.
_COUNT_LIMIT = 100_000
# A started offer that never resolves is forgotten after this long (seconds).
_OFFER_TTL_S = 3600.0
_OPEN_STATES = tuple(s for s in State if s not in TERMINAL)

OFFER_LATENCY_BUCKETS = (0.05, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def webhook_outcome(status: int, message: str = "") -> str:
    """The `outcome` label for a webhook answer, so every caller counts them the same way."""
    if status == 401:
        return "bad-signature"
    if status == 200 and message.startswith("duplicate"):
        return "duplicate"
    if status == 202:
        return "ignored" if message.startswith("ignored") else "accepted"
    if status == 200:
        return "ok"
    return "error" if status >= 500 else "bad-request"


class RunnerSource(Protocol):
    def online(self) -> list[Presence]: ...


class CoordinatorMetrics:
    def __init__(
        self,
        runners: RunnerSource,
        store: JobStore,
        *,
        registry: MetricsRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.registry = registry or MetricsRegistry()
        self._runners = runners
        self._store = store
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._offers: dict[str, float] = {}

        r = self.registry
        info = r.gauge("octopus_coordinator_info", "Coordinator build information.", ["version"])
        info.set(1, version=__version__)
        r.gauge("octopus_runners_online", "Runners currently announcing presence.").set_function(
            lambda: float(len(self._runners.online()))
        )
        slots = r.gauge("octopus_runner_slots", "Job slots of the online runners.", ["kind"])
        slots.set_function(self._slots)
        jobs: Gauge = r.gauge("octopus_jobs", "Jobs not yet finished, by state.", ["state"])
        jobs.set_function(self._open_jobs)
        self._transitions = r.counter(
            "octopus_job_transitions_total", "Job state changes since start.", ["state"]
        )
        self._failures = r.counter(
            "octopus_job_failures_total", "Jobs that failed since start, by reason.", ["reason"]
        )
        self._offers_total = r.counter(
            "octopus_offers_total", "Job offers made to runners, by outcome.", ["outcome"]
        )
        self._offer_latency = r.histogram(
            "octopus_offer_latency_seconds",
            "Time from sending a job offer to the runner's answer.",
            buckets=OFFER_LATENCY_BUCKETS,
        )
        self._webhooks = r.counter(
            "octopus_webhook_events_total",
            "GitHub webhook deliveries, by event and outcome.",
            ["event", "outcome"],
        )
        # Initialise label sets so dashboards see zeros instead of missing series.
        for reason in FailureReason:
            self._failures.inc(0, reason=reason.value)

    # scrape-time gauges

    def _slots(self) -> dict[tuple[str, ...], float]:
        online = self._runners.online()
        return {
            ("capacity",): float(sum(p.capacity for p in online)),
            ("busy",): float(sum(p.busy for p in online)),
        }

    def _open_jobs(self) -> dict[tuple[str, ...], float]:
        return {
            (s.value,): float(len(self._store.list(state=s, limit=_COUNT_LIMIT)))
            for s in _OPEN_STATES
        }

    # hooks

    def job_state(self, state: State, reason: FailureReason | None = None) -> None:
        """A job entered `state` (with `reason` when it failed)."""
        self._transitions.inc(state=state.value)
        if state == State.FAILED:
            self._failures.inc(reason=(reason.value if reason else "unknown"))

    def offer_started(self, job_id: str) -> None:
        now = self._clock.monotonic()
        with self._lock:
            self._offers = {j: t for j, t in self._offers.items() if now - t < _OFFER_TTL_S}
            self._offers[job_id] = now

    def offer_finished(self, job_id: str, outcome: str) -> None:
        """The runner answered (or the offer failed). `outcome`: accepted, rejected, timeout..."""
        now = self._clock.monotonic()
        with self._lock:
            started = self._offers.pop(job_id, None)
        self._offers_total.inc(outcome=outcome)
        if started is not None:
            self._offer_latency.observe(now - started)

    def webhook(self, event: str, outcome: str) -> None:
        self._webhooks.inc(event=event or "unknown", outcome=outcome)

    def render(self) -> str:
        return self.registry.render()
