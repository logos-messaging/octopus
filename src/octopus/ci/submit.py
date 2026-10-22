"""The submit side, run by the short hosted job: ask the fleet, report who took each leg."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from ..crypto import PairCipher, decode_public_key, derive_channel_key
from ..team import TeamState
from ..transport import ChannelTransport
from .engine import valid_node_message
from .messages import (
    CI_TOPIC,
    MAX_CI_BYTES,
    ListMsg,
    ListRequest,
    NodeMessage,
    channel_id,
    leg_topic,
)

log = logging.getLogger(__name__)


@dataclass
class Leg:
    """One OS to run: its id, the OIDC token minted for it, and the key replies are sealed to."""

    os: str
    leg_id: str
    token: str
    reply_key: X25519PrivateKey
    repo: str


@dataclass(frozen=True)
class _Heard:
    """The last announce of one node: where, whose, idle or busy, and when it was heard."""

    os: str
    login: str
    idle: bool
    seen: float


@dataclass
class SubmitResult:
    handled: dict[str, str] = field(default_factory=dict)  # os -> node id
    enc_pubs: dict[str, str] = field(default_factory=dict)  # os -> that node's reply-channel key
    names: dict[str, str] = field(default_factory=dict)  # os -> the node's display name
    unhandled: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # os -> why it fell back
    outcomes: dict[str, dict] = field(default_factory=dict)  # os -> the node's result message


class Submitter:
    def __init__(
        self,
        transport: ChannelTransport,
        team: TeamState,
        *,
        wait_s: float = 45.0,
        list_wait_s: float = 10.0,
        repeat_s: float = 3.0,
        discover_s: float = 25.0,
        busy_wait_s: float = 240.0,
        online_s: float = 35.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        pump: Callable[[], None] | None = None,
        done_grace_s: float = 15.0,
    ) -> None:
        self._done_grace_s = done_grace_s
        self._transport = transport
        self._team = team
        self._wait_s = wait_s
        self._list_wait_s = list_wait_s
        self._repeat_s = repeat_s
        # Nodes announce every 10 s: after two announce intervals and a margin, an OS with no
        # idle node online is given up (one lost announce must not make a leg run twice).
        self._discover_s = discover_s
        # A machine that announces busy will be free in minutes, and a hosted macOS minute costs
        # far more than a minute of this Linux job: wait for it, but for a separate, longer limit.
        self._busy_wait_s = busy_wait_s
        self._online_s = online_s  # a busy node not heard for this long is taken for gone
        self._nodes: dict[str, _Heard] = {}  # node id -> its last announce
        self._given_up: dict[str, str] = {}  # os -> why it falls back
        self._sleep = sleep
        self._mono = monotonic
        self._pump = pump or (lambda: None)
        self._legs: dict[str, Leg] = {}
        self._result = SubmitResult()

    def submit(self, legs: list[Leg]) -> SubmitResult:
        """Ask the fleet for every leg; the legs nobody takes in time come back as unhandled."""
        self._legs = {leg.leg_id: leg for leg in legs}
        self._result = SubmitResult()
        self._nodes, self._given_up = {}, {}
        self._transport.subscribe(CI_TOPIC, self._on_message)
        have_team = False
        try:
            have_team = self._wait_for_team()
            if have_team:
                self._request_until_taken(legs)
        finally:
            self._transport.unsubscribe(CI_TOPIC)
        self._result.unhandled = [leg.os for leg in legs if leg.os not in self._result.handled]
        for os_name in self._result.unhandled:
            if not have_team:
                reason = "no answer from the fleet: no member list was received"
            else:
                reason = self._given_up.get(os_name) or (
                    f"no machine took it within {self._wait_s:.0f} s"
                )
            self._result.reasons[os_name] = reason
        return self._result

    def _wait_for_team(self) -> bool:
        """The member list comes from the fleet; without it no answer can be trusted."""
        start = self._mono()
        last = -1e18
        while not self._team.current():
            if self._mono() - start > self._list_wait_s:
                log.warning("no member list from the fleet: everything falls back")
                return False
            if self._mono() - last >= self._repeat_s:
                last = self._mono()
                self._transport.publish(CI_TOPIC, ListRequest().model_dump_json().encode())
            self._pump()
            self._sleep(0.2)
        return True

    def _request_until_taken(self, legs: list[Leg]) -> None:
        start = self._mono()
        last = -1e18
        busy_seen: dict[str, float] = {}  # os -> when only busy machines of it were last heard
        while self._waiting(legs):
            for leg in self._waiting(legs):
                self._check_limit(leg, start, busy_seen)
            if self._mono() - last >= self._repeat_s:
                last = self._mono()
                for leg in self._waiting(legs):
                    payload = json.dumps({"kind": "request", "token": leg.token})
                    self._transport.publish(CI_TOPIC, payload.encode())
            self._pump()
            self._sleep(0.2)

    def _check_limit(self, leg: Leg, start: float, busy_seen: dict[str, float]) -> None:
        """Give a leg up when the limit of what is known about its OS has passed."""
        now = self._mono()
        idle, busy = self._machines(leg)
        if idle:
            # `wait` counts from the moment a machine that was busy became free: it is about to
            # claim, and the time spent waiting for it must not use up the wait for its answer.
            if now - busy_seen.get(leg.os, start) >= self._wait_s:
                self._given_up[leg.os] = f"no machine took it within {self._wait_s:.0f} s"
        elif busy:
            busy_seen[leg.os] = now
            if now - start >= self._busy_wait_s:
                self._given_up[leg.os] = (
                    f"all team machines of this OS were busy for {self._busy_wait_s:.0f} s"
                )
        elif now - start >= self._discover_s:
            self._given_up[leg.os] = "no team machine of this OS was online"

    def _machines(self, leg: Leg) -> tuple[bool, bool]:
        """(an idle machine, a busy machine) of the leg's OS that is a member for its repo."""
        now = self._mono()
        idle = busy = False
        for heard in self._nodes.values():
            if heard.os != leg.os or not self._team.is_member_login(heard.login, leg.repo):
                continue
            if heard.idle:
                idle = True
            elif now - heard.seen <= self._online_s:
                busy = True
        return idle, busy

    def _waiting(self, legs: list[Leg]) -> list[Leg]:
        return [
            leg
            for leg in legs
            if leg.os not in self._result.handled and leg.os not in self._given_up
        ]

    def _on_message(self, _topic: str, payload: bytes) -> None:
        if len(payload) > MAX_CI_BYTES:
            return
        try:
            raw = json.loads(payload)
            if isinstance(raw, dict) and raw.get("kind") == "list":
                self._team.accept_list(ListMsg.model_validate(raw).list)
                return
            if not isinstance(raw, dict) or raw.get("kind") not in ("announce", "lease", "done"):
                return
            msg = NodeMessage.model_validate(raw)
        except (ValueError, ValidationError):
            return
        if msg.kind == "announce":
            if valid_node_message(msg, self._team):
                self._nodes[msg.node_id] = _Heard(
                    msg.os, msg.proof.login, msg.busy < msg.capacity, self._mono()
                )
                if len(self._nodes) > 1000:
                    del self._nodes[next(iter(self._nodes))]
            return
        leg = self._legs.get(msg.leg_id)
        if leg is None or leg.os in self._result.handled:
            return
        if valid_node_message(msg, self._team, leg.repo):
            self._result.handled[leg.os] = msg.node_id
            self._result.enc_pubs[leg.os] = msg.proof.enc_pub
            self._result.names[leg.os] = msg.name

    def follow(
        self,
        result: SubmitResult,
        on_line: Callable[[str, str], None],
        timeout_s: float,
        silent_s: float = 90.0,
    ) -> dict[str, dict]:
        """Print the log of the handled legs until each reports its result.

        Returns os -> the result message of its node: `state` is success, failure, error or
        cancelled as the node reported it. Two are made up here: `lost`, a node that said nothing
        (no log line, no lease) for `silent_s`, whose leg should run elsewhere, and `timeout`,
        a leg still running when `timeout_s` passed.
        """
        results: dict[str, dict] = {}
        opened: dict[str, str] = {}  # os -> channel
        owners = {os_name: node for os_name, node in result.handled.items()}
        alive: dict[str, float] = {}
        done_said: dict[str, tuple[str, float]] = {}  # os -> (state, when) from a `done` lease

        def on_node_message(_topic: str, payload: bytes) -> None:
            if len(payload) > MAX_CI_BYTES:
                return
            try:
                msg = NodeMessage.model_validate(json.loads(payload))
            except (ValueError, ValidationError):
                return
            if msg.kind not in ("lease", "done"):
                return
            leg = self._legs.get(msg.leg_id)
            if leg is None or owners.get(leg.os) != msg.node_id or leg.os in results:
                return
            if not valid_node_message(msg, self._team, leg.repo):
                return
            alive[leg.os] = self._mono()
            if msg.kind == "done":
                done_said.setdefault(leg.os, (msg.state, self._mono()))

        self._transport.subscribe(CI_TOPIC, on_node_message)
        try:
            for leg in self._legs.values():
                node = result.handled.get(leg.os)
                if node is None:
                    continue
                chan = channel_id(leg.leg_id, node)
                peer = decode_public_key(result.enc_pubs[leg.os])
                cipher = PairCipher(derive_channel_key(leg.reply_key, peer, chan))

                def on_msg(_sender: str, data: bytes, os_name: str = leg.os) -> None:
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        return
                    alive[os_name] = self._mono()
                    if msg.get("t") == "log":
                        on_line(os_name, str(msg.get("line", "")))
                    elif msg.get("t") == "result":
                        results[os_name] = msg

                self._transport.open_channel(
                    chan, leg_topic(leg.leg_id), "submitter", cipher, on_msg
                )
                opened[leg.os] = chan
                alive[leg.os] = self._mono()
                # the node replays what it sent before this channel was open
                self._transport.send_on_channel(chan, b'{"t":"hello"}')
            start = self._mono()
            while len(results) < len(opened):
                now = self._mono()
                if now - start >= timeout_s:
                    for os_name in opened:
                        results.setdefault(
                            os_name,
                            {"t": "result", "state": "timeout", "exit": -1,
                             "detail": f"still running after {timeout_s:.0f} s"},
                        )
                    break
                for os_name in opened:
                    if os_name in results:
                        continue
                    said = done_said.get(os_name)
                    if said is not None and now - said[1] >= self._done_grace_s:
                        # the node finished but its result message never arrived
                        results[os_name] = {
                            "t": "result", "state": said[0] or "error", "exit": -1,
                            "detail": "the result message was lost; the state is from its lease",
                        }
                    elif said is None and now - alive.get(os_name, start) >= silent_s:
                        results[os_name] = {
                            "t": "result", "state": "lost", "exit": -1,
                            "detail": f"the machine said nothing for {silent_s:.0f} s",
                        }
                self._pump()
                self._sleep(0.2)
        finally:
            self._transport.unsubscribe(CI_TOPIC)
            for chan in opened.values():
                self._transport.close_channel(chan)
        result.outcomes = dict(results)
        return results
