"""Wire messages of Octopus CI on CI_TOPIC.

A *leg* is one OS of one workflow run. Everything about it comes from the submitter's OIDC
token: GitHub signs the repo, the commit, the run and, through the audience, the OS and the key
that replies are sealed to, so a request cannot be altered or replayed for something else.
Messages of nodes carry a `NodeProof` (team membership) and an Ed25519 signature.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel

from ..proto.messages import TOPIC_PREFIX
from ..team import MemberList, NodeProof

CI_TOPIC = f"{TOPIC_PREFIX}/ci/proto"
# Anything larger is not a CI message; checked before parsing.
MAX_CI_BYTES = 32 * 1024
AUD_PREFIX = "octopus-ci/"


def leg_audience(reply_pub: str, os_name: str) -> str:
    """The audience to request the OIDC token with: it binds the reply key and the OS."""
    return f"{AUD_PREFIX}{reply_pub}/{os_name}"


def parse_audience(aud: str) -> tuple[str, str] | None:
    """(reply_pub, os) from a leg audience, or None."""
    if not aud.startswith(AUD_PREFIX):
        return None
    parts = aud[len(AUD_PREFIX) :].split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def make_leg_id(repo: str, run_id: str, attempt: str, os_name: str) -> str:
    return f"{repo}:{run_id}:{attempt}:{os_name}"


def score(leg_id: str, node_id: str) -> str:
    """Rendezvous score: the lowest wins. Everyone computes the same order, nobody votes."""
    return hashlib.sha256(f"{leg_id}|{node_id}".encode()).hexdigest()


def precedence(epoch: int, leg_id: str, node_id: str) -> tuple[int, str]:
    """Smaller beats larger: a higher epoch (a takeover) first, then the lower score."""
    return (-epoch, score(leg_id, node_id))


class Request(BaseModel):
    kind: Literal["request"] = "request"
    token: str


class ListRequest(BaseModel):
    kind: Literal["list-request"] = "list-request"


class ListMsg(BaseModel):
    kind: Literal["list"] = "list"
    list: MemberList


NodeKind = Literal["announce", "claim", "lease", "done"]


class NodeMessage(BaseModel):
    """announce: the node is here. claim/lease/done: about the leg `leg_id` at `epoch`."""

    kind: NodeKind
    node_id: str
    proof: NodeProof
    sent_at: float
    os: str = ""
    arch: str = ""
    busy: int = 0
    capacity: int = 1
    leg_id: str = ""
    epoch: int = 0
    # done: success | failure | error
    state: str = ""
    # The machine's name, for display only: it is not authenticated beyond the node's signature.
    name: str = ""
    sig: str = ""

    def signing_bytes(self) -> bytes:
        body = json.dumps(
            self.model_dump(mode="json", exclude={"sig"}), sort_keys=True, separators=(",", ":")
        )
        return b"octopus/1/ci\n" + body.encode()


def channel_id(leg_id: str, node_id: str) -> str:
    digest = hashlib.sha256(leg_id.encode()).hexdigest()[:16]
    return f"octopus-ci-{digest}-{node_id}"


def leg_topic(leg_id: str) -> str:
    digest = hashlib.sha256(leg_id.encode()).hexdigest()[:16]
    return f"{TOPIC_PREFIX}/ci-{digest}/proto"
