"""`POST /pick`: the OIDC token of a workflow job in, a runner label out.

No GitHub credential is involved: the token is checked against GitHub's public keys and the
trust policy is applied to its claims.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..oidc import Claims, OidcError, OidcVerifier
from ..policy import JobContext, TrustPolicy
from .picker import RETRY_AFTER_S, Picker

log = logging.getLogger(__name__)

DEFAULT_PATH = "/pick"


@dataclass(frozen=True)
class PickResponse:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


def job_context(claims: Claims) -> JobContext:
    """The trust inputs that the claims give.

    A fork PR gets no OIDC token, so `repository` is the repo the workflow runs in and the head
    repo cannot differ. Who may push to an allowed repo is the trust boundary, as for any runner.
    """
    return JobContext(
        repo=claims.repository,
        head_repo=claims.repository,
        sender_is_org_member=True,
        event=claims.event_name,
    )


class PickService:
    def __init__(
        self,
        verifier: OidcVerifier,
        picker: Picker,
        policy: TrustPolicy,
    ) -> None:
        self._verifier = verifier
        self._picker = picker
        self._policy = policy

    def handle(self, authorization: str | None) -> PickResponse:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return PickResponse(401, {"error": "missing bearer token"})
        try:
            claims = self._verifier.verify(token.strip())
        except OidcError as e:
            log.info("pick rejected: %s", e)
            return PickResponse(401, {"error": str(e)})
        context = job_context(claims)
        ok, reason = self._policy.check(context)
        if ok and not claims.job_workflow_ref.startswith(f"{claims.repository}/.github/workflows/"):
            ok, reason = False, f"workflow not in the repo: {claims.job_workflow_ref}"
        if not ok:
            log.warning("pick rejected: %s: %s: %s", claims.repository, claims.actor, reason)
            return PickResponse(403, {"error": reason})
        picked = self._picker.pick(claims.repository, claims.run_id)
        if picked is None:
            log.info("pick: no runner free: %s run %s", claims.repository, claims.run_id)
            return PickResponse(
                503,
                {"error": "no runner free", "retry_after_s": RETRY_AFTER_S},
                {"Retry-After": str(RETRY_AFTER_S)},
            )
        log.info("pick: %s run %s -> %s", claims.repository, claims.run_id, picked.label)
        return PickResponse(200, {"label": picked.label, "runner": picked.runner_id})
