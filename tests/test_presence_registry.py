import random
import time

from octopus.agent import PresencePublisher, RunnerInfo
from octopus.coordinator import Registry
from octopus.proto import PRESENCE_TOPIC, Presence
from octopus.transport import MemoryBus, MemoryTransport

INTERVAL = 30.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def setup(runner_id="r1", **kw):
    bus = MemoryBus()
    clock = FakeClock()
    registry = Registry(MemoryTransport(bus), clock, interval_s=INTERVAL)
    registry.start()
    info = RunnerInfo(runner_id, kw.get("os", "linux"), kw.get("arch", "x64"),
                      tuple(kw.get("labels", ())), kw.get("capacity", 1))
    pub = PresencePublisher(MemoryTransport(bus), info, interval_s=INTERVAL)
    return bus, clock, registry, pub


def test_join_visible_after_tick():
    _, _, registry, pub = setup()
    assert registry.online() == []
    pub.tick()
    assert [p.runner_id for p in registry.idle_runners()] == ["r1"]


def test_leave_by_timeout_within_two_intervals_of_deadline():
    _, clock, registry, pub = setup()
    pub.tick()
    clock.advance(3 * INTERVAL)
    assert registry.get("r1") is not None
    clock.advance(1)
    assert registry.get("r1") is None
    assert registry.idle_runners() == []


def test_heartbeat_keeps_runner_online():
    _, clock, registry, pub = setup()
    for _ in range(10):
        pub.tick()
        clock.advance(INTERVAL)
    assert registry.get("r1") is not None


def test_busy_idle_changes():
    _, _, registry, pub = setup(capacity=2)
    pub.tick()
    pub.set_busy(1)
    assert registry.idle_runners()
    pub.set_busy(2)
    assert registry.idle_runners() == []
    assert registry.get("r1").busy == 2
    pub.set_busy(0)
    assert registry.idle_runners()


def test_graceful_stop_removes_runner_immediately():
    _, _, registry, pub = setup()
    pub.start()
    assert registry.get("r1") is not None
    pub.stop()
    assert registry.get("r1") is None


def test_clock_skew_ignored():
    bus, clock, registry, _ = setup()
    t = MemoryTransport(bus)
    skewed = Presence(runner_id="r2", os="linux", arch="x64", sent_at=1.0)
    t.publish(PRESENCE_TOPIC, skewed.model_dump_json().encode())
    assert registry.get("r2") is not None
    future = skewed.model_copy(update={"sent_at": time.time() + 10**9})
    t.publish(PRESENCE_TOPIC, future.model_dump_json().encode())
    clock.advance(3 * INTERVAL + 1)
    assert registry.get("r2") is None


def test_duplicate_and_garbage_payloads():
    bus, _, registry, pub = setup()
    pub.tick()
    pub.tick()
    t = MemoryTransport(bus)
    t.publish(PRESENCE_TOPIC, b"not json")
    t.publish(PRESENCE_TOPIC, b'{"kind":"presence"}')
    assert len(registry.online()) == 1


def test_filters():
    bus, _, registry, _ = setup()
    for rid, os_, arch, labels in [
        ("a", "linux", "x64", ["gpu", "octopus"]),
        ("b", "macos", "arm64", ["octopus"]),
    ]:
        PresencePublisher(MemoryTransport(bus), RunnerInfo(rid, os_, arch, tuple(labels))).tick()
    ids = lambda **kw: sorted(p.runner_id for p in registry.idle_runners(**kw))
    assert ids() == ["a", "b"]
    assert ids(labels=["gpu"]) == ["a"]
    assert ids(os="macos") == ["b"]
    assert ids(arch="arm64", labels=["octopus"]) == ["b"]
    assert ids(os="windows") == []


def test_property_no_idle_after_timeout():
    rng = random.Random(7)
    _, clock, registry, pub = setup()
    last_beat = None
    for _ in range(500):
        if rng.random() < 0.4:
            pub.tick()
            last_beat = clock.now
        clock.advance(rng.uniform(0, 2 * INTERVAL))
        if last_beat is None or clock.now - last_beat > 3 * INTERVAL:
            assert registry.idle_runners() == []
