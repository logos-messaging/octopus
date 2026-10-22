"""The submit job stays until the legs a team machine took end, and says how each ended."""

import json

import pytest
from _ci_harness import REPO, World

from octopus import cli_ci
from octopus.ci.submit import Leg, SubmitResult, Submitter
from octopus.ci.summary import render
from octopus.team import TeamState
from octopus.transport import MemoryTransport


@pytest.fixture
def world():
    return World()


def make_submitter(world, **kw):
    team = TeamState(world.team_key.public_key(), world.keys_for, world.clock.now)
    return Submitter(
        MemoryTransport(world.bus),
        team,
        sleep=lambda _s: None,
        monotonic=world.clock.monotonic,
        pump=lambda: world.advance(0.5),
        **kw,
    )


def legs_for(world, *oses):
    out = []
    for os_name in oses:
        leg_id, payload, reply = world.request(os_name)
        token = json.loads(payload)["token"]
        out.append(Leg(os=os_name, leg_id=leg_id, token=token, reply_key=reply, repo=REPO))
    return out


def test_a_failed_leg_is_reported_with_its_exit_code(world):
    a = world.add("alice")
    a.executor.exit_code = 3
    world.settle()
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    lines = []
    sub.follow(result, lambda o, line: lines.append((o, line)), timeout_s=60)
    assert result.outcomes["linux"]["state"] == "failure"
    assert result.outcomes["linux"]["exit"] == 3
    assert lines == [("linux", "building"), ("linux", "done")]
    assert cli_ci._settle(result) == ["linux"], "a failed team leg fails the job"
    assert result.unhandled == [], "and does not run again on GitHub"


def test_a_leg_that_passed_is_neither_failed_nor_sent_to_github(world):
    world.add("alice")
    world.settle()
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    sub.follow(result, lambda *_: None, timeout_s=60)
    assert result.outcomes["linux"]["state"] == "success"
    assert cli_ci._settle(result) == [] and result.unhandled == []
    assert result.handled


def test_a_silent_machine_is_lost_and_its_leg_falls_back_to_github(world):
    a = world.add("alice", hold=True)
    world.settle()
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    assert "linux" in result.handled
    a.frozen = True  # the machine dies: no log line, no lease
    sub.follow(result, lambda *_: None, timeout_s=3600, silent_s=30)
    assert result.outcomes["linux"]["state"] == "lost"
    assert cli_ci._settle(result) == [], "a lost machine is not a failure of the leg"
    assert result.unhandled == ["linux"] and "linux" not in result.handled
    assert "said nothing" in result.reasons["linux"]


def test_a_machine_that_keeps_leasing_is_not_lost_but_times_out(world):
    world.add("alice", hold=True)  # the leg starts and never ends; the node keeps leasing
    world.settle()
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    sub.follow(result, lambda *_: None, timeout_s=120, silent_s=30)
    assert result.outcomes["linux"]["state"] == "timeout"
    assert cli_ci._settle(result) == ["linux"]


def test_a_status_that_cannot_be_posted_does_not_stop_the_leg(world, monkeypatch):
    a = world.add("alice")
    world.settle()

    def no_write_access(*_a):
        raise RuntimeError("could not post the commit status: exit code: 1")

    monkeypatch.setattr(a.status, "post", no_write_access)
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    sub.follow(result, lambda *_: None, timeout_s=60)
    assert len(a.executor.runs) == 1, "the script ran although no status could be posted"
    assert result.outcomes["linux"]["state"] == "success"


def test_a_cancelled_leg_is_neither_failed_nor_sent_to_github():
    result = SubmitResult(
        handled={"linux": "n1"},
        outcomes={"linux": {"state": "cancelled", "exit": -1, "detail": "the run was cancelled"}},
    )
    assert cli_ci._settle(result) == [] and result.unhandled == []


def test_the_summary_says_how_each_leg_ended():
    result = SubmitResult(
        handled={"linux": "abc", "macos": "def"},
        names={"linux": "vps", "macos": "laptop"},
        outcomes={
            "linux": {"state": "failure", "exit": 3, "detail": ""},
            "macos": {"state": "success", "exit": 0, "detail": ""},
        },
        unhandled=["windows"],
        reasons={"windows": "no team machine of this OS was online"},
    )
    text = render(["linux", "macos", "windows"], result)
    assert "**failure (exit code 3)**" in text
    assert "**success**" in text
    assert "GitHub-hosted runner | no team machine of this OS was online |" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert all(ln.count("|") == 4 for ln in rows)


def test_outputs_include_the_failed_legs(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    cli_ci._write_outputs(["linux"], ["macos"], ["linux"])
    written = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert json.loads(written["failed"]) == ["linux"]
    assert json.loads(written["unhandled"]) == ["macos"]
