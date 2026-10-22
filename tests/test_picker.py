import threading

from conftest import octopus_job

from octopus.agent import PresencePublisher, RunnerInfo
from octopus.coordinator import Registry
from octopus.coordinator.picker import Picker
from octopus.github.webhooks import parse_workflow_job
from octopus.transport import MemoryBus, MemoryTransport


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def now_(self):
        return self.now


def make(names=("a", "b"), **kw):
    bus = MemoryBus()
    clock = Clock()
    clock.now = 1000.0
    registry = Registry(MemoryTransport(bus), clock)
    registry.start()
    pubs = {}
    for n in names:
        info = RunnerInfo(f"id-{n}", "linux", "x64", ("octopus", f"octopus-{n}"))
        pubs[n] = PresencePublisher(MemoryTransport(bus), info)
        pubs[n].tick()
    kw.setdefault("allowed_labels", [f"octopus-{n}" for n in names])
    return clock, registry, pubs, Picker(registry, clock, **kw)


def event(action, label, run_id=1001, repo="acme/app"):
    payload = octopus_job()
    payload["action"] = action
    payload["workflow_job"]["labels"] = ["self-hosted", label]
    payload["workflow_job"]["run_id"] = run_id
    payload["workflow_job"]["runner_name"] = label
    payload["repository"]["full_name"] = repo
    return parse_workflow_job(payload, "d")


def test_least_recently_used_first():
    clock, _, _, picker = make(("a", "b", "c"))
    first = picker.pick("acme/app", "1")
    clock.now += 1
    second = picker.pick("acme/app", "2")
    clock.now += 1
    third = picker.pick("acme/app", "3")
    assert {first.label, second.label, third.label} == {"octopus-a", "octopus-b", "octopus-c"}
    picker.on_job_event(event("completed", first.label, run_id=1))
    picker.on_job_event(event("completed", second.label, run_id=2))
    clock.now += 1
    assert picker.pick("acme/app", "4").label == first.label  # the oldest pick


def test_none_free():
    _, _, _, picker = make(("a",))
    assert picker.pick("acme/app", "1") is not None
    assert picker.pick("acme/app", "2") is None


def test_runner_without_unique_label_or_offline_is_skipped():
    bus = MemoryBus()
    clock = Clock()
    registry = Registry(MemoryTransport(bus), clock)
    registry.start()
    PresencePublisher(MemoryTransport(bus), RunnerInfo("x", "linux", "x64", ("octopus",))).tick()
    gone = PresencePublisher(
        MemoryTransport(bus), RunnerInfo("y", "linux", "x64", ("octopus-y",))
    )
    gone.tick()
    gone.stop()
    assert Picker(registry, clock, allowed_labels=["octopus-y"]).pick("acme/app", "1") is None


def test_a_label_not_on_the_allowlist_is_never_given_out():
    # Presence is not signed: any peer can announce a label that no registered runner has (#80).
    bus = MemoryBus()
    clock = Clock()
    registry = Registry(MemoryTransport(bus), clock)
    registry.start()
    for runner_id, label in (("id-a", "octopus-a"), ("forged", "octopus-forged")):
        info = RunnerInfo(runner_id, "linux", "x64", (label,))
        PresencePublisher(MemoryTransport(bus), info).tick()
    picker = Picker(registry, clock, allowed_labels=["octopus-a"])
    assert picker.pick("acme/app", "1").label == "octopus-a"
    assert picker.pick("acme/app", "2") is None  # the forged label is not taken instead


def test_allowed_labels_compare_case_insensitively():
    _, _, _, picker = make(("a",), allowed_labels=["Octopus-A"])
    assert picker.pick("acme/app", "1").label == "octopus-a"


def test_busy_runner_is_skipped():
    _, _, pubs, picker = make(("a", "b"))
    pubs["a"].set_busy(1)
    assert picker.pick("acme/app", "1").label == "octopus-b"
    assert picker.pick("acme/app", "2") is None


def test_reservation_expires():
    clock, _, pubs, picker = make(("a",), reserve_ttl_s=60)
    assert picker.pick("acme/app", "1") is not None
    clock.now += 59
    pubs["a"].tick()
    assert picker.pick("acme/app", "2") is None
    clock.now += 2
    pubs["a"].tick()
    assert picker.pick("acme/app", "2") is not None


def test_completed_job_releases_only_its_own_run():
    _, _, _, picker = make(("a",))
    label = picker.pick("acme/app", "1001").label
    picker.on_job_event(event("completed", label, run_id=999))
    picker.on_job_event(event("completed", label, run_id=1001, repo="other/repo"))
    assert picker.reserved() == {label: "1001"}
    picker.on_job_event(event("completed", label, run_id=1001))
    assert picker.reserved() == {}


def test_other_jobs_of_the_run_do_not_release():
    _, _, _, picker = make(("a",))
    label = picker.pick("acme/app", "1001").label
    picker.on_job_event(event("completed", "ubuntu-latest", run_id=1001))  # the pick job
    assert picker.reserved() == {label: "1001"}


def test_running_job_holds_longer_than_the_reservation():
    clock, _, pubs, picker = make(("a",), reserve_ttl_s=60, running_ttl_s=3600)
    label = picker.pick("acme/app", "1001").label
    picker.on_job_event(event("in_progress", label, run_id=1001))
    clock.now += 1800
    pubs["a"].tick()
    assert picker.reserved() == {label: "1001"}


def test_concurrent_picks_never_double_assign():
    _, _, _, picker = make(tuple("abcde"))
    got = []
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        got.append(picker.pick("acme/app", str(i)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    labels = [p.label for p in got if p is not None]
    assert len(labels) == 5 and len(set(labels)) == 5
