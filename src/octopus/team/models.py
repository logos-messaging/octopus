"""Signed team data: who belongs to the team, and the proof that a node is one of them.

The team has one Ed25519 key. It signs `MemberList`s (GitHub logins), directly or through a
short-lived `Delegation` to another key, so a service can renew the list daily without ever
holding the team key. A node proves it belongs to a listed login with a `NodeProof`: the
node's keys, signed with that user's SSH key.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

NODE_PROOF_NAMESPACE = "octopus-node@logos-messaging"


def _canonical(model: BaseModel, exclude: set[str], domain: bytes) -> bytes:
    body = json.dumps(
        model.model_dump(mode="json", exclude=exclude), sort_keys=True, separators=(",", ":")
    )
    return domain + body.encode()


class Delegation(BaseModel):
    """The team key lets `delegate_pub` sign member lists of `scope` until `not_after`."""

    delegate_pub: str
    scope: str
    not_after: float
    sig: str = ""

    def signing_bytes(self) -> bytes:
        return _canonical(self, {"sig"}, b"octopus/1/team-delegation\n")


class MemberList(BaseModel):
    scope: str  # "org:<name>" or "repo:<owner/name>"
    version: int
    issued_at: float
    not_after: float
    logins: list[str] = Field(default_factory=list)
    signer_pub: str
    delegation: Delegation | None = None
    sig: str = ""

    def signing_bytes(self) -> bytes:
        return _canonical(self, {"sig"}, b"octopus/1/team-members\n")


class NodeProof(BaseModel):
    login: str
    node_id: str
    enc_pub: str
    sign_pub: str
    not_after: float
    ssh_sig: str = ""

    def signing_bytes(self) -> bytes:
        return _canonical(self, {"ssh_sig"}, b"octopus/1/node-proof\n")
