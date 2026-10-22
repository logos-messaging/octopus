import json
import stat
import subprocess
import sys

import pytest
from _ci_harness import REPO, SHA, World

from octopus.ci.engine import LegInfo
from octopus.ci.exec import GhRunProbe, GhStatusPoster, GitExecutor
from octopus.ci.messages import make_leg_id
from octopus.ci.submit import Leg, Submitter
from octopus.team import TeamState
from octopus.transport import MemoryTransport


def make_submitter(world, **kw):
    team = TeamState(world.team_key.public_key(), world.keys_for, world.clock.now)  # no list yet
    sub = Submitter(
        MemoryTransport(world.bus),
        team,
        sleep=lambda _s: None,
        monotonic=world.clock.monotonic,
        pump=lambda: world.advance(0.5),
        **kw,
    )
    return sub, team


def legs_for(world, *oses):
    out = []
    for os_name in oses:
        leg_id, payload, reply = world.request(os_name)
        token = json.loads(payload)["token"]
        out.append(Leg(os=os_name, leg_id=leg_id, token=token, reply_key=reply, repo=REPO))
    return out


@pytest.fixture
def world():
    return World()


def test_legs_taken_by_the_fleet_are_handled_and_the_rest_fall_back(world):
    linux = world.add("alice", "linux")
    mac = world.add("bob", "macos")
    world.settle()
    sub, _ = make_submitter(world)
    result = sub.submit(legs_for(world, "linux", "macos", "windows"))
    assert result.handled == {"linux": linux.node_id, "macos": mac.node_id}
    assert result.unhandled == ["windows"]  # no node: hosted fallback
    assert len(linux.executor.runs) == 1 and len(mac.executor.runs) == 1


def test_no_fleet_means_everything_falls_back(world):
    sub, _ = make_submitter(world, list_wait_s=3)
    result = sub.submit(legs_for(world, "linux", "macos"))
    assert result.handled == {} and result.unhandled == ["linux", "macos"]


def test_the_submitter_learns_the_members_from_the_fleet(world):
    world.add("alice")
    world.settle()
    sub, team = make_submitter(world)
    assert team.current() == []
    sub.submit(legs_for(world, "linux"))
    assert [ml.logins for ml in team.current()] == [["alice", "bob", "carol"]]


def test_a_lease_forged_by_a_stranger_does_not_count(world):
    a = world.add("alice")
    world.settle()
    sub, _ = make_submitter(world, wait_s=5)
    mallory = world.add("mallory")  # not a member: its messages carry no valid proof
    legs = legs_for(world, "linux")
    # mallory forges a lease for the leg
    forged = mallory.node._signed("lease", leg_id=legs[0].leg_id, epoch=1)
    world.bus.deliver("/octopus/1/ci/proto", forged.model_dump_json().encode())
    a.frozen = True  # the real node stays silent
    result = sub.submit(legs)
    assert result.handled == {} and result.unhandled == ["linux"]


def test_follow_replays_the_log_and_the_result_over_the_channel(world):
    world.add("alice")
    world.settle()
    sub, _ = make_submitter(world)
    result = sub.submit(legs_for(world, "linux"))
    lines = []
    results = sub.follow(result, lambda os_name, line: lines.append((os_name, line)), timeout_s=5)
    assert lines == [("linux", "building"), ("linux", "done")]
    assert results["linux"]["state"] == "success"


# executor and status poster


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "origin"
    src.mkdir()
    run(["git", "init", "-q", "-b", "main"], src)
    run(["git", "config", "user.email", "t@example.org"], src)
    run(["git", "config", "user.name", "t"], src)
    (src / ".octopus").mkdir()
    (src / ".octopus" / "ci.sh").write_text('echo "sha=$OCTOPUS_SHA"\necho "os=$OCTOPUS_OS"\nexit 3\n')
    run(["git", "add", "."], src)
    run(["git", "commit", "-q", "-m", "ci"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], src)
    return src, sha


def leg_info(repo_path, sha, os_name="linux"):
    return LegInfo(
        leg_id=make_leg_id(repo_path, "1", "1", os_name),
        repo=repo_path,
        sha=sha,
        ref="refs/heads/main",
        event="push",
        run_id="1",
        attempt="1",
        os=os_name,
        reply_pub="x",
        workflow="w",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="CI nodes run on Linux and macOS only")
def test_git_executor_checks_out_the_commit_and_runs_the_script(repo, tmp_path):
    src, sha = repo
    lines = []
    ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.01)
    result = ex.run(leg_info(str(src), sha), lines.append, lambda: False)
    assert result.exit_code == 3
    assert f"sha={sha}" in lines and "os=linux" in lines
    assert list((tmp_path / "work").iterdir()) == []  # the checkout is deleted


def test_git_executor_without_a_script_or_commit(repo, tmp_path):
    src, _sha = repo
    ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.01)
    assert ex.run(leg_info(str(src), "0" * 40), lambda _l: None, lambda: False).exit_code != 0


@pytest.mark.skipif(sys.platform == "win32", reason="CI nodes run on Linux and macOS only")
def test_git_executor_stops_a_running_script(tmp_path):
    src = tmp_path / "o"
    src.mkdir()
    run(["git", "init", "-q", "-b", "main"], src)
    run(["git", "config", "user.email", "t@example.org"], src)
    run(["git", "config", "user.name", "t"], src)
    (src / ".octopus").mkdir()
    (src / ".octopus" / "ci.sh").write_text("sleep 60\n")
    run(["git", "add", "."], src)
    run(["git", "commit", "-q", "-m", "x"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.01)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 200  # after the checkout, once the script runs

    result = ex.run(leg_info(str(src), sha), lambda _l: None, should_stop)
    assert result.exit_code == 143 and result.detail == "stopped"


@pytest.mark.skipif(sys.platform == "win32", reason="CI nodes run on Linux and macOS only")
def test_job_environment_has_no_ssh_agent(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    src, _ = repo
    (src / ".octopus" / "ci.sh").write_text('echo "agent=${SSH_AUTH_SOCK:-none}"\n')
    run(["git", "commit", "-qam", "env"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    lines = []
    GitExecutor(tmp_path / "w", url="file://{repo}", poll_s=0.01).run(
        leg_info(str(src), sha), lines.append, lambda: False
    )
    assert "agent=none" in lines


@pytest.mark.skipif(sys.platform == "win32", reason="CI nodes run on Linux and macOS only")
def test_job_environment_has_no_github_token(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "SECRET-TOKEN")
    src, sha = repo
    (src / ".octopus" / "ci.sh").write_text('echo "token=${GH_TOKEN:-none}"\n')
    run(["git", "commit", "-qam", "env"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    lines = []
    GitExecutor(tmp_path / "w", url="file://{repo}", poll_s=0.01).run(
        leg_info(str(src), sha), lines.append, lambda: False
    )
    assert "token=none" in lines and "SECRET-TOKEN" not in "".join(lines)


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_status_poster_calls_gh_with_the_commit_and_context(tmp_path):
    out = tmp_path / "args.txt"
    gh = tmp_path / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    GhStatusPoster(str(gh)).post(leg_info(REPO, SHA, "macos"), "success", "x" * 300)
    args = out.read_text().split("\n")
    assert f"repos/{REPO}/statuses/{SHA}" in args
    assert "state=success" in args
    assert "context=octopus/ci (macos)" in args
    description = next(a for a in args if a.startswith("description="))
    assert len(description) <= len("description=") + 140
    failing = tmp_path / "gh2"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(RuntimeError, match="commit status"):
        GhStatusPoster(str(failing)).post(leg_info(REPO, SHA), "success", "x")


def test_an_os_with_no_node_online_is_given_up_early_not_after_the_full_wait(world):
    world.add("alice", "macos")
    world.settle()
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux", "macos"))
    assert result.handled.keys() == {"macos"} and result.unhandled == ["linux"]
    assert 25 <= world.clock.t - started < 40  # after two announce intervals, not the full 45 s


def make_busy(world, machine, run_id="1"):
    """The machine takes a run and keeps it in progress, so it announces busy."""
    _, payload, _ = world.request(run_id=run_id)
    world.publish(payload)
    world.advance(5)
    assert machine.node.busy() == 1


def releasing(world, machine, at):
    """A pump that lets the machine finish its run once the clock reaches `at`."""

    freed = []

    def pump():
        world.advance(0.5)
        if world.clock.t >= at and machine.held and not freed:
            freed.append(machine.held.pop(0)())

    return pump


def test_a_busy_machine_is_waited_for_and_takes_the_leg_when_it_is_free(world):
    alice = world.add("alice", "linux", hold=True)
    world.settle()
    make_busy(world, alice)
    sub, _ = make_submitter(world, wait_s=45)
    sub._pump = releasing(world, alice, at=world.clock.t + 100)  # well past discover and wait
    started = world.clock.t
    legs = legs_for(world, "linux")
    result = sub.submit(legs)
    assert result.handled == {"linux": alice.node_id} and result.unhandled == []
    assert 100 <= world.clock.t - started < 115
    assert alice.node.leg_state(legs[0].leg_id) == "running"
    alice.held.pop(0)()  # the leg finishes
    assert [leg.run_id for leg in alice.executor.runs] == ["1", "77"]
    assert [s for _, s in alice.status.posts] == ["pending", "success", "pending", "success"]


def test_busy_longer_than_busy_wait_falls_back_with_its_own_reason(world):
    alice = world.add("alice", "linux", hold=True)
    world.settle()
    make_busy(world, alice)
    sub, _ = make_submitter(world, wait_s=45, busy_wait_s=90)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux"))
    assert result.unhandled == ["linux"] and not alice.executor.runs[1:]
    assert result.reasons == {"linux": "all team machines of this OS were busy for 90 s"}
    assert 90 <= world.clock.t - started < 100


def test_the_machine_does_not_run_a_leg_the_submitter_already_gave_up(world):
    alice = world.add("alice", "linux", hold=True)
    world.settle()
    make_busy(world, alice)
    sub, _ = make_submitter(world, busy_wait_s=60)
    legs = legs_for(world, "linux")
    assert sub.submit(legs).unhandled == ["linux"]
    world.advance(40)  # the requests stopped
    alice.held.pop(0)()  # free at last, but nobody asks any more
    world.advance(60)
    assert alice.node.leg_state(legs[0].leg_id) == "busy" and len(alice.executor.runs) == 1


def test_a_busy_machine_of_another_scope_does_not_make_the_submitter_wait(world):
    from octopus.team import make_member_list

    world.member_lists.append(
        make_member_list(world.team_key, "org:other", 1, ["mallory"], world.clock.now())
    )
    mallory = world.add("mallory", "linux", hold=True)  # a member of org:other, not of acme
    world.settle()
    make_busy(world, mallory)
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux"))
    assert result.reasons == {"linux": "no team machine of this OS was online"}
    assert 25 <= world.clock.t - started < 40


def test_a_busy_machine_of_another_os_does_not_make_the_submitter_wait(world):
    mac = world.add("alice", "macos", hold=True)
    world.settle()
    make_busy_mac = world.request("macos", run_id="1")[1]
    world.publish(make_busy_mac)
    world.advance(5)
    assert mac.node.busy() == 1
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux"))
    assert result.reasons == {"linux": "no team machine of this OS was online"}
    assert world.clock.t - started < 40


def test_a_busy_machine_that_went_away_stops_being_waited_for(world):
    alice = world.add("alice", "linux", hold=True)
    world.settle()
    make_busy(world, alice)
    alice.frozen = True  # the laptop sleeps: no more announces
    sub, _ = make_submitter(world, busy_wait_s=240)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux"))
    assert result.reasons == {"linux": "no team machine of this OS was online"}
    assert world.clock.t - started < 40


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_a_pull_request_status_goes_on_the_pr_head_not_the_merge_commit(tmp_path):
    out = tmp_path / "args.txt"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *git/commits/*) printf "BASE\\nHEADSHA\\n" ;;\n'
        f'  *) printf "%s\\n" "$@" > {out} ;;\n'
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    from dataclasses import replace

    pr = replace(leg_info(REPO, "MERGESHA", "macos"), event="pull_request")
    GhStatusPoster(str(gh)).post(pr, "success", "ok")
    assert f"repos/{REPO}/statuses/HEADSHA" in out.read_text().split("\n")
    push = replace(pr, event="push")
    GhStatusPoster(str(gh)).post(push, "success", "ok")
    assert f"repos/{REPO}/statuses/MERGESHA" in out.read_text().split("\n")


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_a_pull_request_status_falls_back_to_the_token_commit_if_the_parents_are_unknown(
    tmp_path, caplog
):
    out = tmp_path / "args.txt"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *git/commits/*) exit 1 ;;\n"
        f'  *) printf "%s\\n" "$@" > {out} ;;\n'
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    from dataclasses import replace

    pr = replace(leg_info(REPO, "MERGESHA", "macos"), event="pull_request")
    with caplog.at_level("WARNING", logger="octopus.ci.exec"):
        GhStatusPoster(str(gh)).post(pr, "success", "ok")
    assert f"repos/{REPO}/statuses/MERGESHA" in out.read_text().split("\n")
    assert "could not find the PR head" in caplog.text  # the fallback leaves a trace


# a running leg whose run was cancelled or superseded


def fake_run_gh(tmp_path, run="completed\tsuccess", pull="open\tHEADSHA", parents="BASE\nHEADSHA"):
    """A `gh` that answers from files, so a test can change the answer between polls."""
    (tmp_path / "run").write_text(run + "\n")
    (tmp_path / "pull").write_text(pull + "\n")
    (tmp_path / "commit").write_text(parents + "\n")
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {tmp_path}/calls\n'
        f"[ -e {tmp_path}/fail ] && exit 1\n"
        'case "$*" in\n'
        f"  *actions/runs/*) cat {tmp_path}/run ;;\n"
        f"  *pulls/*) cat {tmp_path}/pull ;;\n"
        f"  *git/commits/*) cat {tmp_path}/commit ;;\n"
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return gh


def pr_leg(**changes):
    from dataclasses import replace

    base = replace(
        leg_info(REPO, "MERGESHA"), event="pull_request", ref="refs/pull/7/merge", run_id="55"
    )
    return replace(base, **changes)


def push_leg():
    return pr_leg(event="push", ref="refs/heads/main")


def gh_calls(tmp_path):
    f = tmp_path / "calls"
    return f.read_text().splitlines() if f.exists() else []


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_a_cancelled_attempt_is_stale(tmp_path):
    probe = GhRunProbe(str(fake_run_gh(tmp_path, run="completed\tcancelled")))
    assert probe.stale(pr_leg()) == "the run was cancelled"
    assert f"repos/{REPO}/actions/runs/55/attempts/1" in gh_calls(tmp_path)[0]


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_only_a_cancelled_conclusion_counts_not_a_running_or_finished_run(tmp_path):
    probe = GhRunProbe(str(fake_run_gh(tmp_path, run="in_progress\t")))
    assert probe.stale(pr_leg()) is None
    (tmp_path / "run").write_text("in_progress\tcancelled\n")
    assert probe.stale(pr_leg()) == "the run was cancelled"
    # completed as soon as the submit job exited: that says nothing about the leg
    (tmp_path / "run").write_text("completed\tsuccess\n")
    assert GhRunProbe(str(tmp_path / "gh")).stale(push_leg()) is None
    (tmp_path / "run").write_text("completed\tfailure\n")  # e.g. the hosted windows leg failed
    assert GhRunProbe(str(tmp_path / "gh")).stale(push_leg()) is None


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_a_push_leg_is_not_stale_when_a_newer_commit_arrives(tmp_path):
    probe = GhRunProbe(str(fake_run_gh(tmp_path, pull="open\tNEWER")))
    assert probe.stale(push_leg()) is None
    assert not any("pulls/" in c or "git/commits" in c for c in gh_calls(tmp_path))


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_a_pull_request_with_a_newer_head_or_closed_is_stale(tmp_path):
    probe = GhRunProbe(str(fake_run_gh(tmp_path)))
    assert probe.stale(pr_leg()) is None  # the head is the second parent of the merge commit
    assert any(f"repos/{REPO}/pulls/7" in c for c in gh_calls(tmp_path))
    (tmp_path / "pull").write_text("open\tNEWER\n")
    assert probe.stale(pr_leg()) == "the pull request has a newer commit"
    (tmp_path / "pull").write_text("closed\tHEADSHA\n")
    assert probe.stale(pr_leg()) == "the pull request was closed"


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_asks_what_cannot_change_only_once(tmp_path):
    probe = GhRunProbe(str(fake_run_gh(tmp_path)))
    for _ in range(3):
        assert probe.stale(pr_leg()) is None
    seen = gh_calls(tmp_path)
    assert sum("actions/runs" in c for c in seen) == 1
    assert sum("git/commits" in c for c in seen) == 1
    assert sum("pulls/7" in c for c in seen) == 3  # only the PR can change


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell script")
def test_probe_keeps_the_leg_running_on_any_doubt(tmp_path):
    gh = fake_run_gh(tmp_path, run="completed\tsuccess", pull="closed\tNEWER")
    (tmp_path / "fail").write_text("")  # rate limit, no network, expired login
    assert GhRunProbe(str(gh)).stale(pr_leg()) is None
    (tmp_path / "fail").unlink()
    assert GhRunProbe(str(gh)).stale(pr_leg(ref="refs/heads/x")) is None  # no PR number
    (tmp_path / "commit").write_text("ONLY\n")  # not a merge commit: the PR head is unknown
    assert GhRunProbe(str(gh)).stale(pr_leg()) is None
    (tmp_path / "run").write_text("???\n")
    assert GhRunProbe(str(gh)).stale(pr_leg()) is None
    assert GhRunProbe(str(tmp_path / "nope")).stale(pr_leg()) is None  # no gh at all


# visibility: the machine's name, the reasons, the job summary


def test_statuses_say_which_machine_ran_the_leg(world):
    alice = world.add("alice")
    world.settle()
    _, payload, _ = world.request()
    world.publish(payload)
    world.advance(5)
    assert alice.status.descriptions == ["running on alice (linux)", "success on alice"]


def test_a_failed_leg_says_why_in_its_status(world):
    alice = world.add("alice")
    alice.executor.exit_code = 3
    world.settle()
    _, payload, _ = world.request()
    world.publish(payload)
    world.advance(5)
    assert alice.status.descriptions[-1] == "failure on alice"


def test_the_result_names_the_machine_and_explains_each_fallback(world):
    world.add("bob", "macos")
    world.settle()
    sub, _ = make_submitter(world, wait_s=45)
    result = sub.submit(legs_for(world, "linux", "macos"))
    assert result.names == {"macos": "bob"}
    assert result.reasons == {"linux": "no team machine of this OS was online"}


def test_the_reason_is_a_timeout_when_an_idle_machine_never_answers(world):
    alice = world.add("alice")
    world.settle()
    alice.node._on_request = lambda _req: None  # it announces itself idle, but takes nothing
    sub, _ = make_submitter(world, wait_s=30)
    result = sub.submit(legs_for(world, "linux"))
    assert result.reasons == {"linux": "no machine took it within 30 s"}


def test_the_reason_is_the_missing_member_list_when_the_fleet_is_silent(world):
    sub, _ = make_submitter(world, list_wait_s=3)
    result = sub.submit(legs_for(world, "linux"))
    assert "no member list" in result.reasons["linux"]


def test_the_summary_table_lists_each_leg_and_cleans_names_from_remote_machines():
    from octopus.ci.submit import SubmitResult
    from octopus.ci.summary import render

    result = SubmitResult(
        handled={"macos": "jqf7sjhugxa7j7ux"},
        names={"macos": "evil|name\n`<script>`"},
        unhandled=["linux"],
        reasons={"linux": "no team machine of this OS was online"},
    )
    text = render(["linux", "macos"], result)
    rows = [ln for ln in text.splitlines() if ln.startswith("| ")]
    assert len(rows) == 4  # header, separator, two legs: a name cannot add rows or cells
    assert all(ln.count("|") == 4 for ln in rows)
    assert "GitHub-hosted runner" in text and "no team machine" in text
    assert "`<" not in text and "script" in text and "node `jqf7sjhugxa7" in text
    assert "evilname" in text


def test_the_summary_shows_the_busy_reason():
    from octopus.ci.submit import SubmitResult
    from octopus.ci.summary import render

    reason = "all team machines of this OS were busy for 240 s"
    text = render(["macos"], SubmitResult(unhandled=["macos"], reasons={"macos": reason}))
    assert "GitHub-hosted runner | all team machines of this OS were busy for 240 s |" in text
