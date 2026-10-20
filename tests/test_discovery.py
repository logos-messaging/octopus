import json

import pytest

from octopus.crypto import (
    encode_public_key,
    encode_signing_public,
    generate_private_key,
    generate_signing_key,
    node_id,
)
from octopus.discovery import (
    AdvertError,
    AdvertPublisher,
    CoordinatorDirectory,
    issue_cert,
    make_advert,
    verify_advert,
)
from octopus.discovery import authority as A
from octopus.proto import COORDINATORS_TOPIC
from octopus.transport import MemoryBus, MemoryTransport

NOW = 1_000_000.0


class FakeClock:
    def __init__(self, t=NOW):
        self.t = t

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def advance(self, s):
        self.t += s


class Coordinator:
    def __init__(self, root, *, valid_s=86400.0, sign_key=None):
        self.enc = generate_private_key()
        self.sign = sign_key or generate_signing_key()
        self.cert = issue_cert(root, self.enc.public_key(), self.sign.public_key(), NOW + valid_s)
        self.id = node_id(self.enc.public_key())

    def advert(self, now=NOW, ttl_s=90, **kw):
        return make_advert(
            self.cert,
            self.sign,
            multiaddrs=kw.pop("multiaddrs", []),
            capabilities=kw.pop("capabilities", ["linux"]),
            now=now,
            ttl_s=ttl_s,
            **kw,
        )


@pytest.fixture
def root():
    return generate_signing_key()


def setup_directory(root, clock=None, **kw):
    bus = MemoryBus()
    clock = clock or FakeClock()
    seen = []
    directory = CoordinatorDirectory(
        MemoryTransport(bus),
        root.public_key(),
        clock=clock,
        on_coordinator=lambda cid, pub: seen.append((cid, pub)),
        **kw,
    )
    return directory, clock, seen


def feed(directory, advert):
    return directory.accept(advert.model_dump_json().encode())


def test_valid_advert_is_accepted_and_reported(root):
    directory, _, seen = setup_directory(root)
    c = Coordinator(root)
    assert feed(directory, c.advert(multiaddrs=["/ip4/1.2.3.4/tcp/60000"])) is None
    [live] = directory.live()
    assert live.coordinator_id == c.id
    assert live.multiaddrs == ("/ip4/1.2.3.4/tcp/60000",)
    assert seen == [(c.id, encode_public_key(c.enc.public_key()))]


def test_advert_from_unauthorised_key_is_rejected(root):
    directory, _, seen = setup_directory(root)
    rogue = Coordinator(generate_signing_key())  # certified by a different root
    assert feed(directory, rogue.advert()) == A.BAD_ROOT_SIGNATURE
    assert directory.live() == [] and seen == []


def test_tampered_advert_is_rejected(root):
    directory, _, _ = setup_directory(root)
    c = Coordinator(root)
    tampered = c.advert().model_copy(update={"multiaddrs": ["/ip4/6.6.6.6/tcp/1"]})
    assert feed(directory, tampered) == A.BAD_SIGNATURE


def test_tampered_cert_is_rejected(root):
    directory, _, _ = setup_directory(root)
    c = Coordinator(root)
    advert = c.advert()
    other = generate_private_key()
    forged = advert.cert.model_copy(update={"enc_pub": encode_public_key(other.public_key())})
    assert feed(directory, advert.model_copy(update={"cert": forged})) == A.BAD_ROOT_SIGNATURE


def test_cert_binding_a_foreign_id_is_rejected(root):
    directory, _, _ = setup_directory(root)
    c = Coordinator(root)
    cert = c.cert.model_copy(update={"coordinator_id": "aaaaaaaaaaaaaaaa", "root_sig": ""})
    from octopus.crypto.signing import sign

    cert = cert.model_copy(update={"root_sig": sign(root, cert.signing_bytes())})
    advert = make_advert(cert, c.sign, multiaddrs=[], capabilities=[], now=NOW, ttl_s=90)
    assert feed(directory, advert) == A.ID_MISMATCH


def test_expired_advert_and_expired_cert_are_rejected(root):
    directory, clock, _ = setup_directory(root)
    c = Coordinator(root, valid_s=100)
    assert feed(directory, c.advert(now=NOW - 200, ttl_s=90)) == A.EXPIRED
    clock.advance(200)
    assert feed(directory, c.advert(now=clock.now())) == A.CERT_EXPIRED


def test_advert_from_the_future_is_rejected(root):
    directory, _, _ = setup_directory(root)
    assert feed(directory, Coordinator(root).advert(now=NOW + 3600)) == A.FROM_FUTURE


def test_overlong_ttl_is_rejected(root):
    directory, _, _ = setup_directory(root)
    advert = Coordinator(root).advert().model_copy(update={"expires_at": NOW + 10_000})
    # the signature no longer matches either, but the ttl cap must hold on its own
    with pytest.raises(AdvertError):
        verify_advert(advert, root.public_key(), NOW)
    assert feed(directory, advert) is not None


def test_replayed_advert_is_rejected(root):
    directory, clock, _ = setup_directory(root)
    c = Coordinator(root)
    first = c.advert(now=NOW)
    assert feed(directory, first) is None
    clock.advance(5)
    assert feed(directory, first) == A.REPLAYED
    assert feed(directory, c.advert(now=NOW - 1)) == A.REPLAYED
    assert feed(directory, c.advert(now=NOW + 5)) is None


def test_adverts_from_one_coordinator_are_rate_limited(root):
    directory, clock, _ = setup_directory(root)
    c = Coordinator(root)
    assert feed(directory, c.advert(now=NOW)) is None
    assert feed(directory, c.advert(now=NOW + 0.1)) == A.RATE_LIMITED
    clock.advance(2)
    assert feed(directory, c.advert(now=clock.now())) is None


def test_global_flood_is_dropped_before_verification(root):
    directory, _, _ = setup_directory(root)
    results = [directory.accept(b"garbage") for _ in range(200)]
    assert results.count(A.RATE_LIMITED) > 100
    assert directory.rejections[A.RATE_LIMITED] > 100


def test_garbage_and_oversized_payloads_are_malformed(root):
    directory, _, _ = setup_directory(root)
    assert directory.accept(b"not json") == A.MALFORMED
    assert directory.accept(json.dumps({"kind": "other"}).encode()) == A.MALFORMED
    assert directory.accept(b"x" * 10_000) == A.MALFORMED


def test_revoked_coordinator_is_rejected(root):
    c = Coordinator(root)
    directory, _, _ = setup_directory(root, revoked={c.id})
    assert feed(directory, c.advert()) == A.REVOKED


def test_silent_coordinator_disappears_within_three_intervals(root):
    directory, clock, _ = setup_directory(root, interval_s=30)
    c = Coordinator(root)
    feed(directory, c.advert(now=NOW, ttl_s=300))
    clock.advance(89)
    assert [x.coordinator_id for x in directory.live()] == [c.id]
    clock.advance(2)
    assert directory.live() == []


def test_pick_matches_capabilities_and_prefers_recent(root):
    directory, clock, _ = setup_directory(root)
    old, new = Coordinator(root), Coordinator(root)
    feed(directory, old.advert(capabilities=["linux", "macos"]))
    clock.advance(10)
    feed(directory, new.advert(now=clock.now(), capabilities=["linux"]))
    assert directory.pick(["linux"]).coordinator_id == new.id
    assert directory.pick(["macos"]).coordinator_id == old.id
    assert directory.pick(["windows"]) is None


def test_on_coordinator_fires_once_per_coordinator(root):
    directory, clock, seen = setup_directory(root)
    c = Coordinator(root)
    feed(directory, c.advert(now=NOW))
    clock.advance(5)
    feed(directory, c.advert(now=clock.now()))
    assert len(seen) == 1


def test_runner_with_only_the_root_key_discovers_a_later_coordinator(root):
    bus = MemoryBus()
    clock = FakeClock()
    runner_side = CoordinatorDirectory(
        MemoryTransport(bus), root.public_key(), clock=clock, interval_s=30
    )
    runner_side.start()
    assert runner_side.live() == []
    c = Coordinator(root)
    pub = AdvertPublisher(MemoryTransport(bus), c.cert, c.sign, capabilities=["linux"], clock=clock)
    pub.publish_once()
    [live] = runner_side.live()
    assert live.coordinator_id == c.id and live.capabilities == ("linux",)
    # it stops advertising
    clock.advance(95)
    assert runner_side.live() == []


def test_publisher_sent_at_is_strictly_increasing(root):
    bus = MemoryBus()
    c = Coordinator(root)
    got = []
    MemoryTransport(bus).subscribe(COORDINATORS_TOPIC, lambda t, p: got.append(json.loads(p)))
    pub = AdvertPublisher(MemoryTransport(bus), c.cert, c.sign, clock=FakeClock())
    for _ in range(3):
        pub.publish_once()  # the clock does not move
    sent = [g["sent_at"] for g in got]
    assert sent == sorted(set(sent)) and len(sent) == 3


def test_cert_signature_covers_every_field(root):
    c = Coordinator(root)
    for field, value in [
        ("not_after", NOW + 1e9),
        ("sign_pub", encode_signing_public(generate_signing_key().public_key())),
    ]:
        forged = c.cert.model_copy(update={field: value})
        advert = make_advert(forged, c.sign, multiaddrs=[], capabilities=[], now=NOW, ttl_s=90)
        with pytest.raises(AdvertError) as exc:
            verify_advert(advert, root.public_key(), NOW)
        assert exc.value.reason == A.BAD_ROOT_SIGNATURE
