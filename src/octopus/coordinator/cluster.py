"""Several coordinators, one job queue: no job is lost when a coordinator dies, none runs twice.

Purpose: GitHub delivers each webhook to one URL, so whichever coordinator receives it
(`submit`) announces the job to its peers on COORD_TOPIC. Every coordinator keeps a replica
of the jobs that no runner has accepted yet. Ownership is decided without coordination by
rendezvous hashing over the coordinators currently alive: the owner of a job adopts it into its
scheduler and tells the others (`claimed`). When the owner stops heartbeating before a runner
accepted the job, the next owner in the ranking adopts it from the replica. Once a runner
accepted the job it runs on its own, so peers forget it (`accepted`/`done`). When GitHub
completes a job before it ran, the coordinator that receives that webhook may not own the job,
so it tells the others (`cancelled`) and the owner drops the job from its scheduler.

All gossip is signed; messages from coordinators that are not in `peers` are dropped, so
nobody else can inject, claim or cancel jobs. Known limit: during a network partition two
coordinators can each think they own a job and both offer it; the runner and GitHub then run
it once and the extra runner registration is removed by the orphan sweep.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import sign, verify
from ..policy import JobContext
from ..proto import MAX_COORD_BYTES, CoordContext, CoordJob, CoordMessage
from ..proto.coord import CoordKind
from ..proto.messages import COORD_TOPIC
from ..transport import Transport

log = logging.getLogger(__name__)

_FORGET_SIZE = 4096


@dataclass(frozen=True)
class ClusterConfig:
    heartbeat_s: float = 5.0
    # A peer silent for this long is dead. Several heartbeats, so one lost message is no failover.
    dead_after_s: float = 15.0
    # Gossip older (or newer) than this is replayed or from a skewed clock and is dropped.
    max_skew_s: float = 60.0


@dataclass
class _Replica:
    job: CoordJob
    origin: str
    claimed_by: str | None = None


Adopt = Callable[[CoordJob], None]
# Drop a job from the local scheduler if it still waits there: (job id, detail).
Cancel = Callable[[str, str], None]


def to_coord_job(
    job_id: str, repo: str, labels: list[str] | tuple[str, ...], context: JobContext | None
) -> CoordJob:
    return CoordJob(
        job_id=job_id,
        repo=repo,
        labels=list(labels),
        context=None
        if context is None
        else CoordContext(
            repo=context.repo,
            head_repo=context.head_repo,
            sender_is_org_member=context.sender_is_org_member,
            event=context.event,
        ),
    )


def to_job_context(job: CoordJob) -> JobContext | None:
    c = job.context
    if c is None:
        return None
    return JobContext(
        repo=c.repo,
        head_repo=c.head_repo,
        sender_is_org_member=c.sender_is_org_member,
        event=c.event,
    )


class CoordinatorCluster:
    """Also the scheduler's event sink: `accepted` and `terminal` tell peers to forget a job."""

    def __init__(
        self,
        coordinator_id: str,
        signing_key: Ed25519PrivateKey,
        peers: Mapping[str, Ed25519PublicKey],
        transport: Transport,
        adopt: Adopt,
        clock: Clock | None = None,
        config: ClusterConfig | None = None,
        cancel: Cancel | None = None,
    ) -> None:
        self.coordinator_id = coordinator_id
        self._key = signing_key
        self._peers = {cid: pub for cid, pub in peers.items() if cid != coordinator_id}
        self._transport = transport
        self._adopt = adopt
        self._cancel = cancel
        self._clock = clock or SystemClock()
        self._config = config or ClusterConfig()

        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}  # peer id -> local monotonic time of last message
        self._replicas: dict[str, _Replica] = {}
        self._claims: dict[str, str] = {}  # claim heard before the job itself
        self._forgotten: OrderedDict[str, None] = OrderedDict()
        self._last_heartbeat = float("-inf")
        self._started = False

    # lifecycle

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._transport.subscribe(COORD_TOPIC, self._on_message)
        self._send("heartbeat")

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._transport.unsubscribe(COORD_TOPIC)

    def tick(self) -> None:
        """Heartbeat, repeat our own unaccepted jobs, and adopt what we now own."""
        now = self._clock.monotonic()
        republish: list[CoordJob] = []
        with self._lock:
            due = now - self._last_heartbeat >= self._config.heartbeat_s
            if due:
                self._last_heartbeat = now
                republish = [
                    r.job for r in self._replicas.values() if r.origin == self.coordinator_id
                ]
        if due:
            self._send("heartbeat")
            for job in republish:
                self._send("queued", job=job)
        self._adopt_owned()

    # API used by the webhook handler and the scheduler

    def submit(self, job: CoordJob) -> None:
        """A webhook arrived here: announce the job, and adopt it if we own it."""
        with self._lock:
            if job.job_id in self._forgotten:
                return
            self._replicas.setdefault(job.job_id, _Replica(job, self.coordinator_id))
        self._send("queued", job=job)
        self._adopt_owned()

    def accepted(self, job_id: str) -> None:
        self._forget(job_id)
        self._send("accepted", job_id=job_id)

    def terminal(self, job_id: str) -> None:
        self._forget(job_id)
        self._send("done", job_id=job_id)

    def cancel(self, job_id: str, detail: str) -> None:
        """GitHub completed the job before it ran: drop it here and wherever it is queued."""
        with self._lock:
            # Accepted or done: the job ran, so this is its normal completion. No gossip.
            known_over = job_id in self._forgotten
        if not known_over:
            self._forget(job_id)
            self._send("cancelled", job_id=job_id)
        self._cancel_local(job_id, detail)

    # ownership

    def live(self) -> list[str]:
        now = self._clock.monotonic()
        with self._lock:
            alive = [p for p, t in self._seen.items() if now - t < self._config.dead_after_s]
        return sorted([self.coordinator_id, *alive])

    def owner(self, job_id: str, live: list[str] | None = None) -> str:
        """Rendezvous hashing: every coordinator that sees the same live set agrees."""
        members = live if live is not None else self.live()
        return max(members, key=lambda c: hashlib.sha256(f"{job_id}|{c}".encode()).digest())

    def pending(self) -> list[str]:
        """Job ids this coordinator holds a replica of (tests and `status`)."""
        with self._lock:
            return sorted(self._replicas)

    def _adopt_owned(self) -> None:
        live = self.live()
        with self._lock:
            take = [
                r
                for r in self._replicas.values()
                if (r.claimed_by is None or r.claimed_by not in live)
                and self.owner(r.job.job_id, live) == self.coordinator_id
            ]
            for r in take:
                r.claimed_by = self.coordinator_id
        for r in take:
            self._send("claimed", job_id=r.job.job_id)
            try:
                self._adopt(r.job)
            except Exception:
                log.exception("could not adopt job: %s", r.job.job_id)

    def _cancel_local(self, job_id: str, detail: str) -> None:
        if self._cancel is None:
            return
        try:
            self._cancel(job_id, detail)
        except Exception:
            log.exception("could not cancel job: %s", job_id)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._replicas.pop(job_id, None)
            self._claims.pop(job_id, None)
            self._forgotten[job_id] = None
            while len(self._forgotten) > _FORGET_SIZE:
                self._forgotten.popitem(last=False)

    # gossip

    def _send(self, kind: CoordKind, job_id: str = "", job: CoordJob | None = None) -> None:
        msg = CoordMessage(
            kind=kind,
            coordinator_id=self.coordinator_id,
            sent_at=self._clock.now(),
            job_id=job.job_id if job is not None else job_id,
            job=job,
        )
        self._publish(msg)

    def _publish(self, msg: CoordMessage) -> None:
        msg.sig = sign(self._key, msg.signing_bytes())
        try:
            self._transport.publish(COORD_TOPIC, msg.model_dump_json().encode())
        except Exception:
            log.warning("could not publish coordinator message: kind: %s", msg.kind, exc_info=True)

    def _on_message(self, topic: str, payload: bytes) -> None:
        msg = self._verified(payload)
        if msg is None:
            return
        with self._lock:
            self._seen[msg.coordinator_id] = self._clock.monotonic()
            if msg.kind == "queued" and msg.job is not None:
                self._on_queued(msg)
            elif msg.kind == "claimed":
                self._on_claimed(msg)
        if msg.kind in ("accepted", "done", "cancelled"):
            self._forget(msg.job_id)
        if msg.kind == "cancelled":
            self._cancel_local(
                msg.job_id,
                f"GitHub completed the job before it ran: reported by coordinator: {msg.coordinator_id}",
            )
        self._adopt_owned()

    def _on_queued(self, msg: CoordMessage) -> None:
        job = msg.job
        assert job is not None
        if job.job_id in self._forgotten:
            return
        replica = self._replicas.get(job.job_id)
        if replica is None:
            self._replicas[job.job_id] = _Replica(
                job, msg.coordinator_id, self._claims.pop(job.job_id, None)
            )

    def _on_claimed(self, msg: CoordMessage) -> None:
        replica = self._replicas.get(msg.job_id)
        if replica is None:
            self._claims[msg.job_id] = msg.coordinator_id
            return
        if replica.claimed_by == self.coordinator_id and msg.coordinator_id != self.coordinator_id:
            log.warning(
                "job claimed twice: job: %s; by: %s and %s",
                msg.job_id,
                self.coordinator_id,
                msg.coordinator_id,
            )
            return
        replica.claimed_by = msg.coordinator_id

    def _verified(self, payload: bytes) -> CoordMessage | None:
        if len(payload) > MAX_COORD_BYTES:
            return None
        try:
            msg = CoordMessage.model_validate_json(payload)
        except ValidationError:
            return None
        if msg.coordinator_id == self.coordinator_id:
            return None
        pub = self._peers.get(msg.coordinator_id)
        if pub is None or not verify(pub, msg.signing_bytes(), msg.sig):
            log.warning("dropped unauthenticated coordinator message: from: %s", msg.coordinator_id)
            return None
        if abs(self._clock.now() - msg.sent_at) > self._config.max_skew_s:
            return None
        return msg
