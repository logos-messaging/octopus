from __future__ import annotations

from dataclasses import dataclass, field

# Triggers that run with the base repo's secrets and can check out attacker-controlled code.
DEFAULT_DENIED_EVENTS = frozenset({"pull_request_target", "workflow_run"})


@dataclass(frozen=True)
class JobContext:
    repo: str  # "org/name"
    head_repo: str  # repo the PR branch comes from; differs from `repo` for forks
    sender_is_org_member: bool
    event: str = ""  # what triggered the run: "push", "pull_request", ...


@dataclass(frozen=True)
class TrustPolicy:
    """Octopus only serves trusted internal-team PRs. An empty allowlist rejects everything."""

    allowed_repos: frozenset[str] = field(default_factory=frozenset)
    allowed_orgs: frozenset[str] = field(default_factory=frozenset)
    denied_events: frozenset[str] = DEFAULT_DENIED_EVENTS

    def check(self, job: JobContext) -> tuple[bool, str]:
        repo = job.repo.casefold()
        org = repo.split("/", 1)[0]
        allowed_repos = {r.casefold() for r in self.allowed_repos}
        allowed_orgs = {o.casefold() for o in self.allowed_orgs}
        if repo not in allowed_repos and org not in allowed_orgs:
            return False, f"repo not allowed: {job.repo}"
        if job.head_repo.casefold() != repo:
            return False, f"fork PR rejected: {job.head_repo}"
        if job.event in self.denied_events:
            return False, f"event not allowed: {job.event}"
        if not job.sender_is_org_member:
            return False, "sender is not an org member"
        return True, "ok"
