"""A node of Octopus CI: takes legs (one OS of one workflow run) without any central server.

How nodes agree who runs a leg, with no vote:

- Every node ranks the nodes that could run it (members, online, idle, same OS) by a hash of the
  leg id and the node id (rendezvous hashing). Everyone computes the same order.
- The first node claims at once, the second after `step_s`, and so on. A claim is then held for a
  short grace period; a claim that beats it (a higher epoch, or the lower score) makes it yield.
- The owner publishes leases. When they stop for `lease_s` the next node in the order takes over
  with epoch + 1. A node that lost never posts a final status (fencing by epoch).
- Under a partition a leg may run twice. That costs machine time only: the commit status of a
  leg is keyed by (commit, OS), so the later post replaces the earlier.

The engine is single-threaded in spirit: `handle()` takes messages, `tick()` moves time. The
caller (`start()`) drives `tick()` from a thread. Messages are sent after the lock is released.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import (
    KeyMaterialError,
    PairCipher,
    decode_public_key,
    decode_signing_public,
    derive_channel_key,
    node_id,
    sign,
    verify,
)
from ..oidc import OidcError, OidcVerifier, unverified_audience
from ..policy import SAFE_EVENTS, JobContext, TrustPolicy
from ..team import NodeProof, TeamState
from ..transport import ChannelTransport
from .messages import (
    AUD_PREFIX,
    CI_TOPIC,
    MAX_CI_BYTES,
    ListMsg,
    ListRequest,
    NodeMessage,
    Request,
    channel_id,
    leg_topic,
    make_leg_id,
    parse_audience,
    precedence,
    score,
)

log = logging.getLogger(__name__)

OFFICIAL_WORKFLOW = "logos-messaging/octopus/.github/workflows/octopus-ci.yml@"
MAX_LINE_BYTES = 4096


@dataclass(frozen=True)
class CiConfig:
    allowed_repos: frozenset[str] = frozenset()
    allowed_orgs: frozenset[str] = frozenset()
    # `job_workflow_ref` of the token must start with this: only the official workflow submits.
    workflow_pin: str = OFFICIAL_WORKFLOW
    os_name: str = "linux"
    arch: str = "x64"
    name: str = ""  # shown in commit statuses and job summaries
    capacity: int = 1
    grace_s: float = 1.0  # a claim is held this long before running
    step_s: float = 2.0  # each rank waits this much longer to claim
    lease_s: float = 30.0  # a silent owner is dead after this
    lease_every_s: float = 10.0
    announce_s: float = 10.0
    offline_s: float = 35.0
    forget_s: float = 6 * 3600.0 + 900.0
    # A leg declined for being busy is run later only while the submitter still repeats its request
    # (it does every few seconds): once it gave up and went to a hosted runner, running it is waste.
    request_fresh_s: float = 30.0
    reply_hold_s: float = 300.0  # the reply channel stays open this long after the result
    max_buffer: int = 2000


@dataclass(frozen=True)
class LegInfo:
    leg_id: str
    repo: str
    sha: str
    ref: str
    event: str
    run_id: str
    attempt: str
    os: str
    reply_pub: str
    workflow: str


class ExecResult(NamedTuple):
    exit_code: int
    detail: str = ""


class Executor(Protocol):
    def run(
        self, leg: LegInfo, on_line: Callable[[str], None], should_stop: Callable[[], bool]
    ) -> ExecResult: ...


class StatusPoster(Protocol):
    def post(self, leg: LegInfo, state: str, description: str) -> None:
        """state: pending | success | failure | error."""


Spawn = Callable[[Callable[[], None]], None]


def _thread_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="octopus-ci-leg", daemon=True).start()


@dataclass
class _Leg:
    info: LegInfo
    created: float
    # waiting | claiming | running | yielded | closed | busy (declined: no free capacity yet)
    state: str = "waiting"
    claim_at: float = 0.0
    start_at: float = 0.0
    epoch: int = 0  # my claim's epoch, 0 = none
    max_epoch: int = 0
    best: tuple[tuple[int, str], str] | None = None  # (precedence, node id) of the best claim
    last_activity: float = 0.0
    last_request: float = 0.0  # when the submitter last asked for this leg
    last_lease: float = 0.0
    dead: set[str] = field(default_factory=set)
    stop: bool = False
    # What the submitter may ask for again (it opens its channel after the run began).
    buffer: list[dict] = field(default_factory=list)
    chan: str | None = None
    send: Callable[[dict], None] | None = None
    done_at: float = 0.0


@dataclass
class _Peer:
    msg: NodeMessage
    seen: float


class CiNode:
    def __init__(
        self,
        *,
        transport: ChannelTransport,
        team: TeamState,
        proof: NodeProof,
        enc_key: X25519PrivateKey,
        sign_key: Ed25519PrivateKey,
        verifier: OidcVerifier,
        executor: Executor,
        status: StatusPoster,
        config: CiConfig,
        clock: Clock | None = None,
        spawn: Spawn | None = None,
    ) -> None:
        self._transport = transport
        self._team = team
        self._proof = proof
        self._enc_key = enc_key
        self._sign_key = sign_key
        self._verifier = verifier
        self._executor = executor
        self._status = status
        self._cfg = config
        self._clock = clock or SystemClock()
        self._spawn = spawn or _thread_spawn
        self.node_id = proof.node_id
        self._lock = threading.RLock()
        self._outbox: list[bytes] = []
        self._peers: dict[str, _Peer] = {}
        self._legs: dict[str, _Leg] = {}
        self._early: dict[str, list[NodeMessage]] = {}
        # digest of a request token -> the leg it made ("" until known). A token verifies once
        # (one-time `jti`), so a repeat is matched by digest and never verified again.
        self._tokens: dict[str, str] = {}
        self._last_announce = -1e18
        self._stopping = threading.Event()
        self._ticker: threading.Thread | None = None
        self._policy = TrustPolicy(
            allowed_repos=config.allowed_repos,
            allowed_orgs=config.allowed_orgs,
            allowed_events=SAFE_EVENTS,
        )

    # lifecycle

    def start(self, tick_every_s: float = 0.5) -> None:
        self._transport.subscribe(CI_TOPIC, self.handle)
        self.tick()
        self._ticker = threading.Thread(
            target=self._loop, args=(tick_every_s,), name="octopus-ci-tick", daemon=True
        )
        self._ticker.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._ticker is not None:
            self._ticker.join(timeout=5)
        self._transport.unsubscribe(CI_TOPIC)

    def _loop(self, every_s: float) -> None:
        while not self._stopping.wait(every_s):
            try:
                self.tick()
            except Exception:
                log.exception("CI tick failed")

    # state for tests and status

    def leg_state(self, leg_id: str) -> str | None:
        with self._lock:
            leg = self._legs.get(leg_id)
            return leg.state if leg else None

    def busy(self) -> int:
        with self._lock:
            return self._busy()

    def _busy(self) -> int:
        return sum(1 for leg in self._legs.values() if leg.state == "running")

    # messages in

    def handle(self, _topic: str, payload: bytes) -> None:
        if len(payload) > MAX_CI_BYTES:
            return
        try:
            raw = json.loads(payload)
            kind = raw.get("kind") if isinstance(raw, dict) else None
            if kind == "request":
                self._on_request(Request.model_validate(raw))
            elif kind == "list-request":
                ListRequest.model_validate(raw)
                self._on_list_request()
            elif kind == "list":
                self._team.accept_list(ListMsg.model_validate(raw).list)
            elif kind in ("announce", "claim", "lease", "done"):
                self._on_node_message(NodeMessage.model_validate(raw))
        except (ValueError, ValidationError):
            return
        finally:
            self._flush()

    def _on_list_request(self) -> None:
        for ml in self._team.current():
            self._send_raw(ListMsg(list=ml).model_dump_json().encode())

    def _on_request(self, req: Request) -> None:
        aud = unverified_audience(req.token)
        parsed = parse_audience(aud) if aud else None
        if parsed is None or parsed[1] != self._cfg.os_name:
            return  # not a leg for this OS
        digest = hashlib.sha256(req.token.encode()).hexdigest()
        with self._lock:
            if digest in self._tokens:
                # The submitter repeats its request until someone answers: that also tells a
                # node that declined the leg for being busy that it is still wanted.
                known = self._legs.get(self._tokens[digest])
                if known is not None:
                    known.last_request = self._clock.monotonic()
                return
            self._tokens[digest] = ""
            if len(self._tokens) > 10_000:
                self._tokens.clear()
        try:
            claims = self._verifier.verify(req.token, audience_prefix=AUD_PREFIX)
        except OidcError as e:
            log.info("ignored a CI request: %s", e)
            if str(e).startswith("could not fetch"):
                with self._lock:
                    self._tokens.pop(digest, None)  # the submitter's repeat may succeed
            return
        reply_pub, os_name = parsed
        try:
            decode_public_key(reply_pub)
        except KeyMaterialError:
            return
        ok, reason = self._policy.check(
            JobContext(
                repo=claims.repository,
                head_repo=claims.repository,
                sender_is_org_member=True,
                event=claims.event_name,
            )
        )
        if ok and not claims.job_workflow_ref.startswith(self._cfg.workflow_pin):
            ok, reason = False, f"workflow not allowed: {claims.job_workflow_ref}"
        if ok and not claims.sha:
            ok, reason = False, "token has no commit"
        if not ok:
            log.warning("CI request rejected: %s: %s", claims.repository, reason)
            return
        info = LegInfo(
            leg_id=make_leg_id(claims.repository, claims.run_id, claims.run_attempt, os_name),
            repo=claims.repository,
            sha=claims.sha,
            ref=claims.ref,
            event=claims.event_name,
            run_id=claims.run_id,
            attempt=claims.run_attempt,
            os=os_name,
            reply_pub=reply_pub,
            workflow=claims.job_workflow_ref,
        )
        with self._lock:
            if info.leg_id in self._legs:
                return
            now = self._clock.monotonic()
            leg = _Leg(info=info, created=now, last_activity=now, last_request=now)
            self._legs[info.leg_id] = leg
            self._tokens[digest] = info.leg_id
            self._plan_claim(leg, now)
            for early in self._early.pop(info.leg_id, []):
                self._apply(leg, early, now)

    def _on_node_message(self, msg: NodeMessage) -> None:
        if not self._valid_node_message(msg):
            return
        now = self._clock.monotonic()
        with self._lock:
            if msg.kind == "announce":
                self._peers[msg.node_id] = _Peer(msg, now)
                return
            leg = self._legs.get(msg.leg_id)
            if leg is None:
                pending = self._early.setdefault(msg.leg_id, [])
                if len(pending) < 64 and len(self._early) < 1024:
                    pending.append(msg)
                return
            self._apply(leg, msg, now)
            self._peers.setdefault(msg.node_id, _Peer(msg, now))

    def _valid_node_message(self, msg: NodeMessage) -> bool:
        return valid_node_message(msg, self._team)

    # the claim protocol

    def _eligible(self, leg: _Leg, now: float) -> list[str]:
        """Node ids that could run the leg, best first."""
        info = leg.info
        ids = []
        for nid, peer in self._peers.items():
            m = peer.msg
            if nid == self.node_id or nid in leg.dead:
                continue
            if now - peer.seen > self._cfg.offline_s or m.os != info.os:
                continue
            if m.busy >= m.capacity or not self._team.is_member_login(m.proof.login, info.repo):
                continue
            ids.append(nid)
        if self._cfg.os_name == info.os and self._load(leg) < self._cfg.capacity:
            ids.append(self.node_id)
        return sorted(ids, key=lambda n: score(info.leg_id, n))

    def _load(self, besides: _Leg) -> int:
        """Legs this node is running or about to claim, so that it never over-commits."""
        return sum(
            1
            for leg in self._legs.values()
            if leg is not besides
            and (
                leg.state in ("running", "claiming")
                or (leg.state == "waiting" and (leg.best is None or leg.best[1] == self.node_id))
            )
        )

    def _plan_claim(self, leg: _Leg, now: float) -> None:
        order = self._eligible(leg, now)
        if self.node_id not in order:
            # Busy: let the others take it. Not "yielded": a leg nobody takes is planned again
            # once this node is free (see `_tick_leg`), which is what lets a lone machine serve
            # the request that arrived while it was busy.
            leg.state = "busy"
            return
        leg.state = "waiting"
        leg.claim_at = now + order.index(self.node_id) * self._cfg.step_s

    def _apply(self, leg: _Leg, msg: NodeMessage, now: float) -> None:
        """A claim, lease or done about this leg from another node."""
        if msg.node_id == self.node_id:
            return
        # The message was checked as coming from some member; it must be one for this repo.
        if not self._team.is_member_login(msg.proof.login, leg.info.repo):
            return
        if msg.kind == "done":
            leg.state = "closed" if leg.state != "running" else leg.state
            if leg.state == "running" and precedence(
                msg.epoch, leg.info.leg_id, msg.node_id
            ) < precedence(leg.epoch, leg.info.leg_id, self.node_id):
                leg.stop = True
            return
        leg.max_epoch = max(leg.max_epoch, msg.epoch)
        key = precedence(msg.epoch, leg.info.leg_id, msg.node_id)
        if leg.best is None or key < leg.best[0]:
            leg.best = (key, msg.node_id)
        if leg.best[1] == msg.node_id:
            leg.last_activity = now
        mine = precedence(leg.epoch, leg.info.leg_id, self.node_id)
        if leg.state in ("claiming", "running") and key < mine:
            if leg.state == "running":
                leg.stop = True
                leg.state = "yielded"
            else:
                leg.state = "waiting"
                leg.claim_at = now + 1e18  # someone better has it: wait for its lease to lapse

    def tick(self) -> None:
        now = self._clock.monotonic()
        to_run: list[_Leg] = []
        with self._lock:
            if now - self._last_announce >= self._cfg.announce_s:
                self._announce(now)
            for leg in list(self._legs.values()):
                self._tick_leg(leg, now, to_run)
                if now - leg.created > self._cfg.forget_s and leg.state != "running":
                    del self._legs[leg.info.leg_id]
            for leg in self._legs.values():
                if leg.chan and leg.done_at and now - leg.done_at > self._cfg.reply_hold_s:
                    self._close_reply_channel(leg.chan)
                    leg.chan = None
            for nid in [n for n, p in self._peers.items() if now - p.seen > 3 * self._cfg.offline_s]:
                del self._peers[nid]
        self._flush()
        for leg in to_run:
            self._spawn(lambda leg=leg: self._run(leg))

    def _tick_leg(self, leg: _Leg, now: float, to_run: list[_Leg]) -> None:
        cfg = self._cfg
        if leg.state == "busy":
            # In the order of arrival, one per free slot: planning a leg makes it count as load.
            # It joins the ranking of the nodes idle now, so it never jumps ahead of them; a claim
            # of another node that was heard meanwhile (`leg.best`) still stands.
            if now - leg.last_request <= cfg.request_fresh_s:
                self._plan_claim(leg, now)
        elif leg.state == "waiting":
            if leg.best is None or leg.best[1] == self.node_id:
                if now >= leg.claim_at:
                    self._claim(leg, now)
            elif now - leg.last_activity > cfg.lease_s:
                leg.dead.add(leg.best[1])
                leg.best = None
                self._plan_claim(leg, now)
        elif leg.state == "claiming":
            if now >= leg.start_at:
                leg.state = "running"
                leg.last_lease = now
                self._announce(now)
                self._send_leg("lease", leg)
                to_run.append(leg)
        elif leg.state == "running" and now - leg.last_lease >= cfg.lease_every_s:
            leg.last_lease = now
            self._send_leg("lease", leg)

    def _claim(self, leg: _Leg, now: float) -> None:
        leg.epoch = leg.max_epoch + 1
        leg.max_epoch = leg.epoch
        leg.state = "claiming"
        leg.start_at = now + self._cfg.grace_s
        leg.best = (precedence(leg.epoch, leg.info.leg_id, self.node_id), self.node_id)
        leg.last_activity = now
        self._send_leg("claim", leg)

    # running a leg

    def _run(self, leg: _Leg) -> None:
        info = leg.info
        chan = channel_id(info.leg_id, self.node_id)
        log.info("CI leg started: %s at %s", info.leg_id, info.sha)
        send = self._open_reply_channel(leg, chan)
        state, detail, code = "error", "", -1
        try:
            self._status.post(info, "pending", f"running on {self._machine()} ({info.os})")
            result = self._executor.run(
                info,
                lambda line: send({"t": "log", "line": line[:MAX_LINE_BYTES]}),
                lambda: leg.stop or self._stopping.is_set(),
            )
            code, detail = result.exit_code, result.detail
            state = "success" if code == 0 else "failure"
        except Exception as e:  # the executor failed: report an error, never crash the node
            log.exception("CI leg failed: %s", info.leg_id)
            detail = f"{type(e).__name__}"
        with self._lock:
            fenced = leg.stop
            leg.state = "yielded" if fenced else "closed"
        log.info("CI leg finished: %s: %s%s", info.leg_id, state, " (fenced)" if fenced else "")
        if not fenced:
            try:
                self._status.post(
                    info, state, f"{state} on {self._machine()}" + (f": {detail}" if detail else "")
                )
            except Exception:
                log.exception("could not post the commit status: %s", info.leg_id)
            send({"t": "result", "state": state, "exit": code, "detail": detail})
            with self._lock:
                self._send_leg("done", leg, state=state)
                self._announce(self._clock.monotonic())
            self._flush()
        with self._lock:
            leg.done_at = self._clock.monotonic()

    def _machine(self) -> str:
        return self._cfg.name or "a team machine"

    def _open_reply_channel(self, leg: _Leg, chan: str) -> Callable[[dict], None]:
        info = leg.info
        opened = False

        def send_raw(msg: dict) -> None:
            if not opened:
                return
            try:
                self._transport.send_on_channel(chan, json.dumps(msg).encode())
            except Exception:
                log.debug("could not send on the reply channel: %s", chan, exc_info=True)

        def on_reply(_sender: str, payload: bytes) -> None:
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("t") == "hello":
                with self._lock:
                    replay = list(leg.buffer)
                for m in replay:
                    send_raw(m)

        def send(msg: dict) -> None:
            with self._lock:
                leg.buffer.append(msg)
                del leg.buffer[: -self._cfg.max_buffer]
            send_raw(msg)

        try:
            peer = decode_public_key(info.reply_pub)
            cipher = PairCipher(derive_channel_key(self._enc_key, peer, chan))
            self._transport.open_channel(chan, leg_topic(info.leg_id), self.node_id, cipher, on_reply)
            opened = True
            leg.chan = chan
        except Exception:
            log.warning("could not open the reply channel: %s", info.leg_id, exc_info=True)
        return send

    def _close_reply_channel(self, chan: str) -> None:
        try:
            self._transport.close_channel(chan)
        except Exception:
            log.debug("could not close the reply channel: %s", chan, exc_info=True)

    # messages out

    def _announce(self, now: float) -> None:
        self._last_announce = now
        self._peers[self.node_id] = _Peer(self._signed("announce"), now)
        self._outbox.append(self._peers[self.node_id].msg.model_dump_json().encode())

    def _send_leg(self, kind: str, leg: _Leg, state: str = "") -> None:
        msg = self._signed(kind, leg_id=leg.info.leg_id, epoch=leg.epoch, state=state)
        self._outbox.append(msg.model_dump_json().encode())

    def _send_raw(self, payload: bytes) -> None:
        self._outbox.append(payload)

    def _signed(self, kind: str, **fields: object) -> NodeMessage:
        msg = NodeMessage(
            kind=kind,  # type: ignore[arg-type]
            node_id=self.node_id,
            proof=self._proof,
            sent_at=self._clock.now(),
            os=self._cfg.os_name,
            arch=self._cfg.arch,
            busy=self._busy(),
            capacity=self._cfg.capacity,
            name=self._cfg.name,
            **fields,  # type: ignore[arg-type]
        )
        return msg.model_copy(update={"sig": sign(self._sign_key, msg.signing_bytes())})

    def _flush(self) -> None:
        with self._lock:
            out, self._outbox = self._outbox, []
        for payload in out:
            try:
                self._transport.publish(CI_TOPIC, payload)
            except Exception:
                log.warning("could not publish a CI message", exc_info=True)


def valid_node_message(msg: NodeMessage, team: TeamState, repo: str | None = None) -> bool:
    """Signed by a node whose keys a listed GitHub user vouched for.

    With `repo`, the user must be on a list whose scope covers that repo: a login that a
    delegated key put on the list of another org does not count here.
    """
    proof = msg.proof
    if proof.node_id != msg.node_id:
        return False
    try:
        sign_pub = decode_signing_public(proof.sign_pub)
    except KeyMaterialError:
        return False
    if not verify(sign_pub, msg.signing_bytes(), msg.sig):
        return False
    return team.check_proof(proof, repo)


def own_node_id(enc_key: X25519PrivateKey) -> str:
    return node_id(enc_key.public_key())
