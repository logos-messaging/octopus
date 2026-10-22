"""Putting a CI node together from its home directory, and keeping its team data fresh."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..clock import Clock, SystemClock
from ..crypto import decode_signing_public, encode_public_key, encode_signing_public, node_id
from ..oidc import OidcVerifier
from ..team import Delegation, TeamError, TeamState
from ..team.sync import build_list, key_from_text
from ..transport import ChannelTransport
from .engine import CiConfig, CiNode
from .exec import GhStatusPoster, GitExecutor
from .gh import Gh
from .home import Home
from .messages import CI_TOPIC, ListMsg, ListRequest

log = logging.getLogger(__name__)

MAINTAIN_EVERY_S = 60.0
SYNC_EVERY_S = 24 * 3600.0
PUBLISH_LISTS_EVERY_S = 300.0
DELEGATION_WARN_S = 7 * 24 * 3600.0


class RuntimeError_(RuntimeError):
    """The node cannot start: something in its home directory is missing or inconsistent."""


@dataclass
class Settings:
    os_name: str
    arch: str
    capacity: int = 1


class CiRuntime:
    def __init__(
        self,
        home: Home,
        transport: ChannelTransport,
        settings: Settings,
        *,
        gh: Gh | None = None,
        clock: Clock | None = None,
        executor=None,
        status=None,
        verifier: OidcVerifier | None = None,
    ) -> None:
        self.home = home
        self._transport = transport
        self._gh = gh or Gh()
        self._clock = clock or SystemClock()
        enc_key, sign_key = home.identity()
        proof = home.proof()
        if proof is None:
            raise RuntimeError_("no membership proof: run: octopus ci init <owner/repo>")
        if proof.node_id != node_id(enc_key.public_key()) or proof.sign_pub != encode_signing_public(
            sign_key.public_key()
        ):
            raise RuntimeError_("the proof is for other keys: run: octopus ci init again")
        pub = home.team_pub()
        if pub is None:
            raise RuntimeError_("no team key pinned: run: octopus ci init <owner/repo>")
        self.team = TeamState(decode_signing_public(pub), clock=self._clock.now)
        for ml in home.lists():
            self.team.accept_list(ml)
        allow = home.allow()
        self.node = CiNode(
            transport=transport,
            team=self.team,
            proof=proof,
            enc_key=enc_key,
            sign_key=sign_key,
            verifier=verifier or OidcVerifier("octopus-ci"),
            executor=executor or GitExecutor(home.work_dir),
            status=status or GhStatusPoster(),
            config=CiConfig(
                allowed_repos=frozenset(allow.repos),
                allowed_orgs=frozenset(allow.orgs),
                os_name=settings.os_name,
                arch=settings.arch,
                capacity=settings.capacity,
            ),
            clock=self._clock,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sync = -1e18
        self._last_publish = -1e18
        self.public_key = encode_public_key(enc_key.public_key())

    def start(self) -> None:
        self._transport.start()
        self.node.start()
        self._transport.publish(CI_TOPIC, ListRequest().model_dump_json().encode())
        self._thread = threading.Thread(target=self._loop, name="octopus-ci-maintain", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.node.stop()
        self._transport.stop()

    def _loop(self) -> None:
        while not self._stop.wait(MAINTAIN_EVERY_S):
            try:
                self.maintain()
            except Exception:
                log.exception("CI maintenance failed")

    def maintain(self, now: float | None = None) -> None:
        """Take lists from disk, sync when due, share the lists, save what was learned."""
        now = time.monotonic() if now is None else now
        for ml in self.home.lists():
            self.team.accept_list(ml)
        if now - self._last_sync >= SYNC_EVERY_S:
            self._last_sync = now
            self.sync_now()
        if now - self._last_publish >= PUBLISH_LISTS_EVERY_S:
            self._last_publish = now
            for ml in self.team.current():
                self._transport.publish(CI_TOPIC, ListMsg(list=ml).model_dump_json().encode())
        self.home.save_lists(self.team.current())

    def sync_now(self) -> bool:
        """The daily sync of an admin node, signed with its delegation. False when not an admin."""
        held = self.home.delegate()
        scopes = self.home.scopes()
        if held is None or not scopes:
            return False
        try:
            signer = key_from_text(held[0])
            delegation = Delegation.model_validate_json(held[1])
        except (TeamError, ValueError):
            log.warning("unreadable delegation: run: octopus team sync")
            return False
        now = self._clock.now()
        if delegation.not_after - now < DELEGATION_WARN_S:
            log.warning("the sync delegation ends soon: run: octopus team sync")
        done = False
        for scope in scopes:
            previous = next((ml for ml in self.team.current() if ml.scope == scope), None)
            try:
                ml = build_list(
                    self._gh, scope, signer, now, previous=previous, delegation=delegation
                )
            except TeamError as e:
                log.warning("sync of a scope failed, keeping the previous list: %s", e)
                continue
            if self.team.accept_list(ml):
                done = True
        return done
