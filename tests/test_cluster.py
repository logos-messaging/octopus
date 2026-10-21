"""CoordinatorCluster: ownership, takeover and who may talk to it (no scheduler, no runners)."""

import pytest

from octopus.coordinator import ClusterConfig, CoordinatorCluster, to_coord_job
from octopus.crypto import generate_signing_key, sign
from octopus.policy import JobContext
from octopus.proto import COORD_TOPIC, CoordMessage
from octopus.transport import MemoryBus, MemoryTransport


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def now(self):
        return self.t


class Node:
    def __init__(self, cid, key, peers, bus, clock, config):
        self.adopted: list[str] = []
        self.cancelled: list[str] = []
        self.transport = MemoryTransport(bus)
        self.cluster = CoordinatorCluster(
            cid,
            key,
            peers,
            self.transport,
            lambda j: self.adopted.append(j.job_id),
            clock,
            config,
            cancel=lambda job_id, detail: self.cancelled.append(job_id),
        )


@pytest.fixture
def clock():
    return Clock()


def make(clock, n=3, **cfg):
    bus = MemoryBus()
    keys = {f"c{i}": generate_signing_key() for i in range(n)}
    pubs = {c: k.public_key() for c, k in keys.items()}
    config = ClusterConfig(**{"heartbeat_s": 1.0, "dead_after_s": 3.0, **cfg})
    nodes = [Node(c, k, pubs, bus, clock, config) for c, k in keys.items()]
    for node in nodes:
        node.cluster.start()
    return bus, keys, nodes


def job(i):
    ctx = JobContext(repo="acme/app", head_repo="acme/app", sender_is_org_member=True, event="push")
    return to_coord_job(str(i), "acme/app", ["self-hosted", "octopus"], ctx)


def test_all_coordinators_agree_on_one_owner(clock):
    _, _, nodes = make(clock)
    owners = {n.cluster.owner("42") for n in nodes}
    assert len(owners) == 1


def test_exactly_one_coordinator_adopts_a_submitted_job(clock):
    _, _, nodes = make(clock)
    nodes[0].cluster.submit(job(1))
    assert sum(n.adopted == ["1"] for n in nodes) == 1
    assert sum(len(n.adopted) for n in nodes) == 1
    assert all(n.cluster.pending() == ["1"] for n in nodes)


def test_jobs_spread_over_coordinators(clock):
    _, _, nodes = make(clock)
    for i in range(30):
        nodes[0].cluster.submit(job(i))
    assert sum(len(n.adopted) for n in nodes) == 30
    assert all(n.adopted for n in nodes)


def test_dead_owner_is_replaced_and_the_job_adopted_once(clock):
    _, _, nodes = make(clock)
    nodes[0].cluster.submit(job(1))
    owner = next(n for n in nodes if n.adopted)
    survivors = [n for n in nodes if n is not owner]
    clock.t += 10  # the owner stops heartbeating
    for n in survivors:
        n.cluster.tick()
    assert sum(n.adopted == ["1"] for n in survivors) == 1


def test_accepted_job_is_forgotten_and_never_taken_over(clock):
    _, _, nodes = make(clock)
    nodes[0].cluster.submit(job(1))
    owner = next(n for n in nodes if n.adopted)
    owner.cluster.accepted("1")
    assert all(n.cluster.pending() == [] for n in nodes)
    clock.t += 10
    survivors = [n for n in nodes if n is not owner]
    for n in survivors:
        n.cluster.tick()
    assert all(n.adopted == [] for n in survivors)
    nodes[0].cluster.submit(job(1))  # a repeated webhook does not resurrect it
    assert all(n.cluster.pending() == [] for n in nodes)


def test_origin_repeats_unaccepted_jobs_so_a_healed_partition_converges(clock):
    _, _, nodes = make(clock, n=2)
    late = nodes[1]
    late.cluster.stop()  # partitioned: misses the announcement
    nodes[0].cluster.submit(job(1))
    assert late.cluster.pending() == []
    late.cluster.start()  # partition heals
    clock.t += 2
    nodes[0].cluster.tick()
    assert late.cluster.pending() == ["1"]


def send(bus, cid, key, clock, **fields):
    msg = CoordMessage(coordinator_id=cid, sent_at=clock.now(), **fields)
    msg.sig = sign(key, msg.signing_bytes())
    MemoryTransport(bus).publish(COORD_TOPIC, msg.model_dump_json().encode())


def test_unsigned_and_forged_messages_are_ignored(clock):
    bus, _, nodes = make(clock, n=2)
    victim = nodes[0]
    # signed with the wrong key
    send(bus, "c1", generate_signing_key(), clock, kind="queued", job_id="9", job=job(9))
    # a stranger that is not a pinned coordinator
    send(bus, "mallory", generate_signing_key(), clock, kind="queued", job_id="9", job=job(9))
    # unsigned
    raw = CoordMessage(kind="queued", coordinator_id="c1", sent_at=clock.now(), job=job(9))
    MemoryTransport(bus).publish(COORD_TOPIC, raw.model_dump_json().encode())
    MemoryTransport(bus).publish(COORD_TOPIC, b"not json")
    assert victim.cluster.pending() == [] and victim.adopted == []


def test_stale_messages_are_dropped(clock):
    bus, keys, nodes = make(clock, n=2)
    old = Clock()
    old.t = clock.t - 3600
    send(bus, "c1", keys["c1"], old, kind="queued", job_id="9", job=job(9))
    assert nodes[0].cluster.pending() == []


def test_authentic_cancel_from_a_peer_drops_the_replica_but_a_stranger_cannot(clock):
    bus, keys, nodes = make(clock, n=2)
    nodes[0].cluster.submit(job(1))
    send(bus, "mallory", generate_signing_key(), clock, kind="done", job_id="1")
    assert "1" in nodes[1].cluster.pending()
    send(bus, "c0", keys["c0"], clock, kind="done", job_id="1")
    assert nodes[1].cluster.pending() == []


def gossip(bus):
    """The kinds of all coordinator messages sent from now on."""
    kinds: list[str] = []
    MemoryTransport(bus).subscribe(
        COORD_TOPIC, lambda topic, payload: kinds.append(CoordMessage.model_validate_json(payload).kind)
    )
    return kinds


def test_cancel_received_by_a_peer_reaches_the_owner(clock):
    _, _, nodes = make(clock)
    nodes[0].cluster.submit(job(1))
    owner = next(n for n in nodes if n.adopted)
    other = next(n for n in nodes if n is not owner)
    other.cluster.cancel("1", "GitHub completed the job before it ran: cancelled")
    assert owner.cancelled == ["1"]
    assert all(n.cluster.pending() == [] for n in nodes)
    clock.t += 10  # the owner dies: nobody takes the cancelled job over
    for n in nodes:
        if n is not owner:
            n.cluster.tick()
    assert sum(len(n.adopted) for n in nodes) == 1


def test_cancel_before_the_job_is_announced_wins(clock):
    _, _, nodes = make(clock)
    nodes[1].cluster.cancel("1", "GitHub completed the job before it ran: cancelled")
    nodes[0].cluster.submit(job(1))  # the queued webhook was slower than the completed one
    assert all(n.adopted == [] and n.cluster.pending() == [] for n in nodes)


def test_completion_of_a_job_that_ran_sends_no_cancel(clock):
    bus, _, nodes = make(clock)
    nodes[0].cluster.submit(job(1))
    owner = next(n for n in nodes if n.adopted)
    owner.cluster.accepted("1")
    kinds = gossip(bus)
    for n in nodes:
        n.cluster.cancel("1", "GitHub completed the job before it ran: success")
    assert "cancelled" not in kinds


def test_a_stranger_cannot_cancel(clock):
    bus, _, nodes = make(clock, n=2)
    nodes[0].cluster.submit(job(1))
    send(bus, "mallory", generate_signing_key(), clock, kind="cancelled", job_id="1")
    assert all(n.cancelled == [] and n.cluster.pending() == ["1"] for n in nodes)
