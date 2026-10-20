from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

# Anything larger is not a coordinator message; checked before parsing.
MAX_COORD_BYTES = 16 * 1024


CoordKind = Literal["heartbeat", "queued", "claimed", "accepted", "done"]


class CoordContext(BaseModel):
    """The trust inputs of a job (see octopus.policy.JobContext), carried to a taker-over."""

    repo: str
    head_repo: str
    sender_is_org_member: bool
    event: str


class CoordJob(BaseModel):
    job_id: str
    repo: str
    labels: list[str] = Field(default_factory=list)
    context: CoordContext | None = None


class CoordMessage(BaseModel):
    """Gossip between coordinators on COORD_TOPIC, signed with the sender's Ed25519 key.

    - heartbeat: the sender is alive
    - queued:    a job was received and is waiting (carries the job, so any peer can take over)
    - claimed:   the sender is scheduling the job
    - accepted:  a runner accepted the job; it runs on its own, nobody needs to take it over
    - done:      the job reached a terminal state
    """

    kind: CoordKind
    coordinator_id: str
    sent_at: float
    job_id: str = ""
    job: CoordJob | None = None
    sig: str = ""

    def signing_bytes(self) -> bytes:
        body = json.dumps(
            self.model_dump(mode="json", exclude={"sig"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return b"octopus/1/coord\n" + body.encode()
