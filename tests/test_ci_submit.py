import json
import stat
import subprocess
import sys

import pytest
from _ci_harness import REPO, SHA, World

from octopus.ci.engine import LegInfo
from octopus.ci.exec import GhStatusPoster, GitExecutor
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
        out.append(Leg(os=os_name, leg_id=leg_id, token=token, reply_key=reply))
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


def test_an_os_with_no_idle_node_is_given_up_early_not_after_the_full_wait(world):
    world.add("alice", "macos")
    world.settle()
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux", "macos"))
    assert result.handled.keys() == {"macos"} and result.unhandled == ["linux"]
    assert world.clock.t - started < 25  # not the full 45 s


def test_a_busy_node_does_not_keep_the_submitter_waiting(world):
    busy = world.add("alice", "linux", hold=True)
    world.settle()
    # the node is running another leg, so it announces busy
    _, payload, _ = world.request(run_id="1")
    world.publish(payload)
    world.advance(5)
    assert busy.node.busy() == 1
    sub, _ = make_submitter(world, wait_s=45)
    started = world.clock.t
    result = sub.submit(legs_for(world, "linux"))
    assert result.unhandled == ["linux"] and world.clock.t - started < 25
