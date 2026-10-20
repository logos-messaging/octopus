from __future__ import annotations


def job_channel_id(coordinator_id: str, runner_id: str) -> str:
    """One channel per (coordinator, runner) pair, keyed by their pairwise secret."""
    return f"octopus-jobs-{coordinator_id}-{runner_id}"
