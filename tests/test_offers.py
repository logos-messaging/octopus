import logging
import threading

from pydantic import SecretStr

from octopus.agent import OfferReceiver, PresencePublisher, RunnerInfo
from octopus.coordinator import OfferOutcome, OfferSender, Registry
from octopus.crypto import encode_public_key, generate_private_key, node_id
from octopus.proto import FailureReason, JobOffer, Presence, job_channel_id
from octopus.transport import MemoryBus, MemoryTransport

SECRET = "super-secret-jit-config"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def monotonic(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class World:
    def __init__(self, handler=None, runner_key=None):
        self.bus = MemoryBus()
        self.clock = FakeClock()
        self.coord_key = generate_private_key()
        self.runner_key = runner_key or generate_private_key()
        self.seen: list[JobOffer] = []

        def default_handler(offer):
            self.seen.append(offer)

        self.sender = OfferSender(MemoryTransport(self.bus), self.coord_key, self.clock)
        self.receiver = OfferReceiver(
            MemoryTransport(self.bus),
            self.runner_key,
            {self.sender.coordinator_id: self.sender.public_key},
            handler or default_handler,
            self.clock,
        )
        self.receiver.start()
        self.runner = Presence(
            runner_id=self.receiver.runner_id,
            os="linux",
            arch="x64",
            sent_at=0,
            pubkey=encode_public_key(self.runner_key.public_key()),
        )

    def offer(self, job_id="j1", ttl_s=1):
        return self.sender.send_offer(self.runner, job_id, "acme/app", SecretStr(SECRET), ttl_s=ttl_s)


def test_round_trip_accepted():
    w = World()
    assert w.offer().outcome is OfferOutcome.ACCEPTED
    assert [o.jit_config.get_secret_value() for o in w.seen] == [SECRET]


def test_jit_config_not_on_the_wire():
    w = World()
    w.offer()
    assert w.bus.wire and all(SECRET.encode() not in frame for frame in w.bus.wire)


def test_other_runner_cannot_read_offer():
    w = World()
    eavesdropper_key = generate_private_key()
    stolen: list[bytes] = []
    eve = MemoryTransport(w.bus)
    from octopus.crypto import PairCipher, derive_channel_key

    channel_id = job_channel_id(w.sender.coordinator_id, w.receiver.runner_id)
    # Eve joins the same channel with a key derived from her own pair; her cipher cannot decrypt.
    eve.open_channel(
        channel_id,
        "t",
        "eve",
        PairCipher(derive_channel_key(eavesdropper_key, w.coord_key.public_key(), channel_id)),
        lambda s, p: stolen.append(p),
    )
    assert w.offer().outcome is OfferOutcome.ACCEPTED
    assert stolen == []


def test_rejection_reason_is_reported():
    w = World(handler=lambda offer: FailureReason.POLICY_REJECTED)
    result = w.offer()
    assert result.outcome is OfferOutcome.REJECTED
    assert result.reason is FailureReason.POLICY_REJECTED


def test_handler_exception_rejects_without_leaking(caplog):
    def boom(offer):
        raise RuntimeError("kaboom")

    w = World(handler=boom)
    with caplog.at_level(logging.DEBUG):
        result = w.offer()
    assert result.outcome is OfferOutcome.REJECTED
    assert SECRET not in caplog.text


def test_expired_offer_rejected():
    w = World()
    offer = JobOffer(
        job_id="old",
        repo="acme/app",
        runner_id=w.receiver.runner_id,
        coordinator_id=w.sender.coordinator_id,
        jit_config=SecretStr(SECRET),
        expires_at=w.clock.now() - 1,
        nonce="n",
    )
    w.sender._ensure_channel(w.runner)
    event, acks = threading.Event(), []
    w.sender._waiting["old"] = (event, acks)
    channel_id = job_channel_id(w.sender.coordinator_id, w.receiver.runner_id)
    w.sender._transport.send_on_channel(channel_id, offer.model_dump_json().encode())
    assert w.seen == []
    assert [(a.accepted, a.reason) for a in acks] == [(False, FailureReason.OFFER_TIMEOUT)]


def test_replayed_offer_ignored():
    w = World()
    assert w.offer("j1").outcome is OfferOutcome.ACCEPTED
    offer = w.seen[0]
    channel_id = job_channel_id(w.sender.coordinator_id, w.receiver.runner_id)
    w.sender._transport.send_on_channel(channel_id, offer.model_dump_json().encode())
    assert len(w.seen) == 1


def test_offer_for_another_runner_dropped():
    w = World()
    w.sender._ensure_channel(w.runner)
    channel_id = job_channel_id(w.sender.coordinator_id, w.receiver.runner_id)
    wrong = JobOffer(
        job_id="x",
        repo="acme/app",
        runner_id="someone-else",
        coordinator_id=w.sender.coordinator_id,
        jit_config=SecretStr(SECRET),
        expires_at=w.clock.now() + 30,
        nonce="n",
    )
    w.sender._transport.send_on_channel(channel_id, wrong.model_dump_json().encode())
    assert w.seen == []


def test_tampered_frame_is_dropped():
    w = World()
    w.sender._ensure_channel(w.runner)
    channel_id = job_channel_id(w.sender.coordinator_id, w.receiver.runner_id)
    member = next(m for m in w.bus._channels[channel_id] if m.owner is w.sender._transport)
    frame = bytearray(member.cipher.encrypt(b"{}"))
    frame[-1] ^= 1
    assert w.bus.deliver_channel(channel_id, w.sender._transport, bytes(frame)) is True
    assert w.seen == []


def test_lost_send_reported():
    w = World()
    w.bus.drop_channel_sends = True
    assert w.offer(ttl_s=0.05).outcome is OfferOutcome.SEND_FAILED


def test_runner_without_pubkey_cannot_be_offered():
    w = World()
    w.runner = w.runner.model_copy(update={"pubkey": None})
    assert w.offer().outcome is OfferOutcome.NO_KEY


def test_secret_not_in_repr_or_log(caplog):
    w = World()
    with caplog.at_level(logging.DEBUG):
        w.offer()
    assert SECRET not in caplog.text
    assert SECRET not in repr(w.seen[0])
    assert SECRET not in str(w.seen[0])


def test_receiver_rejects_coordinator_id_not_matching_key():
    import pytest

    key = generate_private_key()
    with pytest.raises(ValueError) as exc:
        OfferReceiver(
            MemoryTransport(MemoryBus()),
            key,
            {"wrong-id": encode_public_key(generate_private_key().public_key())},
            lambda o: None,
        )
    assert "wrong-id" in str(exc.value)


def test_registry_rejects_presence_with_mismatched_id():
    bus = MemoryBus()
    registry = Registry(MemoryTransport(bus), FakeClock())
    registry.start()
    key = generate_private_key()
    good = RunnerInfo(node_id(key.public_key()), "linux", "x64", pubkey=encode_public_key(key.public_key()))
    forged = RunnerInfo("victim", "linux", "x64", pubkey=encode_public_key(key.public_key()))
    PresencePublisher(MemoryTransport(bus), forged).tick()
    assert registry.online() == []
    PresencePublisher(MemoryTransport(bus), good).tick()
    assert [p.runner_id for p in registry.online()] == [good.runner_id]
    assert registry.online()[0].pubkey == good.pubkey


def test_jit_config_serialises_for_the_wire():
    offer = JobOffer(job_id="j", repo="r", jit_config=SecretStr(SECRET))
    assert SECRET in offer.model_dump_json()
    assert SECRET not in repr(offer)
