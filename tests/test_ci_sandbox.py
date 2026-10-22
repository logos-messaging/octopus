import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from octopus.ci.engine import LegInfo
from octopus.ci.exec import GitExecutor
from octopus.ci.home import Home
from octopus.ci.sandbox import Sandbox, SandboxError
from octopus.cli import app

cli = CliRunner()

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="CI nodes run on Linux and macOS")


def argv_of(**kw):
    sandbox = Sandbox(image="python:3.12-slim", **kw)
    return sandbox.run_argv("octopus-abc", "/data/work/abc", ".octopus/ci.sh", {"CI": "true"})


def test_the_job_gets_only_its_checkout_and_no_privileges():
    argv = argv_of()
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in argv and "--init" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert mounts == ["/data/work/abc:/work:rw"], "nothing but the checkout is mounted"
    assert argv[-3:] == ["python:3.12-slim", "bash", "/work/.octopus/ci.sh"]
    assert "--privileged" not in argv and "--pid" not in argv and "--network=host" not in argv


def test_the_job_environment_is_only_what_it_is_told():
    argv = argv_of()
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert sorted(env) == ["CI=true", "HOME=/tmp"]


def test_network_and_limits_are_configurable():
    argv = argv_of(network="none", memory="512m", cpus="1", pids=64)
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1"
    assert argv[argv.index("--pids-limit") + 1] == "64"


@posix_only
def test_podman_keeps_the_owner_uid_inside_the_container():
    argv = argv_of(runtime="podman")
    assert argv[0] == "podman" and argv[argv.index("--userns") + 1] == "keep-id"


@pytest.mark.parametrize(
    "kw",
    [
        {"image": "--privileged"},
        {"image": "a b"},
        {"image": ""},
        {"image": "ok", "runtime": "lxc"},
        {"image": "ok", "memory": "lots"},
        {"image": "ok", "cpus": "-1"},
        {"image": "ok", "network": "host --privileged"},
        {"image": "ok", "pids": 0},
    ],
)
def test_bad_settings_are_refused(kw):
    with pytest.raises(SandboxError):
        Sandbox(**kw)


def test_settings_round_trip_and_a_corrupt_file_is_an_error(tmp_path):
    home = Home(tmp_path)
    assert home.sandbox() is None
    box = Sandbox(image="ubuntu:24.04", network="none")
    home.save_sandbox(box)
    assert home.sandbox() == box
    assert home.clear_sandbox() and not home.clear_sandbox()
    (tmp_path / "sandbox.json").write_text("{nope")
    with pytest.raises(SandboxError):
        home.sandbox()
    (tmp_path / "sandbox.json").write_text('{"image": "ok", "extra": 1}')
    with pytest.raises(SandboxError):
        home.sandbox()


# running a leg through a fake container runtime


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
    (src / ".octopus" / "ci.sh").write_text("echo hi\n")
    run(["git", "add", "."], src)
    run(["git", "commit", "-q", "-m", "ci"], src)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], src)
    return src, sha


def leg(repo_path, sha, os_name="linux"):
    return LegInfo(
        leg_id=f"{repo_path}:1:1:{os_name}",
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


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` that prints what it was asked to run, and logs every call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.txt"
    exe = bindir / "docker"
    exe.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {calls}\n'
        'if [ "$1" = run ]; then\n'
        '  echo "client-gh-token=${GH_TOKEN:-none}"\n'
        '  echo "ARGS: $@"\n'
        "  exit 7\n"
        "fi\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.setattr("octopus.ci.sandbox.sys.platform", "linux")
    return calls


@posix_only
def test_the_script_runs_in_the_container_and_the_container_is_removed(
    repo, tmp_path, fake_docker, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "SECRET-TOKEN")
    src, sha = repo
    lines = []
    ex = GitExecutor(
        tmp_path / "work",
        url="file://{repo}",
        poll_s=0.01,
        sandbox=Sandbox(image="python:3.12-slim"),
    )
    result = ex.run(leg(str(src), sha), lines.append, lambda: False)
    assert result.exit_code == 7, "the exit code of the container is the result of the leg"
    assert "client-gh-token=none" in lines, "the container client never sees the owner's token"
    args = next(line for line in lines if line.startswith("ARGS:"))
    assert "SECRET-TOKEN" not in args
    assert f"OCTOPUS_SHA={sha}" in args and "bash /work/.octopus/ci.sh" in args
    calls = fake_docker.read_text().splitlines()
    assert calls[0].startswith("run ") and calls[-1].startswith("rm -f octopus-")
    assert list((tmp_path / "work").iterdir()) == []


@posix_only
def test_a_host_that_cannot_sandbox_refuses_to_run_the_job_unprotected(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setattr("octopus.ci.sandbox.sys.platform", "darwin")
    src, sha = repo
    lines = []
    ex = GitExecutor(
        tmp_path / "work", url="file://{repo}", poll_s=0.01, sandbox=Sandbox(image="ubuntu")
    )
    result = ex.run(leg(str(src), sha, "macos"), lines.append, lambda: False)
    assert result.exit_code == 126 and "could not sandbox" in result.detail
    assert "hi" not in lines, "the script did not run on the host"


@posix_only
def test_cli_enable_status_disable(tmp_path, fake_docker):
    data = str(tmp_path / "data")
    out = cli.invoke(app, ["ci", "sandbox", "status", "--data-dir", data])
    assert out.exit_code == 0 and "no sandbox" in out.output
    out = cli.invoke(
        app,
        ["ci", "sandbox", "enable", "--image", "ubuntu:24.04", "--network", "none", "--data-dir", data],
    )
    assert out.exit_code == 0, out.output
    assert fake_docker.read_text().splitlines() == ["pull ubuntu:24.04"]
    out = cli.invoke(app, ["ci", "sandbox", "status", "--data-dir", data])
    assert "ubuntu:24.04" in out.output and "network none" in out.output
    out = cli.invoke(app, ["ci", "sandbox", "disable", "--data-dir", data])
    assert out.exit_code == 0 and "sandbox off" in out.output


@posix_only
def test_cli_enable_refuses_bad_input_and_other_hosts(tmp_path, fake_docker, monkeypatch):
    data = str(tmp_path / "data")
    out = cli.invoke(app, ["ci", "sandbox", "enable", "--image", "-v", "--data-dir", data])
    assert out.exit_code == 2
    monkeypatch.setattr("octopus.ci.sandbox.sys.platform", "darwin")
    out = cli.invoke(app, ["ci", "sandbox", "enable", "--image", "ubuntu", "--data-dir", data])
    assert out.exit_code == 3 and "macOS" in out.output
