from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JobContext:
    repo: str  # "org/name"
    head_repo: str  # repo the PR branch comes from; differs from `repo` for forks
    sender_is_org_member: bool


@dataclass(frozen=True)
class TrustPolicy:
    """Octopus only serves trusted internal-team PRs."""

    allowed_repos: frozenset[str] = field(default_factory=frozenset)
    allowed_orgs: frozenset[str] = field(default_factory=frozenset)

    def check(self, job: JobContext) -> tuple[bool, str]:
        org = job.repo.split("/", 1)[0]
        if job.repo not in self.allowed_repos and org not in self.allowed_orgs:
            return False, f"repo not allowed: {job.repo}"
        if job.head_repo != job.repo:
            return False, f"fork PR rejected: {job.head_repo}"
        if not job.sender_is_org_member:
            return False, "sender is not an org member"
        return True, "ok"
