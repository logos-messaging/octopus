"""What the executor needs from a way of isolating a job: a container or the light level."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

HOST = "host"
HOST_DESCRIPTION = "host (no isolation: the job runs as the machine owner)"


class Isolation(Protocol):
    level: str

    def supported(self) -> str | None:
        """Why this host cannot isolate a job this way, or None."""
        ...

    def describe(self) -> str: ...

    def prepare(self, work: Path, tag: str) -> str:
        """Make the checkout ready for the job; returns the directory the job runs in."""
        ...

    def cleanup(self, job_dir: str) -> None: ...

    def run_argv(self, name: str, work: str, script: str, env: dict[str, str]) -> list[str]: ...

    def kill_argv(self, name: str) -> list[str]: ...
