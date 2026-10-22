"""A member list only speaks for the repos inside its scope."""

import httpx
import pytest
from _ci_harness import REPO, World
from conftest import SECRET  # noqa: F401  (keeps the rootdir conftest imported first)

from octopus.ci.messages import score
from octopus.team import TeamState, make_member_list
from octopus.team.state import KEYS_RETRY_S, scope_covers
from octopus.transport import MemoryTransport

TOPIC = "/octopus/1/ci/proto"


@pytest.mark.parametrize(
    ("scope", "repo", "covers"),
    [
        ("org:acme", "acme/app", True),
        ("org:ACME", "acme/app", True),
        ("org:acme", "other/app", False),
        ("org:acme", "acme-evil/app", False),
        ("repo:acme/app", "acme/app", True),
        ("repo:acme/app", "acme/other", False),
        ("team:acme", "acme/app", False),
        ("org:acme", "", False),
    ],
)
def test_scope_covers(scope, repo, covers):
    assert scope_covers(scope, repo) is covers


def test_membership_is_checked_against_the_repo_of_the_leg():
    world = World()
    world.member_lists.append(
        make_member_list(world.team_key, "org:other", 1, ["mallory"], world.clock.now())
    )
    state = TeamState(world.team_key.public_key(), world.keys_for, world.clock.now)
    for ml in world.member_lists:
        state.accept_list(ml)
    assert state.is_member_login("mallory") and state.is_member_login("mallory", "other/app")
    assert not state.is_member_login("mallory", REPO)
    assert state.is_member_login("alice", REPO) and not state.is_member_login("alice", "other/app")


def mallory_world():
    """Alice is a member of acme; mallory only of org:other (as if a leaked delegated key of
    that scope had put her there)."""
    world = World()
    world.member_lists.append(
        make_member_list(world.team_key, "org:other", 1, ["mallory"], world.clock.now())
    )
    return world


def test_a_member_of_another_scope_cannot_take_or_block_a_leg(world=None):
    world = mallory_world()
    alice = world.add("alice")
    mallory = world.add("mallory")
    world.settle()
    # a run where mallory would win the rank if she counted
    run_id = next(
        str(i)
        for i in range(500)
        if score(f"{REPO}:{i}:1:linux", mallory.node_id) < score(f"{REPO}:{i}:1:linux", alice.node_id)
    )
    _, payload, _ = world.request(run_id=run_id)
    mallory.frozen = True  # she claims and leases nothing that alice's node would believe anyway
    world.publish(payload)
    world.advance(10)
    assert len(alice.executor.runs) == 1
    assert mallory.node_id in alice.node._peers  # her announce was heard...
    leg = alice.node._legs[f"{REPO}:{run_id}:1:linux"]
    assert leg.best is None or leg.best[1] == alice.node_id  # ...but her claims never counted


def test_a_lease_from_another_scope_does_not_make_the_submitter_think_it_is_handled():
    from test_ci_submit import legs_for, make_submitter

    world = mallory_world()
    alice = world.add("alice")
    mallory = world.add("mallory")
    world.settle()
    alice.frozen = True
    legs = legs_for(world, "linux")
    forged = mallory.node._signed("lease", leg_id=legs[0].leg_id, epoch=1)
    world.bus.deliver(TOPIC, forged.model_dump_json().encode())
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs)
    assert result.handled == {} and result.unhandled == ["linux"]
    # mallory's idle announce is not an idle node for this repo: it gives up early, not at 45 s
    assert world.clock.t - started < 40


# the keys of a member


class Flaky:
    def __init__(self, keys):
        self.keys = keys
        self.fail = False
        self.calls = 0

    def __call__(self, login):
        self.calls += 1
        if self.fail:
            raise httpx.HTTPStatusError("429", request=None, response=None)  # type: ignore[arg-type]
        return self.keys


def test_an_outage_of_github_keeps_the_keys_held_and_retries_soon():
    world = World()
    source = Flaky([b"key"])
    state = TeamState(world.team_key.public_key(), source, world.clock.now)
    assert state._login_keys("alice") == [b"key"]
    world.clock.t += 7200  # the cache is stale (the Clock here only moves monotonic time)
    state._now = lambda: world.clock.now() + 7200
    source.fail = True
    assert state._login_keys("alice") == [b"key"]  # not []
    calls = source.calls
    assert state._login_keys("alice") == [b"key"] and source.calls == calls  # retried later
    state._now = lambda: world.clock.now() + 7200 + KEYS_RETRY_S + 1
    state._login_keys("alice")
    assert source.calls == calls + 1


def test_an_outage_with_nothing_cached_does_not_cache_no_keys():
    world = World()
    source = Flaky([b"key"])
    source.fail = True
    state = TeamState(world.team_key.public_key(), source, world.clock.now)
    assert state._login_keys("alice") == []
    source.fail = False
    assert state._login_keys("alice") == [b"key"]  # tried again at once, not an hour later


def test_fetch_github_keys_raises_for_errors_other_than_not_found(monkeypatch):
    from octopus.team import state as team_state

    def fake_get(status):
        return lambda url, **kw: httpx.Response(status, text="", request=httpx.Request("GET", url))

    monkeypatch.setattr(team_state.httpx, "get", fake_get(404))
    assert team_state.fetch_github_keys("ghost") == []
    for status in (429, 500, 503):
        monkeypatch.setattr(team_state.httpx, "get", fake_get(status))
        with pytest.raises(httpx.HTTPStatusError):
            team_state.fetch_github_keys("alice")
    assert MemoryTransport is not None
