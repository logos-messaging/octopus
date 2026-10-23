"""The light isolation level: the profile, the run command, the account steps, the wizard step."""

import os
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from octopus.ci import light_account, light_wizard
from octopus.ci.engine import LegInfo
from octopus.ci.exec import GitExecutor
from octopus.ci.home import Home
from octopus.ci.light import Light, LightError
from octopus.ci.light_profile import ProfileError, render
from octopus.cli import app
from octopus.github.setup import Status

cli = CliRunner()

macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


def root_of(tmp_path) -> str:
    root = tmp_path / "octopus-work"
    root.mkdir()
    return str(root)


def ok(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


# --- profile and light settings ---------------------------------------------------------


def test_the_profile_cuts_localhost_or_the_whole_network():
    assert '(deny network* (remote ip "localhost:*"))' in render("internet")
    assert "(deny network*)\n" in render("none")
    with pytest.raises(ProfileError):
        render("lan")


def test_the_job_directory_is_a_parameter_not_part_of_the_profile():
    assert '(param "WORK")' in render() and "/tmp/job" not in render()


@pytest.mark.parametrize(
    "kw",
    [
        {"account": "root"},
        {"account": "Octo Pus"},
        {"account": ""},
        {"work_root": "relative/octopus-work"},
        {"work_root": "/Users/Shared"},
        {"work_root": "/Users/Shared/other"},
        {"work_root": "/Users/Shared/octopus-work/../x"},
        {"network": "lan"},
        {"max_processes": 0},
    ],
)
def test_invalid_settings_are_refused(kw):
    with pytest.raises(LightError):
        Light(**kw)


def test_settings_round_trip(tmp_path):
    light = Light(network="none", max_processes=64)
    assert Light.loads(light.dumps()) == light
    home = Home(tmp_path)
    assert home.light() is None
    home.save_light(light)
    assert home.light() == light
    assert home.clear_light() and home.light() is None and not home.clear_light()


def test_a_corrupt_settings_file_is_an_error_not_silence(tmp_path):
    (tmp_path / "light.json").write_text("{nope")
    with pytest.raises(LightError):
        Home(tmp_path).light()


# --- the run command ---------------------------------------------------------------------


def test_the_job_runs_as_the_account_with_a_clean_environment_under_the_profile():
    light = Light()
    argv = light.run_argv("octopus-abc", "/Users/Shared/octopus-work/job-abc", ".octopus/ci.sh", {"CI": "true"})
    assert argv[:6] == ["sudo", "-n", "-u", "octopus", "-H", "/usr/bin/env"]
    assert argv[6] == "-i", "the owner's environment is not inherited"
    env = argv[7 : argv.index("/usr/bin/sandbox-exec")]
    assert "HOME=/Users/Shared/octopus-work/job-abc/.home" in env
    assert "CI=true" in env and all("=" in item for item in env)
    assert not any(k in " ".join(env) for k in ("GH_TOKEN", "SSH_AUTH_SOCK", "GITHUB_TOKEN"))
    assert argv[argv.index("-D") + 1] == "WORK=/Users/Shared/octopus-work/job-abc"
    assert argv[-1] == "/Users/Shared/octopus-work/job-abc/.octopus/ci.sh"
    assert "sudo" not in argv[1:], "no second sudo"


def test_an_environment_name_cannot_inject_arguments():
    with pytest.raises(LightError):
        Light().run_argv("n", "/Users/Shared/octopus-work/job-x", "s", {"A B": "1"})


def test_limits_are_in_the_wrapper():
    argv = Light(max_processes=11, max_files=22, max_cpu_s=33).run_argv(
        "n", "/Users/Shared/octopus-work/job-x", "s", {}
    )
    wrapper = argv[argv.index("-c") + 1]
    assert "ulimit -n 22" in wrapper and "ulimit -u 11" in wrapper and "ulimit -t 33" in wrapper


def test_supported_says_why_not(tmp_path, monkeypatch):
    light = Light(work_root=root_of(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    assert "macOS hosts only" in light.supported()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("octopus.ci.light.os.path.exists", lambda p: True)
    assert light.supported(lambda argv: ok("octopus\n")) is None
    assert "without a password" in light.supported(lambda argv: ok("someone\n"))
    assert "without a password" in light.supported(lambda argv: ok("", 1))
    assert "no work root" in Light(work_root=str(tmp_path / "octopus-gone")).supported(
        lambda argv: ok("octopus\n")
    )


# --- the copy the job gets ----------------------------------------------------------------


def test_the_job_gets_a_copy_without_git_and_symlinks_are_not_followed(tmp_path):
    work = tmp_path / "checkout"
    (work / ".git" / "hooks").mkdir(parents=True)
    (work / ".git" / "config").write_text("[core]\n")
    (work / "src").mkdir()
    (work / "src" / "a.sh").write_text("echo a\n")
    (work / "src" / "a.sh").chmod(0o755)
    secret = tmp_path / "secret.txt"
    secret.write_text("owner's secret")
    (work / "link").symlink_to(secret)
    light = Light(work_root=root_of(tmp_path))
    job = Path(light.prepare(work, "abc"))
    assert not (job / ".git").exists(), "the owner's git never runs on a tree the job can change"
    assert (job / "link").is_symlink() and (job / "link").readlink() == secret
    assert (job / ".home").is_dir() and (job / ".tmp").is_dir()
    mode = stat.S_IMODE((job / "src" / "a.sh").stat().st_mode)
    assert mode & stat.S_IXGRP and mode & stat.S_IWGRP, "the account can run and write it"
    assert stat.S_IMODE(job.stat().st_mode) == 0o2770
    with pytest.raises(LightError):
        light.prepare(work, "abc")


def test_cleanup_only_touches_job_directories_of_the_work_root(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("octopus.ci.light._run", lambda argv: calls.append(argv))
    light = Light(work_root=root_of(tmp_path))
    light.cleanup(str(tmp_path))
    light.cleanup(str(Path(light.work_root) / "not-a-job"))
    light.cleanup(str(Path(light.work_root).parent / "job-x"))
    assert calls == []
    job = Path(light.work_root) / "job-abc"
    job.mkdir()
    light.cleanup(str(job))
    assert calls == [["sudo", "-n", "-u", "octopus", "/bin/rm", "-rf", str(job)]]


# --- the account steps --------------------------------------------------------------------


def test_pick_uid_takes_the_first_free_one_in_the_hidden_range():
    listing = "root 0\n_www 70\noctopus_old 300\nsvc 301\nsomeone 501\n"
    assert light_account.pick_uid(listing) == 302
    with pytest.raises(light_account.LightSetupError):
        light_account.pick_uid("\n".join(f"u{i} {i}" for i in range(300, 400)))


def test_the_sudo_rule_goes_down_in_privilege_only():
    assert light_account.sudoers_line("ivan", "octopus") == "ivan ALL=(octopus) NOPASSWD: ALL\n"
    for bad in ("", "a b", "x\nALL ALL=(ALL) NOPASSWD: ALL", "-x"):
        with pytest.raises(LightError):
            light_account.sudoers_line(bad, "octopus")


def test_the_install_steps_make_a_hidden_non_admin_account():
    steps = light_account.install_steps("ivan", 305, "/tmp/rule")
    commands = [s.command() for s in steps]
    assert "sudo dscl . -create /Users/octopus UniqueID 305" in commands
    assert "sudo dscl . -create /Users/octopus IsHidden 1" in commands
    assert "sudo dscl . -create /Users/octopus Password *" in commands
    assert "sudo chmod 700 /Users/octopus" in commands
    assert "sudo chmod 2770 /Users/Shared/octopus-work" in commands
    assert not any("admin" in c for c in commands), "never added to the admin group"
    assert commands.index("sudo visudo -cf /tmp/rule") < commands.index(
        "sudo install -m 440 -o root -g wheel /tmp/rule /etc/sudoers.d/octopus-ci"
    ), "the rule is checked before it is installed"
    assert steps[-1].argv == ("rm", "-f", "/tmp/rule") and not steps[-1].sudo


def test_an_existing_account_is_not_recreated():
    commands = [s.command() for s in light_account.install_steps("ivan", 305, "/t", exists=True)]
    assert not any("-create /Users/octopus" in c for c in commands)


def test_the_account_cannot_be_the_owner_or_a_system_account():
    with pytest.raises(LightError):
        light_account.install_steps("octopus", 305, "/t")
    with pytest.raises(LightError):
        light_account.install_steps("ivan", 305, "/t", account="root")


def test_uninstall_refuses_paths_it_does_not_own():
    steps = light_account.uninstall_steps("/Users/octopus")
    assert "sudo rm -rf /Users/octopus" in [s.command() for s in steps]
    for home in ("/Users", "/Users/ivan", "/Users/octopus/../ivan", "/"):
        with pytest.raises(LightError):
            light_account.uninstall_steps(home)
    with pytest.raises(LightError):
        light_account.uninstall_steps("/Users/octopus", work_root="/Users/Shared")


class Machine:
    """A fake machine: records the steps and can fail one."""

    def __init__(self, fail_on=None, existing=False, admin=False):
        self.steps = []
        self.fail_on = fail_on
        self.existing = existing
        self.admin = admin

    def execute(self, step):
        self.steps.append(step.label)
        code = 1 if step.label == self.fail_on else 0
        return subprocess.CompletedProcess([], code, "", "boom\n" if code else "")

    def run(self, argv):
        if argv[:3] == ["dscl", ".", "-read"]:
            return ok("UniqueID: 311\n") if self.existing else ok("", 56)
        if argv[:3] == ["dscl", ".", "-list"]:
            return ok("root 0\nsomeone 501\n")
        if argv[0] == "dseditgroup":
            return ok("yes\n" if self.admin else "no\n")
        if argv[:5] == ["sudo", "-n", "-u", "octopus", "/usr/bin/id"]:
            return ok("octopus\n") if self.existing else ok("", 1)
        return ok()


def test_install_runs_every_step_and_returns_the_settings():
    m = Machine()
    light = light_account.install("ivan", execute=m.execute, run=m.run, sudoers_tmp="/t")
    assert light == Light()
    assert m.steps[0] == "create the group octopus-ci" and m.steps[-1] == "remove the temporary file"


def test_a_failed_step_undoes_a_fresh_install():
    m = Machine(fail_on="install the sudo rule")
    with pytest.raises(light_account.LightSetupError, match="install the sudo rule: boom"):
        light_account.install("ivan", execute=m.execute, run=m.run, sudoers_tmp="/t")
    assert "delete the account" in m.steps and "delete the work root" in m.steps


def test_a_failed_step_keeps_an_account_that_was_there_before():
    m = Machine(fail_on="set its mode", existing=True)
    with pytest.raises(light_account.LightSetupError):
        light_account.install("ivan", execute=m.execute, run=m.run, sudoers_tmp="/t")
    assert "delete the account" not in m.steps


def test_an_account_that_is_an_administrator_is_refused():
    m = Machine(existing=True, admin=True)
    with pytest.raises(light_account.LightSetupError, match="administrator"):
        light_account.install("ivan", execute=m.execute, run=m.run, sudoers_tmp="/t")
    assert m.steps == []


def test_readable_secrets_lists_what_the_account_can_read(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".gitconfig").write_text("x")

    def run(argv):
        return ok("", 0 if argv[-1].endswith(".gitconfig") else 1)

    assert light_account.readable_secrets(str(tmp_path), "octopus", run) == [
        str(tmp_path / ".gitconfig")
    ]


# --- the wizard step ----------------------------------------------------------------------


def wizard(tmp_path, m, *, terminal=True, answer=True, assume_yes=False, **kw):
    said = []
    check = light_wizard.offer_light(
        Home(tmp_path / "home"),
        assume_yes=assume_yes,
        say=said.append,
        confirm=lambda *a, **k: answer,
        execute=m.execute,
        run=m.run,
        terminal=lambda: terminal,
        owner="ivan",
        owner_home=str(tmp_path / "ivan-home"),
        **kw,
    )
    return check, said


def test_without_a_terminal_nothing_is_done(tmp_path):
    m = Machine()
    check, _ = wizard(tmp_path, m, terminal=False, assume_yes=True)
    assert check.status == Status.SKIPPED and "octopus ci light install" in check.detail
    assert m.steps == []


def test_the_wizard_shows_the_commands_and_the_removal_before_and_after(tmp_path):
    m = Machine()
    check, said = wizard(tmp_path, m)
    text = "\n".join(said)
    assert 'hidden, non-admin account called "octopus"' in text
    assert "sudo dscl . -create /Users/octopus UniqueID 300" in text
    assert "octopus ci uninstall" in text
    assert check.status == Status.CHANGED
    assert Home(tmp_path / "home").light() == Light()


def test_declining_changes_nothing(tmp_path):
    m = Machine()
    check, _ = wizard(tmp_path, m, answer=False)
    assert check.status == Status.SKIPPED and m.steps == []
    assert Home(tmp_path / "home").light() is None


def test_a_failure_is_reported_and_nothing_is_saved(tmp_path):
    m = Machine(fail_on="create the account")
    check, _ = wizard(tmp_path, m)
    assert check.status == Status.FAIL and "half-installed" in check.detail
    assert Home(tmp_path / "home").light() is None


def test_secrets_the_account_can_read_are_warned_about(tmp_path):
    (tmp_path / "ivan-home").mkdir()
    (tmp_path / "ivan-home" / ".ssh").mkdir()
    m = Machine()
    m.run_orig = m.run
    m.run = lambda argv: ok("", 0) if argv[:6] == ["sudo", "-n", "-u", "octopus", "/bin/test", "-r"] else m.run_orig(argv)
    _, said = wizard(tmp_path, m)
    assert any(line.startswith("warning: the account can read") and ".ssh" in line for line in said)


def test_an_unusable_account_fails_the_wizard_closed(tmp_path, monkeypatch):
    home = Home(tmp_path / "home")
    home.save_light(Light())
    monkeypatch.setattr(Light, "supported", lambda self, run=None: "the account is gone")
    check = light_wizard.offer_light(home, assume_yes=True)
    assert check.status == Status.FAIL and "the account is gone" in check.detail


# --- the CLI -------------------------------------------------------------------------------


def test_cli_status_and_disable(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    out = cli.invoke(app, ["ci", "light", "status", "--data-dir", data])
    assert out.exit_code == 0 and "off" in out.output
    from octopus.ci.home import ci_home

    ci_home(Path(data)).save_light(Light(work_root=root_of(tmp_path)))
    monkeypatch.setattr(Light, "supported", lambda self, run=None: "no password")
    out = cli.invoke(app, ["ci", "light", "status", "--data-dir", data])
    assert out.exit_code == 3 and "NOT USABLE: no password" in out.output
    out = cli.invoke(app, ["ci", "status", "--data-dir", data])
    assert "light (account octopus" in out.output and "NOT USABLE" in out.output
    out = cli.invoke(app, ["ci", "light", "disable", "--data-dir", data])
    assert "off" in out.output
    out = cli.invoke(app, ["ci", "status", "--data-dir", data])
    assert "isolation   host (no isolation" in out.output


def test_cli_uninstall_shows_the_commands_and_runs_them_after_asking(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(
        light_account, "execute_step", lambda step: ran.append(step.command()) or ok()
    )
    data = str(tmp_path / "data")
    out = cli.invoke(app, ["ci", "uninstall", "--data-dir", data], input="n\n")
    assert out.exit_code == 1 and "sudo rm -rf /Users/octopus" in out.output and ran == []
    out = cli.invoke(app, ["ci", "uninstall", "--yes", "--data-dir", data])
    assert out.exit_code == 0 and "sudo dscl . -delete /Users/octopus" in ran


# --- the executor with the light level -------------------------------------------------------


def make_repo(tmp_path, script):
    src = tmp_path / "origin"
    src.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.org"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=src, check=True, capture_output=True)
    (src / ".octopus").mkdir()
    (src / ".octopus" / "ci.sh").write_text(script)
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "ci"], cwd=src, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=src, check=True
    )
    return src, sha


def leg(repo_path, sha):
    return LegInfo(
        leg_id=f"{repo_path}:1:1:macos",
        repo=repo_path,
        sha=sha,
        ref="refs/heads/main",
        event="push",
        run_id="1",
        attempt="1",
        os="macos",
        reply_pub="x",
        workflow="w",
    )


@pytest.fixture
def fake_sudo(tmp_path, monkeypatch):
    """A `sudo` that only drops its own options and runs the command as the current user."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "sudo"
    exe.write_text(
        "#!/bin/sh\n"
        'while [ "$1" = -n ] || [ "$1" = -H ]; do shift; done\n'
        'if [ "$1" = -u ]; then shift; shift; fi\n'
        'while [ "$1" = -H ]; do shift; done\n'
        'case "$1" in /usr/bin/pkill) exit 0;; esac\n'
        'exec "$@"\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(Light, "supported", lambda self, run=None: None)


def test_a_host_job_says_so_in_its_log(tmp_path):
    src, sha = make_repo(tmp_path, "echo hi\n")
    lines = []
    ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.01)
    assert ex.run(leg(str(src), sha), lines.append, lambda: False).exit_code == 0
    assert lines[0].startswith("octopus: isolation: host") and "hi" in lines


@macos_only
def test_a_light_job_runs_in_a_copy_under_the_profile_and_leaves_nothing(tmp_path, fake_sudo):
    home = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".octopus-test-"))
    try:
        (home / "owner-secret").write_text("secret")
        script = (
            "echo home=$HOME\n"
            "echo git=$(test -e .git && echo present || echo absent)\n"
            f"cat {home}/owner-secret 2>/dev/null && echo READ-SECRET || echo secret-denied\n"
            "echo ghtoken=${GH_TOKEN:-none}\n"
            "echo written > out.txt && echo write-ok\n"
            f"touch {home}/planted 2>/dev/null && echo PLANTED || echo plant-denied\n"
        )
        src, sha = make_repo(tmp_path, script)
        light = Light(work_root=root_of(tmp_path))
        lines = []
        os.environ["GH_TOKEN"] = "OWNER-TOKEN"
        try:
            ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.01, isolation=light)
            result = ex.run(leg(str(src), sha), lines.append, lambda: False)
        finally:
            del os.environ["GH_TOKEN"]
        assert result.exit_code == 0, lines
        assert lines[0].startswith("octopus: isolation: light (account octopus")
        assert "git=absent" in lines and "secret-denied" in lines and "plant-denied" in lines
        assert "ghtoken=none" in lines and "write-ok" in lines
        assert "READ-SECRET" not in lines and "PLANTED" not in lines
        assert not (home / "planted").exists()
        assert any(line.startswith("home=") and "/job-" in line for line in lines)
        assert list(Path(light.work_root).iterdir()) == [], "the copy is removed"
        assert list((tmp_path / "work").iterdir()) == [], "so is the checkout"
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


@macos_only
def test_a_light_node_that_cannot_isolate_refuses_to_run_the_job(tmp_path, fake_sudo, monkeypatch):
    monkeypatch.setattr(Light, "supported", lambda self, run=None: "no password")
    src, sha = make_repo(tmp_path, "echo SHOULD-NOT-RUN\n")
    lines = []
    ex = GitExecutor(
        tmp_path / "work",
        url="file://{repo}",
        poll_s=0.01,
        isolation=Light(work_root=root_of(tmp_path)),
    )
    result = ex.run(leg(str(src), sha), lines.append, lambda: False)
    assert result.exit_code == 126 and "no password" in result.detail
    assert "SHOULD-NOT-RUN" not in lines


# --- the profile itself, with the real sandbox-exec (runs as the current user) -----------------


def seatbelt(profile, work, argv, **kw):
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-D", f"WORK={work}", "-p", profile, *argv],
        check=False,
        capture_output=True,
        text=True,
        cwd=work,
        timeout=30,
        **kw,
    )


@macos_only
def test_the_real_profile_blocks_homes_writes_localhost_and_launchctl(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "in.txt").write_text("in")
    home = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".octopus-test-"))
    try:
        (home / "secret").write_text("secret")
        profile = render("internet")
        assert seatbelt(profile, work, ["/bin/cat", str(work / "in.txt")]).stdout == "in"
        assert seatbelt(profile, work, ["/bin/cat", str(home / "secret")]).returncode != 0
        assert seatbelt(profile, work, ["/bin/ls", str(Path.home())]).returncode != 0
        out = seatbelt(profile, work, ["/bin/sh", "-c", f"echo x > {work}/o && cat {work}/o"])
        assert out.stdout.strip() == "x"
        assert seatbelt(profile, work, ["/bin/sh", "-c", f"echo x > {home}/w"]).returncode != 0
        assert not (home / "w").exists()
        assert seatbelt(profile, work, ["/bin/launchctl", "list"]).returncode != 0
        assert seatbelt(profile, work, ["/usr/bin/crontab", "-l"]).returncode != 0
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


@macos_only
@pytest.mark.parametrize("network", ["internet", "none"])
def test_the_real_profile_blocks_services_on_localhost(tmp_path, network):
    work = tmp_path / "work"
    work.mkdir()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    code = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(3)\n"
        "try:\n"
        f"    s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
        "except OSError:\n"
        "    print('DENIED')\n"
    )
    try:
        control = subprocess.run(
            ["/usr/bin/python3", "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert control.stdout.strip() == "CONNECTED", "the service is reachable without the profile"
        out = seatbelt(render(network), work, ["/usr/bin/python3", "-c", code])
        assert out.stdout.strip() == "DENIED"
    finally:
        server.close()


# --- the node ----------------------------------------------------------------------------------


def test_the_node_takes_one_isolation_and_one_job_at_a_time_in_the_light_level(tmp_path):
    from octopus.ci.runtime import CiRuntime, RuntimeError_, Settings
    from octopus.ci.sandbox import Sandbox

    home = Home(tmp_path / "home")
    settings = Settings("macos", "arm64")
    assert CiRuntime._isolation(home, settings) is None
    home.save_light(Light(work_root=root_of(tmp_path)))
    assert isinstance(CiRuntime._isolation(home, settings), Light)
    with pytest.raises(RuntimeError_, match="one job at a time"):
        CiRuntime._isolation(home, Settings("macos", "arm64", capacity=2))
    home.save_sandbox(Sandbox(image="python:3.12-slim"))
    with pytest.raises(RuntimeError_, match="both"):
        CiRuntime._isolation(home, settings)
