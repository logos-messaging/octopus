"""Picks a free runner for a workflow run and keeps it reserved for that run.

Manual runners announce a unique `octopus-<name>` label in their presence. A reservation
lasts until GitHub reports the job on that label done, or until its TTL. GitHub assigns a job
by label, so a unique label is what makes the choice stick.
"""

from __future__ import annotations

import threading
from collections.abc import Collection
from dataclasses import dataclass

from ..clock import Clock, SystemClock
from ..github.webhooks import WorkflowJobEvent
from .registry import Registry

PICK_LABEL_PREFIX = "octopus-"
# From the pick until the job starts on the runner.
DEFAULT_RESERVE_TTL_S = 600.0
# Once the job runs, GitHub's own job limit (6 h) is the longest it can take.
RUNNING_TTL_S = 6 * 3600.0
RETRY_AFTER_S = 30


@dataclass(frozen=True)
class Pick:
    label: str
    runner_id: str


@dataclass
class _Reservation:
    repo: str
    run_id: str
    expires_at: float


def pick_label_of(labels: list[str]) -> str | None:
    return next((lbl for lbl in labels if lbl.casefold().startswith(PICK_LABEL_PREFIX)), None)


def target_covers(target: str, repo: str) -> bool:
    """Whether a runner registered on `target` (`owner/name` or `owner`) can take jobs of `repo`."""
    target, repo = target.casefold(), repo.casefold()
    return target == repo if "/" in target else repo.split("/", 1)[0] == target


class Picker:
    def __init__(
        self,
        registry: Registry,
        clock: Clock | None = None,
        reserve_ttl_s: float = DEFAULT_RESERVE_TTL_S,
        running_ttl_s: float = RUNNING_TTL_S,
        *,
        allowed_labels: Collection[str],
    ) -> None:
        self._registry = registry
        # Only these labels are given out. Presence is not signed, so any peer can announce any
        # label: without this list, a label that no registered runner has would win the pick and
        # the job would stay queued in GitHub (#80).
        self._allowed = frozenset(lbl.casefold() for lbl in allowed_labels)
        self._clock = clock or SystemClock()
        self._reserve_ttl_s = reserve_ttl_s
        self._running_ttl_s = running_ttl_s
        self._lock = threading.Lock()
        self._reserved: dict[str, _Reservation] = {}
        self._last_picked: dict[str, float] = {}

    def pick(self, repo: str, run_id: str) -> Pick | None:
        """Reserve the least recently picked runner that is online and not reserved or busy."""
        candidates = []
        for p in self._registry.idle_runners():
            label = pick_label_of(p.labels)
            if label is None or label.casefold() not in self._allowed:
                continue
            # A runner that names its target takes only jobs of that repo or org. A runner that
            # names none (made before this field) can still take jobs of any allowed repo.
            if p.target is None or target_covers(p.target, repo):
                candidates.append((label, p.runner_id))
        now = self._clock.monotonic()
        with self._lock:
            self._expire(now)
            free = [c for c in candidates if c[0] not in self._reserved]
            if not free:
                return None
            label, runner_id = min(free, key=lambda c: (self._last_picked.get(c[0], 0.0), c[0]))
            self._reserved[label] = _Reservation(repo, run_id, now + self._reserve_ttl_s)
            self._last_picked[label] = now
            return Pick(label, runner_id)

    def on_job_event(self, event: WorkflowJobEvent) -> None:
        """A job that ran on a reserved label: lengthen the hold while it runs, drop it after."""
        label = pick_label_of(list(event.labels))
        if label is None:
            return
        with self._lock:
            held = self._reserved.get(label)
            if held is None or held.repo != event.repo or held.run_id != str(event.run_id):
                return
            if event.action == "in_progress":
                held.expires_at = self._clock.monotonic() + self._running_ttl_s
            elif event.action == "completed":
                del self._reserved[label]

    def reserved(self) -> dict[str, str]:
        """label -> run id, for what is held now."""
        with self._lock:
            self._expire(self._clock.monotonic())
            return {label: r.run_id for label, r in self._reserved.items()}

    def _expire(self, now: float) -> None:
        for label in [lbl for lbl, r in self._reserved.items() if r.expires_at <= now]:
            del self._reserved[label]
