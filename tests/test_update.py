import sys

import httpx
import pytest
from typer.testing import CliRunner

from octopus import cli_update, update
from octopus.agent import service
from octopus.cli import app

cli = CliRunner()


def test_versions_are_compared_by_number():
    assert update.parse_version("v0.3.1") == (0, 3, 1)
    assert update.parse_version("0.10.0") == (0, 10, 0)
    assert update.parse_version("main") is None
    assert update.is_newer("v0.10.0", "0.9.9")
    assert not update.is_newer("v0.3.1", "0.3.1")
    assert not update.is_newer("v0.3.0", "0.3.1")
    assert not update.is_newer("nightly", "0.3.1"), "a version we cannot read is not newer"


@pytest.mark.parametrize("ref", ["v0.3.1", "main", "feat/x", "a1b2c3d", "release-1.2"])
def test_good_refs(ref):
    assert update.check_ref(ref) == ref


@pytest.mark.parametrize("ref", ["", "-x", "a b", "../x", "a..b", "v1;rm", "https://evil/x", "a@b"])
def test_bad_refs_are_refused(ref):
    with pytest.raises(update.UpdateError):
        update.check_ref(ref)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_latest_release_reads_the_tag():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"tag_name": "v0.4.0"})

    assert update.latest_release(client(handler)) == "v0.4.0"
    assert seen == [update.LATEST_URL]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(200, json={"nope": 1}),
        httpx.Response(200, json={"tag_name": "v1; rm -rf"}),
    ],
)
def test_latest_release_failures_say_how_to_go_on(response):
    with pytest.raises(update.UpdateError, match="--ref|invalid ref"):
        update.latest_release(client(lambda request: response))


def test_install_method_is_read_from_the_environment(monkeypatch):
    monkeypatch.setattr(update, "_is_editable", lambda: False)
    assert update.install_method("/home/u/.local/pipx/venvs/octopus") == "pipx"
    assert update.install_method("/home/u/.local/share/uv/tools/octopus") == "uv"
    assert update.install_method("/home/u/.local/share/octopus/venv") == "pip"
    monkeypatch.setattr(update, "_is_editable", lambda: True)
    assert update.install_method("/anything") == "editable"


def test_the_upgrade_command_matches_the_installer():
    url = "octopus[logos] @ https://github.com/logos-messaging/octopus/archive/v0.4.0.tar.gz"
    assert update.upgrade_argv("pipx", "v0.4.0", "/py") == [
        "pipx", "install", "--force", "--python", "/py", url,
    ]
    assert update.upgrade_argv("uv", "v0.4.0", "/py") == [
        "uv", "tool", "install", "--force", "--python", "/py", url,
    ]
    assert update.upgrade_argv("pip", "v0.4.0", "/py") == [
        "/py", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", url,
    ]
    with pytest.raises(update.UpdateError, match="git pull"):
        update.upgrade_argv("editable", "v0.4.0")
    with pytest.raises(update.UpdateError):
        update.upgrade_argv("pip", "--evil")


# services


class FakeRun:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, argv):
        self.calls.append(argv)
        return type("P", (), {"returncode": self.code, "stdout": "", "stderr": "boom"})()


def test_installed_services_are_found_by_name(tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for name in ("runner", "ci", "manual-acme__app"):
        (agents / f"org.logos-messaging.octopus.{name}.plist").write_text("x")
    (agents / "com.other.plist").write_text("x")
    assert service.installed_names("darwin", tmp_path) == ["ci", "manual-acme__app", "runner"]

    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("ci", "runner"):
        (units / f"octopus-{name}.service").write_text("x")
    (units / "other.service").write_text("x")
    assert service.installed_names("linux", tmp_path) == ["ci", "runner"]
    assert service.installed_names("win32", tmp_path) == []
    assert service.installed_names("linux", tmp_path / "nothing") == []


def test_restart_uses_the_service_manager_of_the_platform():
    run = FakeRun()
    service.restart("ci", "darwin", run, uid=501)
    service.restart("runner", "linux", run)
    assert run.calls == [
        ["launchctl", "kickstart", "-k", "gui/501/org.logos-messaging.octopus.ci"],
        ["systemctl", "--user", "restart", "octopus-runner.service"],
    ]
    with pytest.raises(service.ServiceError, match="could not restart"):
        service.restart("ci", "linux", FakeRun(code=1))
    with pytest.raises(service.ServiceError, match="unsupported"):
        service.restart("ci", "win32", FakeRun())


# the command


@pytest.fixture
def env(monkeypatch):
    """Everything the command touches is replaced: nothing is installed or restarted for real."""
    state = {"ran": [], "restarted": [], "run_code": 0, "names": ["ci", "runner"]}

    def fake_run(argv):
        state["ran"].append(argv)
        return state["run_code"] if len(state["ran"]) == 1 else 0  # only the install can fail

    monkeypatch.setattr(cli_update, "_run", fake_run)
    monkeypatch.setattr(cli_update, "__version__", "0.3.1")
    monkeypatch.setattr(cli_update.sys, "platform", "linux")
    monkeypatch.setattr(update, "latest_release", lambda client=None: "v0.4.0")
    monkeypatch.setattr(update, "install_method", lambda prefix=None: "pip")
    monkeypatch.setattr(service, "installed_names", lambda *a, **k: list(state["names"]))
    monkeypatch.setattr(service, "restart", lambda name, *a, **k: state["restarted"].append(name))
    return state


def test_update_installs_refreshes_the_library_and_restarts_the_node(env):
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 0, out.output
    assert "0.3.1 -> v0.4.0" in out.output
    first, *rest = env["ran"]
    assert first[:4] == [sys.executable, "-m", "pip", "install"]
    assert "archive/v0.4.0.tar.gz" in first[-1]
    assert rest == [
        [sys.executable, "-m", "octopus", "version"],
        [sys.executable, "-m", "octopus", "lib", "install"],
    ]
    assert env["restarted"] == ["ci", "runner"]
    assert "restarted: ci" in out.output


def test_update_does_nothing_when_up_to_date(env, monkeypatch):
    monkeypatch.setattr(update, "latest_release", lambda client=None: "v0.3.1")
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 0 and "up to date: 0.3.1" in out.output
    assert env["ran"] == [] and env["restarted"] == []


def test_check_only_reports(env):
    out = cli.invoke(app, ["update", "--check"])
    assert out.exit_code == 10 and "update available: 0.3.1 -> v0.4.0" in out.output
    assert env["ran"] == [] and env["restarted"] == []


def test_a_ref_is_installed_even_when_the_version_is_the_same(env):
    out = cli.invoke(app, ["update", "--ref", "main"])
    assert out.exit_code == 0, out.output
    assert "archive/main.tar.gz" in env["ran"][0][-1]


def test_flags_skip_the_library_and_the_restart(env):
    out = cli.invoke(app, ["update", "--no-lib", "--no-restart"])
    assert out.exit_code == 0, out.output
    assert env["ran"][1:] == [[sys.executable, "-m", "octopus", "version"]]
    assert env["restarted"] == []


def test_a_failed_install_stops_before_touching_the_node(env):
    env["run_code"] = 1
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 4 and "could not install the update" in out.output
    assert len(env["ran"]) == 1 and env["restarted"] == []


def test_no_service_says_so(env):
    env["names"] = []
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 0 and "no service installed" in out.output


def test_a_development_install_is_not_touched(env, monkeypatch):
    monkeypatch.setattr(update, "install_method", lambda prefix=None: "editable")
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 1 and "git pull" in out.output
    assert env["ran"] == []


def test_bad_ref_and_windows_are_refused(env, monkeypatch):
    out = cli.invoke(app, ["update", "--ref", "-x"])
    assert out.exit_code == 1 and "invalid ref" in out.output
    monkeypatch.setattr(cli_update.sys, "platform", "win32")
    out = cli.invoke(app, ["update"])
    assert out.exit_code == 1 and "install.ps1" in out.output
    assert env["ran"] == []
