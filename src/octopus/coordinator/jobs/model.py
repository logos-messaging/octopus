from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from octopus.proto import FailureReason


class State(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    OFFERED = "offered"
    ACKED = "acked"
    RUNNER_STARTED = "runner-started"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


TERMINAL = frozenset({State.FINISHED, State.FAILED})

ALLOWED: dict[State, frozenset[State]] = {
    State.QUEUED: frozenset({State.ASSIGNED, State.FAILED}),
    State.ASSIGNED: frozenset({State.OFFERED, State.FAILED}),
    State.OFFERED: frozenset({State.ACKED, State.FAILED}),
    State.ACKED: frozenset({State.RUNNER_STARTED, State.FAILED}),
    State.RUNNER_STARTED: frozenset({State.RUNNING, State.FINISHED, State.FAILED}),
    # RUNNING -> RUNNING records the next step.
    State.RUNNING: frozenset({State.RUNNING, State.FINISHED, State.FAILED}),
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
