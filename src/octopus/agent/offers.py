from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import PairCipher, decode_public_key, derive_channel_key, node_id
from ..proto import FailureReason, JobAck, JobOffer, job_channel_id, jobs_topic
from ..transport import ChannelTransport

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class Rejection:
    reason: FailureReason
    detail: str | None = None


# Returns None to accept the offer, or why it is rejected (e.g. POLICY_REJECTED).
OfferHandler = Callable[[JobOffer], "FailureReason | Rejection | None"]

_REPLAY_CACHE = 4096


class OfferReceiver:
    """Runner side of the job channel. Only coordinators it was told to trust can reach it:
    the channel cipher is keyed by the runner/coordinator pair, so anything else fails to decrypt."""

    def __init__(
        self,
        transport: ChannelTransport,
        private_key: X25519PrivateKey,
        trusted_coordinators: Mapping[str, str],
        on_offer: OfferHandler,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._key = private_key
        self.runner_id = node_id(private_key.public_key())
        self._on_offer = on_offer
        self._clock = clock or SystemClock()
        self._coordinators: dict[str, str] = {}
        for coordinator_id, pubkey in trusted_coordinators.items():
            if node_id(decode_public_key(pubkey)) != coordinator_id:
                raise ValueError(f"could not trust coordinator: id does not match key: {coordinator_id}")
            self._coordinators[coordinator_id] = pubkey
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._channels: dict[str, str] = {}  # channel_id -> coordinator_id

    def start(self) -> None:
        for coordinator_id, pubkey in self._coordinators.items():
            channel_id = job_channel_id(coordinator_id, self.runner_id)
            cipher = PairCipher(
                derive_channel_key(self._key, decode_public_key(pubkey), channel_id)
            )
            self._channels[channel_id] = coordinator_id
            self._transport.open_channel(
                channel_id,
                jobs_topic(self.runner_id),
                self.runner_id,
                cipher,
                lambda sender, payload, cid=channel_id: self._on_message(cid, payload),
            )

    def stop(self) -> None:
        for channel_id in self._channels:
            self._transport.close_channel(channel_id)
        self._channels.clear()

    def _on_message(self, channel_id: str, payload: bytes) -> None:
        try:
            offer = JobOffer.model_validate_json(payload)
        except ValidationError:
            return
        if offer.runner_id != self.runner_id or offer.coordinator_id != self._channels.get(
            channel_id
        ):
            log.warning("dropped offer addressed to another party: job: %s", offer.job_id)
            return
        if self._is_replay(offer):
            return
        if offer.expires_at <= self._clock.now():
            self._ack(channel_id, offer, FailureReason.OFFER_TIMEOUT)
            return
        try:
            reason = self._on_offer(offer)
        except Exception:
            log.exception("could not handle offer: job: %s", offer.job_id)
            reason = FailureReason.JOB_FAILED
        self._ack(channel_id, offer, reason)

    def _is_replay(self, offer: JobOffer) -> bool:
        key = (offer.job_id, offer.nonce)
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = None
            if len(self._seen) > _REPLAY_CACHE:
                self._seen.popitem(last=False)
        return False

    def _ack(
        self, channel_id: str, offer: JobOffer, outcome: FailureReason | Rejection | None
    ) -> None:
        rejection = Rejection(outcome) if isinstance(outcome, FailureReason) else outcome
        ack = JobAck(
            job_id=offer.job_id,
            runner_id=self.runner_id,
            accepted=rejection is None,
            reason=rejection.reason if rejection else None,
            detail=rejection.detail if rejection else None,
        )
        self._transport.send_on_channel(channel_id, ack.model_dump_json().encode())
