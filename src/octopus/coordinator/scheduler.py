"""Scheduler: gives each queued job to exactly one idle runner.

Purpose: turn queued GitHub jobs into encrypted offers. It matches a job to a runner
by labels/OS/arch, picks the least recently used candidate, tracks reservations so a
runner never exceeds its capacity and a job is never assigned twice, retries on
reject/timeout/runner loss up to a cap, and records every step (and every failure
reason) in the JobStore so `octopus explain` can show what happened.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import SecretStr

from ..clock import Clock, SystemClock
from ..policy import JobContext, TrustPolicy
from ..proto import FailureReason, JobStatus, Presence
from .jobs import IllegalTransition, JobStore, State, UnknownJob
from .offers import OfferOutcome, OfferResult

log = logging.getLogger(__name__)

ACTOR = "scheduler"

# Labels every Octopus job carries; they say nothing about the runner's capabilities.
_GENERIC_LABELS = frozenset({"self-hosted", "octopus"})
_OS_NAMES = {"linux": "linux", "macos": "macos", "darwin": "macos", "windows": "windows"}
_ARCH_NAMES = {"x64": "x64", "x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}


class JitConfigError(Exception):
    """The JIT runner config could not be created; the job fails with JITCONFIG_ERROR."""


class RunnerSource(Protocol):
    """What the scheduler needs from the presence registry."""

    def online(self) -> list[Presence]: ...


class OfferSink(Protocol):
    """What the scheduler needs from OfferSender."""

    def send_offer(
        self,
        runner: Presence,
        job_id: str,
        repo: str,
        jit_config: SecretStr,
        labels: list[str] | None = None,
        ttl_s: float = 30,
        context: JobContext | None = None,
    ) -> OfferResult: ...


JitProvider = Callable[["PendingJob", Presence], SecretStr]


class LogSink(Protocol):
    """What the scheduler needs from LogCollector: listen for a job's log on a runner."""

    def watch(self, job_id: str, runner: Presence) -> None: ...

    def unwatch(self, job_id: str, runner_id: str) -> None: ...


@dataclass(frozen=True)
class SchedulerConfig:
    queue_timeout_s: float = 300.0
    offer_ttl_s: float = 30.0
    # Offers (or runner losses) a job may burn before it fails.
    max_attempts: int = 3
    tick_s: float = 1.0


@dataclass
class PendingJob:
    job_id: str
    repo: str
    labels: tuple[str, ...]
    enqueued_at: float
    context: JobContext | None = None
    attempt: int = 0
    # Runners that already refused or missed this job: the next try prefers others.
    tried: set[str] = field(default_factory=set)
    last_reason: FailureReason | None = None

    @property
    def os(self) -> str | None:
        return next((_OS_NAMES[la.lower()] for la in self.labels if la.lower() in _OS_NAMES), None)

    @property
    def arch(self) -> str | None:
        return next((_ARCH_NAMES[la.lower()] for la in self.labels if la.lower() in _ARCH_NAMES), None)

    @property
    def required_labels(self) -> set[str]:
        return {
            la
            for la in self.labels
            if la.lower() not in _GENERIC_LABELS
            and la.lower() not in _OS_NAMES
            and la.lower() not in _ARCH_NAMES
        }


class Scheduler:
    def __init__(
        self,
        registry: RunnerSource,
        store: JobStore,
        offers: OfferSink,
        jit_provider: JitProvider,
        clock: Clock | None = None,
        config: SchedulerConfig | None = None,
        executor: Executor | None = None,
        policy: TrustPolicy | None = None,
        logs: LogSink | None = None,
    ) -> None:
        self._policy = policy
        self._logs = logs
        self._registry = registry
        self._store = store
        self._offers = offers
        self._jit = jit_provider
        self._clock = clock or SystemClock()
        self._config = config or SchedulerConfig()
        self._executor = executor
        self._own_executor = executor is None

        self._lock = threading.RLock()
        self._pending: dict[str, PendingJob] = {}  # queued, nobody working on them
        self._inflight: dict[str, PendingJob] = {}  # an offer is being sent
        self._active: dict[str, tuple[PendingJob, str]] = {}  # accepted: job -> runner
        self._reserved: dict[str, set[str]] = {}  # runner -> job ids holding a slot
        self._last_assigned: dict[str, float] = {}
        self._early_status: dict[str, list[JobStatus]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # lifecycle

    def start(self) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="octopus-offer")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="octopus-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._own_executor and self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def _loop(self) -> None:
        while not self._stop.wait(self._config.tick_s):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    # API

    def submit(
        self,
        job_id: str,
        repo: str,
        labels: list[str] | tuple[str, ...] = (),
        context: JobContext | None = None,
    ) -> bool:
        """Queue a job. A duplicate id (e.g. a repeated webhook) is ignored; returns False.

        With a trust policy configured, a job that fails it (or arrives without a context)
        is failed as POLICY_REJECTED right away and never offered to a runner.
        """
        try:
            self._store.create(job_id, repo, actor=ACTOR)
        except ValueError:
            return False
        job = PendingJob(job_id, repo, tuple(labels), self._clock.monotonic(), context)
        if self._policy is not None:
            ok, why = (
                self._policy.check(context)
                if context is not None
                else (False, "no trust context for job")
            )
            if not ok:
                self._fail(job, FailureReason.POLICY_REJECTED, f"trust policy: {why}")
                return True
        with self._lock:
            self._pending[job_id] = job
        return True

    def cancel(self, job_id: str, detail: str) -> bool:
        """Drop a job that is still waiting for a runner and fail it as CANCELLED.

        A job already being offered or running is left alone: GitHub stops it on the runner.
        """
        with self._lock:  # popped under the lock, so tick() can no longer pick it
            job = self._pending.pop(job_id, None)
        if job is None:
            return False
        self._transition(job_id, State.FAILED, detail=detail, reason=FailureReason.CANCELLED)
        return True

    def tick(self) -> None:
        """One scheduling pass: detect lost runners, expire waiting jobs, start offers."""
        self._requeue_lost_runners()
        online = self._registry.online()
        with self._lock:
            queue = sorted(self._pending.values(), key=lambda j: j.enqueued_at)
        for job in queue:
            runner = self._reserve_runner(job, online)
            if runner is not None:
                self._dispatch(job, runner)
            elif self._clock.monotonic() - job.enqueued_at >= self._config.queue_timeout_s:
                self._fail(job, FailureReason.NO_IDLE_RUNNER, "queue timeout: no idle runner")

    def on_status(self, status: JobStatus) -> None:
        """Apply a JobStatus from a runner (runner-started / step / finished)."""
        with self._lock:
            if status.job_id in self._inflight:
                self._early_status.setdefault(status.job_id, []).append(status)
                return
            entry = self._active.get(status.job_id)
        if entry is None:
            return
        self._apply_status(entry[0], entry[1], status)

    # matching and reservation

    def _load(self, runner: Presence) -> int:
        return max(runner.busy, len(self._reserved.get(runner.runner_id, ())))

    def _matches(self, job: PendingJob, runner: Presence) -> bool:
        if job.os is not None and _OS_NAMES.get(runner.os.lower(), runner.os.lower()) != job.os:
            return False
        if job.arch is not None and _ARCH_NAMES.get(runner.arch.lower(), runner.arch.lower()) != job.arch:
            return False
        return job.required_labels <= set(runner.labels)

    def _reserve_runner(self, job: PendingJob, online: list[Presence]) -> Presence | None:
        with self._lock:
            if job.job_id not in self._pending:
                return None
            candidates = [
                r for r in online if self._matches(job, r) and self._load(r) < r.capacity
            ]
            fresh = [r for r in candidates if r.runner_id not in job.tried]
            pool = fresh or candidates
            if not pool:
                return None
            runner = min(pool, key=lambda r: self._last_assigned.get(r.runner_id, float("-inf")))
            self._reserved.setdefault(runner.runner_id, set()).add(job.job_id)
            self._last_assigned[runner.runner_id] = self._clock.monotonic()
            del self._pending[job.job_id]
            self._inflight[job.job_id] = job
            return runner

    def _release(self, runner_id: str, job_id: str) -> None:
        with self._lock:
            slots = self._reserved.get(runner_id)
            if slots is not None:
                slots.discard(job_id)
                if not slots:
                    del self._reserved[runner_id]

    # offer flow

    def _dispatch(self, job: PendingJob, runner: Presence) -> None:
        if self._executor is None:
            self._run_offer(job, runner)
        else:
            self._executor.submit(self._run_offer, job, runner)

    def _run_offer(self, job: PendingJob, runner: Presence) -> None:
        try:
            self._offer(job, runner)
        except Exception:
            log.exception("offer failed unexpectedly for job: %s", job.job_id)
            self._release(runner.runner_id, job.job_id)
            with self._lock:
                self._inflight.pop(job.job_id, None)
            self._fail(job, FailureReason.JITCONFIG_ERROR, "unexpected scheduler error")

    def _offer(self, job: PendingJob, runner: Presence) -> None:
        self._transition(job.job_id, State.ASSIGNED, detail=f"runner: {runner.runner_id}",
                         runner_id=runner.runner_id)
        try:
            jit = self._jit(job, runner)
        except JitConfigError as e:
            self._release(runner.runner_id, job.job_id)
            with self._lock:
                self._inflight.pop(job.job_id, None)
            self._fail(job, FailureReason.JITCONFIG_ERROR, f"could not create JIT config: {e}")
            return
        self._transition(job.job_id, State.OFFERED, detail=f"runner: {runner.runner_id}")
        # Listen before the offer goes out, so the runner's first log chunk finds us.
        self._watch_logs(job, runner)
        result = self._offers.send_offer(
            runner,
            job.job_id,
            job.repo,
            jit,
            list(job.labels),
            self._config.offer_ttl_s,
            job.context,
        )
        if result.outcome is OfferOutcome.ACCEPTED:
            self._accepted(job, runner)
        else:
            self._refused(job, runner, result)

    def _accepted(self, job: PendingJob, runner: Presence) -> None:
        self._transition(job.job_id, State.ACKED, detail=f"runner: {runner.runner_id}")
        with self._lock:
            self._inflight.pop(job.job_id, None)
            self._active[job.job_id] = (job, runner.runner_id)
            early = self._early_status.pop(job.job_id, [])
        for status in early:
            self._apply_status(job, runner.runner_id, status)

    def _refused(self, job: PendingJob, runner: Presence, result: OfferResult) -> None:
        reason = result.reason or FailureReason.OFFER_TIMEOUT
        detail = f"offer {result.outcome.value}: runner {runner.runner_id}"
        if result.detail:
            detail += f": {result.detail}"
        self._unwatch_logs(job.job_id, runner.runner_id)
        self._release(runner.runner_id, job.job_id)
        with self._lock:
            self._inflight.pop(job.job_id, None)
        job.tried.add(runner.runner_id)
        job.last_reason = reason
        self._retry_or_fail(job, reason, detail)

    def _retry_or_fail(self, job: PendingJob, reason: FailureReason, detail: str) -> None:
        job.attempt += 1
        if job.attempt >= self._config.max_attempts:
            self._fail(job, reason, f"{detail}; gave up after {job.attempt} attempts")
            return
        if self._transition(job.job_id, State.QUEUED, detail=f"{detail}; retrying"):
            with self._lock:
                self._pending[job.job_id] = job

    # runner loss and status

    def _requeue_lost_runners(self) -> None:
        online_ids = {r.runner_id for r in self._registry.online()}
        with self._lock:
            lost = [(j, rid) for j, rid in self._active.values() if rid not in online_ids]
            for job, rid in lost:
                del self._active[job.job_id]
        for job, rid in lost:
            self._unwatch_logs(job.job_id, rid)
            self._release(rid, job.job_id)
            job.tried.add(rid)
            self._retry_or_fail(job, FailureReason.RUNNER_LOST, f"runner lost: {rid}")

    def _apply_status(self, job: PendingJob, runner_id: str, status: JobStatus) -> None:
        current = self._store.get(job.job_id)
        if current is None:
            return
        if status.state == "runner-started":
            if current.state is State.ACKED:
                self._transition(job.job_id, State.RUNNER_STARTED, detail="runner started")
            return
        if current.state is State.ACKED:
            self._transition(job.job_id, State.RUNNER_STARTED, detail="runner started")
        if status.state == "step":
            self._transition(job.job_id, State.RUNNING, detail=f"step: {status.step}",
                             step=status.step)
            return
        # finished
        with self._lock:
            self._active.pop(job.job_id, None)
        self._release(runner_id, job.job_id)
        if status.exit_code in (None, 0) and status.reason is None:
            self._transition(job.job_id, State.FINISHED, detail="job finished")
        else:
            self._transition(
                job.job_id,
                State.FAILED,
                detail=f"exit code: {status.exit_code}",
                reason=status.reason or FailureReason.JOB_FAILED,
            )

    # helpers

    def _watch_logs(self, job: PendingJob, runner: Presence) -> None:
        if self._logs is None:
            return
        try:
            self._logs.watch(job.job_id, runner)
        except Exception:  # logs are best-effort; the job still runs
            log.exception("could not watch logs of job %s", job.job_id)

    def _unwatch_logs(self, job_id: str, runner_id: str) -> None:
        if self._logs is None:
            return
        try:
            self._logs.unwatch(job_id, runner_id)
        except Exception:
            log.exception("could not stop watching logs of job %s", job_id)

    def _fail(self, job: PendingJob, reason: FailureReason, detail: str) -> None:
        with self._lock:
            self._pending.pop(job.job_id, None)
        self._transition(job.job_id, State.FAILED, detail=detail, reason=reason)

    def _transition(self, job_id: str, state: State, **kwargs) -> bool:
        try:
            self._store.transition(job_id, state, actor=ACTOR, **kwargs)
        except (IllegalTransition, UnknownJob) as e:
            log.warning("could not move job to state %s: %s", state.value, e)
            return False
        return True
