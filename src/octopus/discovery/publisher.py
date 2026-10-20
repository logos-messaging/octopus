from __future__ import annotations

import logging
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..clock import Clock, SystemClock
from ..proto.adverts import COORDINATORS_TOPIC, CoordinatorCert
from ..transport import Transport
from .authority import make_advert

log = logging.getLogger(__name__)

ADVERT_INTERVAL_S = 30.0


class AdvertPublisher:
    """Coordinator side: publishes a signed advert every `interval_s`."""

    def __init__(
        self,
        transport: Transport,
        cert: CoordinatorCert,
        sign_key: Ed25519PrivateKey,
        *,
        multiaddrs: list[str] | None = None,
        capabilities: list[str] | None = None,
        software: str = "",
        interval_s: float = ADVERT_INTERVAL_S,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._cert = cert
        self._sign_key = sign_key
        self._multiaddrs = list(multiaddrs or [])
        self._capabilities = list(capabilities or [])
        self._software = software
        self._interval_s = interval_s
        self._clock = clock or SystemClock()
        self._last_sent_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def publish_once(self) -> None:
        # Strictly increasing sent_at lets runners reject replays of older adverts.
        now = max(self._clock.now(), self._last_sent_at + 0.001)
        self._last_sent_at = now
        advert = make_advert(
            self._cert,
            self._sign_key,
            multiaddrs=self._multiaddrs,
            capabilities=self._capabilities,
            software=self._software,
            now=now,
            ttl_s=self._interval_s * 3,
        )
        self._transport.publish(COORDINATORS_TOPIC, advert.model_dump_json().encode())

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="octopus-advert", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception:
                log.exception("could not publish coordinator advert")
            self._stop.wait(self._interval_s)
