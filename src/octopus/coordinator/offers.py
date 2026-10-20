from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from enum import Enum

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import SecretStr, ValidationError

from ..clock import Clock, SystemClock
from ..crypto import PairCipher, decode_public_key, derive_channel_key, encode_public_key, node_id
from ..proto import FailureReason, JobAck, JobOffer, Presence, job_channel_id, jobs_topic
from ..transport import ChannelTransport

log = logging.getLogger(__name__)


class OfferOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    SEND_FAILED = "send-failed"
    NO_KEY = "no-key"


@dataclass(frozen=True)
class OfferResult:
    outcome: OfferOutcome
    reason: FailureReason | None = None


class OfferSender:
    """Coordinator side of the job channel: seals a JIT config to one runner and waits for its ack."""

    def __init__(
        self,
        transport: ChannelTransport,
        private_key: X25519PrivateKey,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._key = private_key
        self._clock = clock or SystemClock()
        self.coordinator_id = node_id(private_key.public_key())
        self.public_key = encode_public_key(private_key.public_key())
        self._lock = threading.Lock()
        self._open: set[str] = set()
        self._waiting: dict[str, tuple[threading.Event, list[JobAck]]] = {}

    def send_offer(
        self,
        runner: Presence,
        job_id: str,
        repo: str,
        jit_config: SecretStr,
        labels: list[str] | None = None,
        ttl_s: float = 30,
    ) -> OfferResult:
        if not runner.pubkey:
            return OfferResult(OfferOutcome.NO_KEY)
        try:
            channel_id = self._ensure_channel(runner)
        except ValueError:
            return OfferResult(OfferOutcome.NO_KEY)

        offer = JobOffer(
            job_id=job_id,
            repo=repo,
            runner_id=runner.runner_id,
            coordinator_id=self.coordinator_id,
            jit_config=jit_config,
            labels=labels or [],
            offer_ttl_s=ttl_s,
            expires_at=self._clock.now() + ttl_s,
            nonce=secrets.token_hex(16),
        )
        event = threading.Event()
        acks: list[JobAck] = []
        with self._lock:
            self._waiting[job_id] = (event, acks)
        failed: list[str | None] = []
        try:
            self._transport.send_on_channel(
                channel_id,
                offer.model_dump_json().encode(),
                lambda ok, reason: None if ok else (failed.append(reason), event.set()),
            )
            if not event.wait(ttl_s):
                return OfferResult(OfferOutcome.TIMEOUT)
        except RuntimeError:
            return OfferResult(OfferOutcome.SEND_FAILED)
        finally:
            with self._lock:
                self._waiting.pop(job_id, None)
        if not acks:
            return OfferResult(OfferOutcome.SEND_FAILED)
        ack = acks[0]
        if ack.accepted:
            return OfferResult(OfferOutcome.ACCEPTED)
        return OfferResult(OfferOutcome.REJECTED, ack.reason)

    def _ensure_channel(self, runner: Presence) -> str:
        channel_id = job_channel_id(self.coordinator_id, runner.runner_id)
        with self._lock:
            if channel_id in self._open:
                return channel_id
        peer = decode_public_key(runner.pubkey or "")
        cipher = PairCipher(derive_channel_key(self._key, peer, channel_id))
        self._transport.open_channel(
            channel_id,
            jobs_topic(runner.runner_id),
            self.coordinator_id,
            cipher,
            self._on_message,
        )
        with self._lock:
            self._open.add(channel_id)
        return channel_id

    def _on_message(self, _sender_id: str, payload: bytes) -> None:
        try:
            ack = JobAck.model_validate_json(payload)
        except ValidationError:
            return
        with self._lock:
            waiting = self._waiting.get(ack.job_id)
        if waiting is not None:
            waiting[1].append(ack)
            waiting[0].set()
