from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JobContext:
    repo: str  # "org/name"
    head_repo: str  # repo the PR branch comes from; differs from `repo` for forks
    sender_is_org_member: bool
    event: str  # what triggered the run: "push", "pull_request", "pull_request_target", ...


# pull_request_target, workflow_run and issue_comment run with base-repo trust but can be
# made to check out fork code, so they are not served.
SAFE_EVENTS: frozenset[str] = frozenset({"push", "pull_request"})


@dataclass(frozen=True)
class TrustPolicy:
    """Octopus only serves trusted internal-team PRs. An empty allowlist rejects everything."""

    allowed_repos: frozenset[str] = field(default_factory=frozenset)
    allowed_orgs: frozenset[str] = field(default_factory=frozenset)
    allowed_events: frozenset[str] = SAFE_EVENTS

    def check(self, job: JobContext) -> tuple[bool, str]:
        repo = job.repo.casefold()
        org = repo.split("/", 1)[0]
        allowed_repos = {r.casefold() for r in self.allowed_repos}
        allowed_orgs = {o.casefold() for o in self.allowed_orgs}
        if repo not in allowed_repos and org not in allowed_orgs:
            return False, f"repo not allowed: {job.repo}"
        if job.head_repo.casefold() != repo:
            return False, f"fork PR rejected: {job.head_repo}"
        if job.event not in self.allowed_events:
            return False, f"event not allowed: {job.event or '?'}"
        if not job.sender_is_org_member:
            return False, "sender is not an org member"
        return True, "ok"
