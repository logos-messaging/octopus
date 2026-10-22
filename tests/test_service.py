import plistlib
import subprocess
from pathlib import Path

import pytest

from octopus.agent import service

SPEC = service.ServiceSpec(
    argv=("/opt/my venv/bin/python", "-m", "octopus", "runner", "start", "--config", "/c/r.json"),
    env={"OCTOPUS_BINDINGS": "/b"},
)
SPEC_WD = service.ServiceSpec(argv=SPEC.argv, env=SPEC.env, workdir="/d")


class FakeRun:
    def __init__(self, fail_on: str | None = None, stdout: str = ""):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.stdout = stdout

    def __call__(self, argv):
        self.calls.append(argv)
        bad = self.fail_on is not None and self.fail_on in " ".join(argv)
        return subprocess.CompletedProcess(
            argv, 1 if bad else 0, self.stdout, "boom" if bad else ""
        )


def test_runner_spec_uses_this_interpreter_and_passes_env():
    spec = service.runner_spec(Path("cfg.json"), {"OCTOPUS_BINDINGS": "/b", "OTHER": "x"})
    assert spec.argv[1:5] == ("-m", "octopus", "runner", "start")
    assert spec.argv[-2] == "--config" and Path(spec.argv[-1]).is_absolute()
    assert spec.env == {"OCTOPUS_BINDINGS": str(Path("/b").resolve())}  # a drive path on Windows


def test_launchd_plist():
    plist = plistlib.loads(service.render_launchd(SPEC, Path("/l/r.log")).encode())
    assert plist["Label"] == service.LABEL
    assert plist["ProgramArguments"] == list(SPEC.argv)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["EnvironmentVariables"] == {"OCTOPUS_BINDINGS": "/b"}


def test_systemd_unit_quotes_and_does_not_loop_on_bad_config():
    unit = service.render_systemd(SPEC)
    assert 'ExecStart="/opt/my venv/bin/python" -m octopus runner start' in unit
    assert "Environment=OCTOPUS_BINDINGS=/b" in unit
    assert "RestartPreventExitStatus=2 3" in unit
    assert "WantedBy=default.target" in unit


def test_task_xml_is_well_formed():
    import xml.etree.ElementTree as ET

    xml = service.render_task_xml(SPEC).replace('encoding="UTF-16"', "")
    root = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger", ns) is not None
    assert root.find(".//t:Command", ns).text == "/opt/my venv/bin/python"
    assert "runner start" in root.find(".//t:Arguments", ns).text
    assert root.find(".//t:RestartOnFailure", ns) is not None


def test_install_launchd(tmp_path):
    run = FakeRun()
    res = service.install(SPEC, "darwin", tmp_path, run, uid=501)
    assert res.path.read_text().startswith("<?xml")
    assert run.calls[0][:2] == ["launchctl", "bootout"]
    assert run.calls[1] == ["launchctl", "bootstrap", "gui/501", str(res.path)]


def test_install_launchd_retries_while_the_old_job_is_still_going_away(tmp_path, monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda _s: None)

    class Flaky(FakeRun):
        def __call__(self, argv):
            self.calls.append(argv)
            bootstraps = sum(1 for c in self.calls if c[:2] == ["launchctl", "bootstrap"])
            bad = argv[:2] == ["launchctl", "bootstrap"] and bootstraps <= 2
            return subprocess.CompletedProcess(argv, 5 if bad else 0, "", "I/O error" if bad else "")

    run = Flaky()
    service.install(SPEC, "darwin", tmp_path, run, uid=501)
    assert [c[1] for c in run.calls].count("bootstrap") == 3


def test_install_launchd_gives_up_after_the_last_try(tmp_path, monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda _s: None)

    class Never(FakeRun):
        def __call__(self, argv):
            self.calls.append(argv)
            bad = argv[:2] == ["launchctl", "bootstrap"]
            return subprocess.CompletedProcess(argv, 5 if bad else 0, "", "I/O error" if bad else "")

    with pytest.raises(service.ServiceError, match="could not load"):
        service.install(SPEC, "darwin", tmp_path, Never(), uid=501)


def test_install_is_idempotent(tmp_path):
    for _ in range(2):
        service.install(SPEC, "darwin", tmp_path, FakeRun(), uid=1)
    assert len(list((tmp_path / "Library" / "LaunchAgents").iterdir())) == 1


def test_install_systemd_enables_and_hints_linger(tmp_path):
    run = FakeRun(stdout="Linger=no")
    res = service.install(SPEC, "linux", tmp_path, run)
    assert ["systemctl", "--user", "enable", "--now", service.SYSTEMD_UNIT] in run.calls
    assert any("enable-linger" in n for n in res.notes)
    assert res.path == tmp_path / ".config" / "systemd" / "user" / service.SYSTEMD_UNIT


def test_install_systemd_no_hint_when_lingering(tmp_path):
    res = service.install(SPEC, "linux", tmp_path, FakeRun(stdout="Linger=yes"))
    assert not any("linger" in n for n in res.notes)


def test_install_task_cleans_up_xml_and_warns_about_env(tmp_path):
    run = FakeRun()
    res = service.install(SPEC, "win32", tmp_path, run)
    assert run.calls[0][:4] == ["schtasks", "/Create", "/TN", service.TASK_NAME]
    assert run.calls[1] == ["schtasks", "/Run", "/TN", service.TASK_NAME]
    assert not list(tmp_path.iterdir())
    assert any("setx" in n for n in res.notes)


def test_install_failure_names_the_step(tmp_path):
    with pytest.raises(service.ServiceError, match="could not load the launchd job.*boom"):
        service.install(SPEC, "darwin", tmp_path, FakeRun(fail_on="bootstrap"), uid=1)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_removes_file_and_keeps_going(tmp_path, platform):
    service.install(SPEC, platform, tmp_path, FakeRun(), uid=1)
    res = service.uninstall(platform, tmp_path, FakeRun(fail_on="bootout"), uid=1)
    assert not res.path.exists()
    service.uninstall(platform, tmp_path, FakeRun(), uid=1)  # already gone


def test_status(tmp_path):
    assert service.status("linux", tmp_path, FakeRun()) == (False, "not installed")
    service.install(SPEC, "linux", tmp_path, FakeRun())
    assert service.status("linux", tmp_path, FakeRun(stdout="active\n")) == (True, "active")


def test_cli_commands_exist():
    from typer.testing import CliRunner

    from octopus.cli import app

    out = CliRunner().invoke(app, ["runner", "--help"]).output
    for name in ("install-service", "uninstall-service", "service-status"):
        assert name in out


def test_runner_spec_makes_paths_absolute_and_sets_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = service.runner_spec(None, {"OCTOPUS_DATA_DIR": "rel-data"})
    assert spec.env["OCTOPUS_DATA_DIR"] == str((tmp_path / "rel-data").resolve())
    assert spec.workdir == spec.env["OCTOPUS_DATA_DIR"]
    assert Path(service.runner_spec(None, {}).workdir).is_absolute()


def test_workdir_is_set_for_every_service_manager():
    import plistlib

    assert (
        plistlib.loads(service.render_launchd(SPEC_WD, Path("/l")).encode())["WorkingDirectory"]
        == "/d"
    )
    assert "WorkingDirectory=/d\n" in service.render_systemd(SPEC_WD)
    assert "<WorkingDirectory>/d</WorkingDirectory>" in service.render_task_xml(SPEC_WD)


def test_install_creates_the_workdir(tmp_path):
    spec = service.ServiceSpec(argv=("py",), workdir=str(tmp_path / "data"))
    service.install(spec, "linux", tmp_path, FakeRun())
    assert (tmp_path / "data").is_dir()


def test_systemd_escapes_dollar_and_percent():
    spec = service.ServiceSpec(argv=("/opt/$x/%h/py", "-m", "octopus"))
    assert "/opt/$$x/%%h/py" in service.render_systemd(spec)


def test_install_script_puts_the_extra_before_the_url():
    script = (Path(__file__).parent.parent / "scripts" / "install.sh").read_text()
    assert 'SPEC="octopus[logos] @ git+' in script
    assert "$SPEC[logos]" not in script


def test_install_script_names_all_three_ways_and_the_path_hint():
    script = (Path(__file__).parent.parent / "scripts" / "install.sh").read_text()
    assert "octopus ci init <owner/repo>" in script
    assert "octopus init <owner/repo>" in script
    assert "pipx ensurepath" in script


def test_version_matches_pyproject():
    import octopus

    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    root = Path(__file__).parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert octopus.__version__ == declared
