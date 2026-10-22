"""Building the member list from GitHub, and the safeguards around it."""

from __future__ import annotations

import base64
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import Delegation, MemberList
from .state import TeamError, make_member_list

# A new list with fewer than this fraction of the previous logins is refused without --force.
SHRINK_LIMIT = 0.5
SHRINK_MIN_SIZE = 4


class Source(Protocol):
    def org_members(self, org: str) -> list[str] | None: ...

    def collaborators(self, repo: str) -> list[str] | None: ...


def scope_logins(source: Source, scope: str) -> list[str]:
    kind, _, name = scope.partition(":")
    if kind == "org":
        logins = source.org_members(name)
    elif kind == "repo":
        logins = source.collaborators(name)
    else:
        raise TeamError(f"unknown scope, expected org:<name> or repo:<owner/name>: {scope}")
    if logins is None:
        raise TeamError(f"could not list the members of: {scope}: is gh logged in with access?")
    return logins


def build_list(
    source: Source,
    scope: str,
    signer: Ed25519PrivateKey,
    now: float,
    *,
    previous: MemberList | None = None,
    delegation: Delegation | None = None,
    extra: list[str] | None = None,
    force: bool = False,
) -> MemberList:
    logins = scope_logins(source, scope) + list(extra or [])
    fresh = {lg.lower() for lg in logins}
    if (
        previous is not None
        and not force
        and len(previous.logins) >= SHRINK_MIN_SIZE
        and len(fresh) < len(previous.logins) * SHRINK_LIMIT
    ):
        raise TeamError(
            f"the list would shrink from {len(previous.logins)} to {len(fresh)} members: "
            "check it, then run: octopus team sync --force"
        )
    version = max(int(now), (previous.version + 1) if previous is not None else 0)
    return make_member_list(signer, scope, version, sorted(fresh), now, delegation=delegation)


def key_to_text(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return base64.urlsafe_b64encode(raw).decode()


def key_from_text(text: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(text.strip()))
    except ValueError:
        raise TeamError("unreadable delegate key") from None
