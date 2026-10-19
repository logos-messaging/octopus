from __future__ import annotations

from abc import ABC, abstractmethod

from octopus.proto import FailureReason

from .model import Event, Job, State


class JobStore(ABC):
    """Records each job's state and an append-only timeline of what happened."""

    @abstractmethod
    def create(self, job_id: str, repo: str, *, actor: str = "coordinator") -> Job: ...

    @abstractmethod
    def transition(
        self,
        job_id: str,
        state: State,
        *,
        actor: str,
        detail: str = "",
        reason: FailureReason | None = None,
        runner_id: str | None = None,
        step: str | None = None,
    ) -> Job:
        """Move a job to `state`. `reason` is required for FAILED.

        Raises IllegalTransition, UnknownJob.
        """

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def list(
        self,
        *,
        state: State | None = None,
        failed: bool = False,
        running: bool = False,
        repo: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        """Newest first. `running` means any non-terminal state past QUEUED."""

    @abstractmethod
    def timeline(self, job_id: str) -> list[Event]: ...

    @abstractmethod
    def purge(self, older_than_s: float) -> int:
        """Delete terminal jobs (and events) not updated for `older_than_s`."""
