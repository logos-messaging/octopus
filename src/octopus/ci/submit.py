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


@dataclass
class SubmitResult:
    handled: dict[str, str] = field(default_factory=dict)  # os -> node id
    enc_pubs: dict[str, str] = field(default_factory=dict)  # os -> that node's reply-channel key
    unhandled: list[str] = field(default_factory=list)


class Submitter:
    def __init__(
        self,
        transport: ChannelTransport,
        team: TeamState,
        *,
        wait_s: float = 45.0,
        list_wait_s: float = 10.0,
        repeat_s: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        pump: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._team = team
        self._wait_s = wait_s
        self._list_wait_s = list_wait_s
        self._repeat_s = repeat_s
        self._sleep = sleep
        self._mono = monotonic
        self._pump = pump or (lambda: None)
        self._legs: dict[str, Leg] = {}
        self._result = SubmitResult()

    def submit(self, legs: list[Leg]) -> SubmitResult:
        """Ask the fleet for every leg; the legs nobody takes in time come back as unhandled."""
        self._legs = {leg.leg_id: leg for leg in legs}
        self._result = SubmitResult()
        self._transport.subscribe(CI_TOPIC, self._on_message)
        try:
            if self._wait_for_team():
                self._request_until_taken(legs)
        finally:
            self._transport.unsubscribe(CI_TOPIC)
        self._result.unhandled = [leg.os for leg in legs if leg.os not in self._result.handled]
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
        while len(self._result.handled) < len(legs) and self._mono() - start < self._wait_s:
            if self._mono() - last >= self._repeat_s:
                last = self._mono()
                for leg in legs:
                    if leg.os not in self._result.handled:
                        payload = json.dumps({"kind": "request", "token": leg.token})
                        self._transport.publish(CI_TOPIC, payload.encode())
            self._pump()
            self._sleep(0.2)

    def _on_message(self, _topic: str, payload: bytes) -> None:
        if len(payload) > MAX_CI_BYTES:
            return
        try:
            raw = json.loads(payload)
            if isinstance(raw, dict) and raw.get("kind") == "list":
                self._team.accept_list(ListMsg.model_validate(raw).list)
                return
            if not isinstance(raw, dict) or raw.get("kind") not in ("lease", "done"):
                return
            msg = NodeMessage.model_validate(raw)
        except (ValueError, ValidationError):
            return
        leg = self._legs.get(msg.leg_id)
        if leg is None or leg.os in self._result.handled:
            return
        if valid_node_message(msg, self._team):
            self._result.handled[leg.os] = msg.node_id
            self._result.enc_pubs[leg.os] = msg.proof.enc_pub

    def follow(
        self,
        result: SubmitResult,
        on_line: Callable[[str, str], None],
        timeout_s: float,
    ) -> dict[str, dict]:
        """Print the log of the handled legs until each reports its result (or time runs out)."""
        results: dict[str, dict] = {}
        opened = []
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
                if msg.get("t") == "log":
                    on_line(os_name, str(msg.get("line", "")))
                elif msg.get("t") == "result":
                    results[os_name] = msg

            self._transport.open_channel(chan, leg_topic(leg.leg_id), "submitter", cipher, on_msg)
            opened.append(chan)
            # the node replays what it sent before this channel was open
            self._transport.send_on_channel(chan, b'{"t":"hello"}')
        start = self._mono()
        try:
            while len(results) < len(opened) and self._mono() - start < timeout_s:
                self._pump()
                self._sleep(0.2)
        finally:
            for chan in opened:
                self._transport.close_channel(chan)
        return results
