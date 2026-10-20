"""Job states, legal transitions and the Job/Event records.

Purpose: define, in one place, the lifecycle every job follows so illegal moves can
be rejected and every part of the coordinator talks about states the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from octopus.proto import FailureReason


class State(str, Enum):
    """Where a job is in its life, from GitHub queueing it to a final outcome.

    Happy path: QUEUED -> ASSIGNED -> OFFERED -> ACKED -> RUNNER_STARTED
    -> RUNNING -> FINISHED. Any non-terminal state can end in FAILED.
    """

    # GitHub sent a `workflow_job` "queued" webhook and the coordinator accepted the
    # job (it passed the trust policy). No runner is chosen yet.
    QUEUED = "queued"
    # The scheduler picked an idle runner that matches the job's labels, OS and arch
    # and reserved it, so no other job can take that slot. Nothing was sent yet.
    ASSIGNED = "assigned"
    # The coordinator requested the JIT runner config from GitHub and sent it,
    # encrypted, to the chosen runner. Waiting for the runner to answer within
    # the offer TTL.
    OFFERED = "offered"
    # The runner accepted the offer (it passed its own trust policy check).
    # It has the JIT config but has not started the GitHub runner process yet.
    ACKED = "acked"
    # The runner started `actions/runner` with the JIT config. It is registered
    # with GitHub and waiting for GitHub to hand it the job.
    RUNNER_STARTED = "runner-started"
    # GitHub dispatched the job to the runner and its steps are executing.
    # `Job.step` holds the current step; each new step is another RUNNING event.
    RUNNING = "running"
    # Terminal, success: the job ran to completion and the runner exited cleanly.
    # A workflow step that fails does not end here: it ends in FAILED with
    # reason `job-failed`.
    FINISHED = "finished"
    # Terminal, failure: the job will not complete. Always carries a FailureReason
    # (no idle runner, offer timeout, JIT config error, policy rejected, runner
    # lost, job failed...) that `octopus explain` shows.
    FAILED = "failed"


TERMINAL = frozenset({State.FINISHED, State.FAILED})

# Every state between QUEUED and a terminal one can return to QUEUED: the scheduler
# re-queues a job when its runner refuses the offer, times out or disappears.
ALLOWED: dict[State, frozenset[State]] = {
    State.QUEUED: frozenset({State.ASSIGNED, State.FAILED}),
    State.ASSIGNED: frozenset({State.OFFERED, State.QUEUED, State.FAILED}),
    State.OFFERED: frozenset({State.ACKED, State.QUEUED, State.FAILED}),
    State.ACKED: frozenset({State.RUNNER_STARTED, State.QUEUED, State.FAILED}),
    State.RUNNER_STARTED: frozenset({State.RUNNING, State.FINISHED, State.QUEUED, State.FAILED}),
    # RUNNING -> RUNNING records the next step.
    State.RUNNING: frozenset({State.RUNNING, State.FINISHED, State.QUEUED, State.FAILED}),
    State.FINISHED: frozenset(),
    State.FAILED: frozenset(),
}


class IllegalTransition(ValueError):
    pass


class UnknownJob(KeyError):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    repo: str
    state: State
    created_at: float
    updated_at: float
    reason: FailureReason | None = None
    runner_id: str | None = None
    step: str | None = None


@dataclass(frozen=True)
class Event:
    job_id: str
    ts: float
    actor: str
    state: State
    detail: str = ""
    reason: FailureReason | None = None
