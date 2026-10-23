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


# a clear output: which machine, how long, what ended how


def _taken(world, login="alice", name_of=None, exit_code=0):
    a = world.add(login)
    a.executor.exit_code = exit_code
    world.settle()
    sub = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    lines = []
    sub.follow(result, lambda o, line: lines.append((o, line)), timeout_s=60)
    return result, lines


def test_the_submitter_knows_the_machine_by_name_and_owner(world):
    result, _ = _taken(world)
    assert result.names["linux"] == "alice" and result.logins["linux"] == "alice"
    from octopus.ci.summary import machine

    assert machine(result, "linux") == "alice (alice)"


def test_the_result_message_says_how_long_the_script_ran(world):
    result, _ = _taken(world)
    seconds = result.outcomes["linux"]["seconds"]
    assert isinstance(seconds, float) and seconds >= 0


def test_the_log_block_names_the_machine_the_state_and_the_exit_code(world):
    from octopus.ci.summary import render_text

    result, _ = _taken(world, exit_code=3)
    result.outcomes["linux"]["seconds"] = 104.0
    text = render_text(["linux", "windows"], result)
    lines = text.splitlines()
    assert lines[0] == "Octopus CI result"
    assert "linux" in lines[1] and "failure (exit code 3)" in lines[1]
    assert "alice (alice)" in lines[1] and "1m44s" in lines[1]
    assert "windows" in lines[2] and "GitHub" in lines[2] and "hosted runner" in lines[2]


def test_the_log_block_says_when_no_log_came_from_the_machine():
    from octopus.ci.summary import render_text

    result = SubmitResult(
        handled={"linux": "n1"},
        names={"linux": "ces"},
        logins={"linux": "bot"},
        outcomes={"linux": {"state": "success", "exit": 0, "seconds": 61.0}},
    )
    text = render_text(["linux"], result)
    assert "ces (bot)" in text and "1m01s" in text and "[log not received]" in text
    result.lines["linux"] = 40
    assert "[log not received]" not in render_text(["linux"], result)


def test_the_markdown_table_shows_the_machine_owner_and_duration():
    result = SubmitResult(
        handled={"macos": "jqf7sjhugxa7j7ux"},
        names={"macos": "Ivans-MacBook"},
        logins={"macos": "ivansete-status"},
        outcomes={"macos": {"state": "success", "exit": 0, "seconds": 182.0}},
    )
    text = render(["macos"], result)
    assert "**Ivans-MacBook** (ivansete-status)" in text and "in 3m02s" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert all(ln.count("|") == 4 for ln in rows)


def test_a_name_with_markup_cannot_break_the_log_block():
    from octopus.ci.summary import render_text

    result = SubmitResult(handled={"linux": "n1"}, names={"linux": "evil|name\n`<x>`"})
    text = render_text(["linux"], result)
    assert "\n`" not in text and "|" not in text and len(text.splitlines()) == 2


def test_the_duration_is_short_and_absent_when_unknown():
    from octopus.ci.summary import duration

    assert duration(0.4) == "0s" and duration(59.6) == "1m00s" and duration(104) == "1m44s"
    assert duration(None) == "" and duration(-1) == "" and duration("x") == ""


# the job summary of the run page

CTX = {
    "repo": "acme/app",
    "sha": "b638772" + "0" * 33,
    "run": "123",
    "attempt": "2",
    "server": "https://github.com",
}


def _ok_result():
    return SubmitResult(
        handled={"linux": "n1", "macos": "n2"},
        names={"linux": "Fabiana VPS", "macos": "Ivan laptop"},
        logins={"linux": "bot", "macos": "ivan"},
        outcomes={
            "linux": {"state": "success", "exit": 0, "seconds": 106.0},
            "macos": {"state": "success", "exit": 0, "seconds": 192.0},
        },
    )


def test_the_summary_says_first_that_everything_on_team_machines_passed():
    text = render(["linux", "macos"], _ok_result(), context=CTX, hosted=["windows"])
    lines = text.splitlines()
    assert lines[0] == "### Octopus CI"
    assert lines[2] == "✅ every leg on a team machine passed"
    assert "`acme/app` @ `b638772`" in text
    assert "[run 123](https://github.com/acme/app/actions/runs/123), attempt 2" in text
    assert "| ✅ linux | **Fabiana VPS** (bot)" in text and "**success** in 1m46s" in text
    assert "| ✅ macos | **Ivan laptop** (ivan)" in text and "in 3m12s" in text
    assert "| ☁️ windows | GitHub-hosted runner | always runs on GitHub |" in text
    assert "**Team machines:** 2 of 2 legs passed" in text
    assert "**always on GitHub:** windows" in text


def test_the_summary_shows_a_failed_leg_and_a_fallback_in_the_verdict_and_the_tally():
    result = SubmitResult(
        handled={"linux": "n1"},
        names={"linux": "Fabiana VPS"},
        logins={"linux": "bot"},
        outcomes={"linux": {"state": "failure", "exit": 1, "seconds": 51.0}},
        unhandled=["macos"],
        reasons={"macos": "no team machine of this OS was online"},
    )
    text = render(["linux", "macos"], result, context=CTX)
    assert "❌ a leg failed on a team machine" in text
    assert "| ❌ linux |" in text and "**failure (exit code 1)** in 51s" in text
    assert "| 🔁 macos | GitHub-hosted runner | no team machine of this OS was online |" in text
    assert "0 of 2 legs passed, 1 failed" in text and "**fell back to GitHub:** 1" in text


def test_the_summary_warns_when_nothing_ran_on_a_team_machine():
    result = SubmitResult(unhandled=["linux"], reasons={"linux": "no answer from the fleet"})
    text = render(["linux"], result, context=CTX)
    assert "⚠️ no team machine took a leg: everything runs on GitHub" in text
    assert "**Team machines:** 0 of 1 legs passed" in text


def test_the_summary_without_the_run_context_still_renders():
    text = render(["linux", "macos"], _ok_result())
    assert text.startswith("### Octopus CI\n\n✅") and "actions/runs" not in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert len(rows) == 4 and all(ln.count("|") == 4 for ln in rows)


def test_the_run_context_cannot_inject_markup_into_the_summary():
    ctx = {"repo": "a/b](http://evil) x", "sha": "abc`def", "run": "1)\n#", "attempt": "2"}
    text = render(["linux"], _ok_result(), context=ctx)
    head = text.split("| Leg |")[0]
    assert head.count("](") == 1, "only the link to the run"
    assert "](http://evil" not in head
    assert head.count("`") == 4, "two code spans: the repo and the commit"
    assert "\n#" not in head.split("### Octopus CI", 1)[1]


def test_the_summary_file_is_written_as_utf8_with_the_icons(tmp_path, monkeypatch):
    """Windows writes text as cp1252 unless told otherwise, and the icons then raise."""
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))
    cli_ci._write_summary(render(["linux"], _ok_result(), context=CTX, hosted=["windows"]))
    raw = out.read_bytes()
    assert "✅".encode() in raw and "☁️".encode() in raw
    assert b"\r\n" not in raw, "GitHub reads \\n line ends"
    assert raw.decode("utf-8").startswith("### Octopus CI\n")
