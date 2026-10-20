"""Trust-policy inputs for a `workflow_job`: repo, PR head repo, sender and whether to trust them.

A `workflow_job` payload names the repo and the sender but not where the code comes from,
so the workflow run is fetched: for a fork PR its `head_repository` is the fork (and its
`pull_requests` list is empty, so that cannot be used). Every unknown resolves to "untrusted".

The sender is trusted by org membership (GitHub App) or by write access to the repo (token
setup, `octopus github setup`, which needs no org permissions).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from ..policy import JobContext
from .app import GitHubApiError, GitHubClient, _error_message, repo_path
from .token import TokenClient
from .webhooks import WorkflowJobEvent

log = logging.getLogger(__name__)

# Stands in for a head repo GitHub did not report (e.g. a deleted fork); never equals a repo.
UNKNOWN_HEAD_REPO = "<unknown>"
# `permission` is the legacy base role: maintain reports as write, triage as read.
WRITE_PERMISSIONS = frozenset({"admin", "write"})

SenderCheck = Literal["org-membership", "repo-permission"]


@dataclass(frozen=True)
class TrustInputs:
    repo: str
    head_repo: str
    event: str  # what triggered the run: "push", "pull_request", "pull_request_target", ...
    sender: str
    sender_type: str
    # Whether the sender passed `sender_check`; the policy's (and wire's) name predates
    # the repo-permission check.
    sender_is_org_member: bool
    sender_check: SenderCheck = "org-membership"

    @property
    def is_fork(self) -> bool:
        return self.head_repo != self.repo

    def job_context(self) -> JobContext:
        return JobContext(
            repo=self.repo,
            head_repo=self.head_repo,
            sender_is_org_member=self.sender_is_org_member,
            event=self.event,
        )


def resolve_trust_inputs(
    job: WorkflowJobEvent, app: GitHubClient, *, sender_check: SenderCheck | None = None
) -> TrustInputs:
    """Fetch what the trust policy needs.

    `sender_check` defaults to repo permission for a `TokenClient`, org membership otherwise.
    Raises `GitHubApiError` if GitHub refuses, `httpx.HTTPError` if it cannot be reached.
    """
    if sender_check is None:
        sender_check = "repo-permission" if isinstance(app, TokenClient) else "org-membership"
    installation_id = job.installation_id or app.repo_installation_id(job.repo)
    run = app.get_json(f"{repo_path(job.repo)}/actions/runs/{int(job.run_id)}", installation_id)
    head = (run.get("head_repository") or {}).get("full_name") or UNKNOWN_HEAD_REPO
    return TrustInputs(
        repo=job.repo,
        head_repo=head,
        event=str(run.get("event") or ""),
        sender=job.sender,
        sender_type=job.sender_type,
        sender_is_org_member=(
            has_write_access(app, job, installation_id)
            if sender_check == "repo-permission"
            else is_org_member(app, job, installation_id)
        ),
        sender_check=sender_check,
    )


def is_org_member(app: GitHubClient, job: WorkflowJobEvent, installation_id: int) -> bool:
    """Whether the sender belongs to the org that owns the repo.

    For a repo owned by a user account, only that user counts. Bots (dependabot, ...) are
    never members. Needs the App's "Members: read" organization permission.
    """
    if job.sender_type == "Bot":
        return False
    if job.org is None:
        return job.sender.casefold() == job.owner.casefold()
    path = f"/orgs/{quote(job.org, safe='')}/members/{quote(job.sender, safe='')}"
    resp = app.request("GET", path, installation_id)
    if resp.status_code == 204:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 302:
        # GitHub answers 302 when the caller may not see private membership: a missing
        # "Members: read" permission. Treat as not a member, loudly.
        log.warning("cannot check membership in %s: grant the App 'Members: read'", job.org)
        return False
    raise GitHubApiError("GET", path, resp.status_code)


def has_write_access(app: GitHubClient, job: WorkflowJobEvent, installation_id: int) -> bool:
    """Whether the sender can push to the repo (write, maintain or admin).

    Read, triage, no access and unknown users are not trusted; neither are bots. Needs only
    "Metadata: read".
    """
    if job.sender_type == "Bot":
        return False
    path = f"{repo_path(job.repo)}/collaborators/{quote(job.sender, safe='')}/permission"
    resp = app.request("GET", path, installation_id)
    if resp.status_code == 404:  # no such user
        return False
    if resp.status_code != 200:
        raise GitHubApiError("GET", path, resp.status_code, _error_message(resp))
    try:
        permission = resp.json().get("permission")
    except (ValueError, AttributeError):
        raise GitHubApiError("GET", path, resp.status_code, "malformed response") from None
    return permission in WRITE_PERMISSIONS
