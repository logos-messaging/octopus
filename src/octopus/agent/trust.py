from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import typer

from ..policy import JobContext, TrustPolicy
from ..proto import FailureReason, JobOffer
from .offers import OfferHandler, Rejection

log = logging.getLogger(__name__)

# GitHub owner and repo names: letters, digits, '-', '_' and '.' (owners: no '_' or '.').
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_ORG_RE = re.compile(rf"^{_OWNER}$")
_REPO_RE = re.compile(rf"^{_OWNER}/{_REPO}$")


def default_config_path() -> Path:
    return Path(typer.get_app_dir("octopus")) / "runner.json"


class InvalidTarget(ValueError):
    """The name is neither an org nor an org/name repo."""


class AllowlistError(ValueError):
    """The allowlist file could not be read."""


def parse_target(target: str) -> tuple[str, str]:
    """Return ("repo" | "org", normalized name). Names are case-insensitive on GitHub."""
    name = target.strip()
    if _REPO_RE.match(name) and name.split("/", 1)[1] not in (".", ".."):
        return "repo", name.casefold()
    if _ORG_RE.match(name):
        return "org", name.casefold()
    raise InvalidTarget(f"could not parse target, expected org or org/name: {target}")


@dataclass
class Allowlist:
    """What this lender is willing to run. Persisted so it survives restarts."""

    repos: set[str] = field(default_factory=set)
    orgs: set[str] = field(default_factory=set)

    def add(self, target: str) -> bool:
        kind, name = parse_target(target)
        bucket = self.repos if kind == "repo" else self.orgs
        if name in bucket:
            return False
        bucket.add(name)
        return True

    def remove(self, target: str) -> bool:
        kind, name = parse_target(target)
        bucket = self.repos if kind == "repo" else self.orgs
        if name not in bucket:
            return False
        bucket.discard(name)
        return True

    def policy(self) -> TrustPolicy:
        return TrustPolicy(
            allowed_repos=frozenset(self.repos), allowed_orgs=frozenset(self.orgs)
        )

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            raise AllowlistError(f"could not read allowlist: {path}: {e}") from e
        if not isinstance(raw, dict):
            raise AllowlistError(f"could not read allowlist: not an object: {path}")
        # Skip anything invalid rather than widening trust by guessing.
        allow = cls()
        for key in ("repos", "orgs"):
            for item in raw.get(key, []):
                try:
                    allow.add(str(item))
                except InvalidTarget:
                    log.warning("ignored invalid allowlist entry: %s", item)
        return allow

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"repos": sorted(self.repos), "orgs": sorted(self.orgs)}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".runner-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def make_offer_handler(allowlist: Allowlist, inner: OfferHandler | None = None) -> OfferHandler:
    """Runner-side enforcement: re-check every offer against this lender's allowlist.

    Never trusts the coordinator's own check. Facts the runner cannot verify itself (fork,
    sender, trigger) come from the offer, which only a pinned coordinator can deliver.
    `inner` runs afterwards for offers that pass (e.g. start the runner).
    """

    def handle(offer: JobOffer) -> FailureReason | Rejection | None:
        context = JobContext(
            repo=offer.repo,
            head_repo=offer.head_repo,
            sender_is_org_member=offer.sender_is_org_member,
            event=offer.event,
        )
        ok, why = allowlist.policy().check(context)
        if not ok:
            log.warning("rejected offer: job: %s: %s", offer.job_id, why)
            return Rejection(FailureReason.POLICY_REJECTED, why)
        return inner(offer) if inner is not None else None

    return handle
