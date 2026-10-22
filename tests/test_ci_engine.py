import json

import pytest
from _ci_harness import PIN, REPO, SHA, World

from octopus.ci.messages import channel_id, leg_topic, score
from octopus.crypto import PairCipher, derive_channel_key, encode_public_key


@pytest.fixture
def world():
    return World()


def runs(*machines):
    return [len(m.executor.runs) for m in machines]


def test_a_lone_node_claims_waits_the_grace_and_runs(world):
    a = world.add("alice")
    world.settle()
    leg_id, payload, _ = world.request()
    world.publish(payload)
    assert a.node.leg_state(leg_id) == "waiting"
    world.advance(0.5)
    assert a.node.leg_state(leg_id) == "claiming"
    assert runs(a) == [0]  # held for the grace period first
    world.advance(1.5)
    assert runs(a) == [1]
    leg = a.executor.runs[0]
    assert (leg.repo, leg.sha, leg.os) == (REPO, SHA, "linux")
    assert [s for _, s in a.status.posts] == ["pending", "success"]
    assert a.node.leg_state(leg_id) == "closed"


def test_failure_and_executor_errors_are_reported(world):
    a = world.add("alice")
    a.executor.exit_code = 3
    world.settle()
    _, payload, _ = world.request()
    world.publish(payload)
    world.advance(3)
    assert [s for _, s in a.status.posts] == ["pending", "failure"]


def test_with_three_nodes_exactly_one_runs_the_lowest_score(world):
    machines = [world.add(n) for n in ("alice", "bob", "carol")]
    world.settle()
    leg_id, payload, _ = world.request()
    world.publish(payload)
    world.advance(10)
    assert sum(runs(*machines)) == 1
    winner = min(machines, key=lambda m: score(leg_id, m.node_id))
    assert runs(winner) == [1]


def test_only_a_node_of_the_right_os_runs_it(world):
    linux = world.add("alice", "linux")
    mac = world.add("bob", "macos")
    world.settle()
    _, payload, _ = world.request("macos")
    world.publish(payload)
    world.advance(5)
    assert runs(linux, mac) == [0, 1]


def test_two_legs_of_a_run_go_to_the_nodes_of_their_os(world):
    linux = world.add("alice", "linux")
    mac = world.add("bob", "macos")
    world.settle()
    for os_name in ("linux", "macos"):
        world.publish(world.request(os_name)[1])
    world.advance(5)
    assert runs(linux, mac) == [1, 1]


TOPIC = "/octopus/1/ci/proto"


def spy_on(world):
    heard: list[bytes] = []
    from octopus.transport import MemoryTransport

    MemoryTransport(world.bus).subscribe(TOPIC, lambda _t, p: heard.append(p))
    return heard


def test_simultaneous_claims_resolve_to_one_runner(world):
    a, b = world.add("alice"), world.add("bob")
    heard = spy_on(world)
    for m in (a, b):
        m.transport.unsubscribe(TOPIC)  # deaf while both claim
    leg_id, payload, _ = world.request()
    a.node.handle(TOPIC, payload)
    b.node.handle(TOPIC, payload)
    world.advance(0.5)  # both claim at once, neither hears the other
    assert a.node.leg_state(leg_id) == b.node.leg_state(leg_id) == "claiming"
    claims = [p for p in heard if b'"claim"' in p]
    assert len(claims) == 2
    for p in claims:  # now the claims cross
        a.node.handle(TOPIC, p)
        b.node.handle(TOPIC, p)
    winner = min((a, b), key=lambda m: score(leg_id, m.node_id))
    loser = b if winner is a else a
    assert winner.node.leg_state(leg_id) == "claiming"
    assert loser.node.leg_state(leg_id) == "waiting"
    a.start()
    b.start()
    world.advance(10)
    assert runs(winner) == [1] and runs(loser) == [0]


def test_a_busy_node_declines_and_the_next_one_runs(world):
    a = world.add("alice", hold=True)  # its runs stay in progress
    b = world.add("bob")
    world.settle()
    # a run that a wins, so that a is busy
    run_id = next(
        str(i)
        for i in range(100)
        if score(f"{REPO}:{i}:1:linux", a.node_id) < score(f"{REPO}:{i}:1:linux", b.node_id)
    )
    _, p1, _ = world.request(run_id=run_id)
    world.publish(p1)
    world.advance(5)
    assert a.node.busy() == 1 and runs(b) == [0]
    # a second run that a would win again must go to b, because a is busy
    second = next(
        str(i)
        for i in range(100, 200)
        if score(f"{REPO}:{i}:1:linux", a.node_id) < score(f"{REPO}:{i}:1:linux", b.node_id)
    )
    _, p2, _ = world.request(run_id=second)
    world.publish(p2)
    world.advance(10)
    assert runs(b) == [1]


def test_takeover_when_the_owner_goes_silent_and_the_old_owner_is_fenced(world):
    a = world.add("alice", hold=True)
    b = world.add("bob", hold=True)
    world.settle()
    leg_id, payload, _ = world.request()
    world.publish(payload)
    world.advance(5)
    owner = a if a.held else b
    other = b if owner is a else a
    assert owner.node.leg_state(leg_id) == "running" and not other.held
    owner.frozen = True  # the laptop sleeps: no ticks, no leases (but it still hears)
    world.advance(20)
    assert not other.held, "a live lease must not be taken over"
    world.advance(30)
    assert other.held, "the next node took over after the lease lapsed"
    assert other.node.leg_state(leg_id) == "running"
    other.held.pop()()  # the new owner finishes
    assert [s for _, s in other.status.posts] == ["pending", "success"]
    # the old owner wakes up having heard the higher epoch: it must not post a final status
    owner.held.pop()()
    assert [s for _, s in owner.status.posts] == ["pending"]
    assert owner.node.leg_state(leg_id) == "yielded"


def test_a_duplicate_run_is_stopped_by_the_better_lease(world):
    a = world.add("alice", hold=True)
    b = world.add("bob", hold=True)
    leg_id, payload, _ = world.request()
    for m in (a, b):
        m.transport.unsubscribe(TOPIC)  # the claims are lost: a partition
        m.node.handle(TOPIC, payload)
    world.advance(3)
    assert len(a.held) == 1 and len(b.held) == 1, "both ran the same leg"
    a.start()
    b.start()  # the partition heals
    world.advance(12)  # the next leases cross
    winner = min((a, b), key=lambda m: score(leg_id, m.node_id))
    loser = b if winner is a else a
    assert winner.node.leg_state(leg_id) == "running"
    assert loser.node.leg_state(leg_id) == "yielded"
    loser.held.pop()()
    assert [s for _, s in loser.status.posts] == ["pending"]  # no final status from the loser
    winner.held.pop()()
    assert [s for _, s in winner.status.posts] == ["pending", "success"]


# trust


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": "evil/app"},
        {"event_name": "pull_request_target"},
        {"job_workflow_ref": "evil/repo/.github/workflows/ci.yml@refs/heads/main"},
        {"sha": ""},
    ],
)
def test_untrusted_requests_are_ignored(world, overrides):
    a = world.add("alice")
    world.settle()
    _, payload, _ = world.request(**overrides)
    world.publish(payload)
    world.advance(10)
    assert runs(a) == [0]


def test_a_replayed_request_runs_once(world):
    a = world.add("alice")
    world.settle()
    _, payload, _ = world.request()
    for _ in range(3):
        world.publish(payload)
        world.advance(0.5)
    world.advance(5)
    assert runs(a) == [1]


def test_a_token_for_another_os_cannot_be_replayed_for_this_one(world):
    a = world.add("alice", "linux")
    world.settle()
    # the audience binds the OS: a macos token reaches a linux node as a macos leg and is ignored
    _, payload, _ = world.request("macos")
    world.publish(payload)
    world.advance(5)
    assert runs(a) == [0]


def test_a_node_of_a_login_not_on_the_list_is_ignored(world):
    a = world.add("alice")
    mallory = world.add("mallory")  # not in the member list
    world.settle()
    assert mallory.node_id not in a.node._peers
    assert a.node_id in a.node._peers
    leg_id, payload, _ = world.request()
    # mallory would win the rank if it counted: make sure it is the better scorer for this test
    if score(leg_id, mallory.node_id) > score(leg_id, a.node_id):
        leg_id, payload, _ = world.request(run_id="78")
    world.publish(payload)
    world.advance(10)
    assert runs(a) == [1]


def test_forged_messages_are_dropped(world):
    a = world.add("alice")
    world.settle()
    leg_id, payload, _ = world.request()
    world.publish(payload)
    world.advance(0.5)
    forged = {
        "kind": "claim",
        "node_id": a.node_id,
        "proof": json.loads(a.node._proof.model_dump_json()),
        "sent_at": 1.0,
        "leg_id": leg_id,
        "epoch": 99,
        "sig": "AAAA",
    }
    world.publish(json.dumps(forged).encode())
    world.advance(5)
    assert runs(a) == [1]
    assert a.node._legs[leg_id].max_epoch == 1  # the epoch-99 claim was not believed


# replies to the submitter over the reliable channel


def test_logs_and_the_result_reach_the_submitter_on_an_encrypted_channel(world):
    a = world.add("alice")
    world.settle()
    leg_id, payload, reply_key = world.request()
    got: list[dict] = []
    chan = channel_id(leg_id, a.node_id)
    cipher = PairCipher(derive_channel_key(reply_key, a.enc.public_key(), chan))
    sub = world.machines[0].transport.__class__(world.bus)
    sub.open_channel(
        chan, leg_topic(leg_id), "submitter", cipher, lambda _s, p: got.append(json.loads(p))
    )
    world.publish(payload)
    world.advance(5)
    assert [m["t"] for m in got] == ["log", "log", "result"]
    assert got[-1]["state"] == "success"
    assert encode_public_key(a.enc.public_key()) not in str(world.bus.wire[-1])
    assert PIN.startswith("logos-messaging")
