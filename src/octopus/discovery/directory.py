from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..proto.adverts import COORDINATORS_TOPIC, MAX_ADVERT_BYTES, Advert
from ..transport import Transport
from .authority import (
    MALFORMED,
    RATE_LIMITED,
    REPLAYED,
    REVOKED,
    AdvertError,
    verify_advert,
)
from .publisher import ADVERT_INTERVAL_S

log = logging.getLogger(__name__)

MISSED_ADVERTS = 3
# Cheap global limit applied before any signature check, so a flood of garbage cannot burn CPU.
MAX_ADVERTS_PER_S = 20.0
BURST = 50.0
# Minimum spacing between two accepted adverts from the same coordinator.
MIN_SPACING_S = 1.0

OnCoordinator = Callable[[str, str], None]  # (coordinator_id, enc_pub)


@dataclass(frozen=True)
class LiveCoordinator:
    coordinator_id: str
    enc_pub: str
    multiaddrs: tuple[str, ...]
    capabilities: tuple[str, ...]
    sent_at: float
    last_seen: float  # clock.monotonic() of the last accepted advert


class CoordinatorDirectory:
    """Runner side: the live coordinators learned from adverts the root key authorised.

    A coordinator is live until `missed * interval_s` passes without an advert (or its advert
    or certificate expires). Invalid adverts are dropped and counted in `rejections`.
    """

    def __init__(
        self,
        transport: Transport,
        root_pub: Ed25519PublicKey,
        *,
        on_coordinator: OnCoordinator | None = None,
        revoked: Collection[str] = (),
        interval_s: float = ADVERT_INTERVAL_S,
        missed: int = MISSED_ADVERTS,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._root_pub = root_pub
        self._on_coordinator = on_coordinator
        self._revoked = frozenset(revoked)
        self._ttl_s = interval_s * missed
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[Advert, float]] = {}
        self._tokens = BURST
        self._tokens_at = self._clock.monotonic()
        self.rejections: Counter[str] = Counter()

    def start(self) -> None:
        self._transport.subscribe(COORDINATORS_TOPIC, self._on_message)

    def stop(self) -> None:
        self._transport.unsubscribe(COORDINATORS_TOPIC)

    def live(self) -> list[LiveCoordinator]:
        now, mono = self._clock.now(), self._clock.monotonic()
        with self._lock:
            out = [
                self._view(advert, seen)
                for advert, seen in self._entries.values()
                if mono - seen <= self._ttl_s
                and advert.expires_at > now
                and advert.cert.not_after > now
            ]
        return sorted(out, key=lambda c: c.last_seen, reverse=True)

    def pick(self, capabilities: Collection[str] = ()) -> LiveCoordinator | None:
        """The most recently heard coordinator that offers all `capabilities`."""
        wanted = set(capabilities)
        return next((c for c in self.live() if wanted <= set(c.capabilities)), None)

    def accept(self, payload: bytes) -> str | None:
        """Process one advert; returns None if accepted, else the rejection reason."""
        try:
            self._accept(payload)
        except AdvertError as e:
            with self._lock:
                self.rejections[e.reason] += 1
            log.debug("dropped coordinator advert: %s", e)
            return e.reason
        return None

    def _on_message(self, topic: str, payload: bytes) -> None:
        self.accept(payload)

    def _accept(self, payload: bytes) -> None:
        if not self._take_token():
            raise AdvertError(RATE_LIMITED, "global")
        if len(payload) > MAX_ADVERT_BYTES:
            raise AdvertError(MALFORMED, "too large")
        try:
            advert = Advert.model_validate_json(payload)
        except ValidationError as e:
            raise AdvertError(MALFORMED, "could not parse advert") from e
        now, mono = self._clock.now(), self._clock.monotonic()
        verify_advert(advert, self._root_pub, now)
        cid = advert.cert.coordinator_id
        if cid in self._revoked:
            raise AdvertError(REVOKED, cid)
        with self._lock:
            known = self._entries.get(cid)
            if known is not None:
                previous, seen = known
                if advert.sent_at <= previous.sent_at:
                    raise AdvertError(REPLAYED, cid)
                if mono - seen < MIN_SPACING_S:
                    raise AdvertError(RATE_LIMITED, cid)
            self._entries[cid] = (advert, mono)
        if known is None and self._on_coordinator is not None:
            self._on_coordinator(cid, advert.cert.enc_pub)

    def _take_token(self) -> bool:
        mono = self._clock.monotonic()
        with self._lock:
            self._tokens = min(BURST, self._tokens + (mono - self._tokens_at) * MAX_ADVERTS_PER_S)
            self._tokens_at = mono
            if self._tokens < 1:
                return False
            self._tokens -= 1
            return True

    @staticmethod
    def _view(advert: Advert, seen: float) -> LiveCoordinator:
        return LiveCoordinator(
            coordinator_id=advert.cert.coordinator_id,
            enc_pub=advert.cert.enc_pub,
            multiaddrs=tuple(advert.multiaddrs),
            capabilities=tuple(advert.capabilities),
            sent_at=advert.sent_at,
            last_seen=seen,
        )
