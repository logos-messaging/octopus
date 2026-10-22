"""Run `octopus runner start` as a per-user service: launchd, systemd --user or Task Scheduler.

Rendering is pure; the install and uninstall steps go through an injectable `Run` so tests
never touch the host's service manager.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "org.logos-messaging.octopus.runner"
SYSTEMD_UNIT = "octopus-runner.service"
TASK_NAME = "Octopus Runner"

# Environment captured at install time: where the native library bindings and the data live.
PASSTHROUGH_ENV = ("OCTOPUS_BINDINGS", "OCTOPUS_DATA_DIR")

# Exit codes that restarting cannot fix (see cli.py: usage, bad settings).
NO_RESTART_EXIT_CODES = (2, 3)


class ServiceError(RuntimeError):
    """The service could not be installed, removed or queried."""


@dataclass(frozen=True)
class ServiceSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    # Services start in `/` (launchd) or C:\Windows\System32 (Task Scheduler): give them a
    # directory the user can write, so nothing relative lands in a system directory.
    workdir: str | None = None


@dataclass(frozen=True)
class ServiceResult:
    path: Path
    notes: tuple[str, ...] = ()


Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def runner_spec(
    config: Path | None = None, environ: Mapping[str, str] | None = None
) -> ServiceSpec:
    """The command the service runs: this interpreter, so a venv or pipx install keeps working."""
    from .runner import default_data_dir

    environ = os.environ if environ is None else environ
    argv = [sys.executable, "-m", "octopus", "runner", "start"]
    if config is not None:
        argv += ["--config", str(config.expanduser().resolve())]
    # Paths are made absolute: the service does not start in the installing shell's directory.
    env = {
        k: str(Path(environ[k]).expanduser().resolve()) for k in PASSTHROUGH_ENV if environ.get(k)
    }
    workdir = env.get("OCTOPUS_DATA_DIR") or str(default_data_dir().resolve())
    return ServiceSpec(argv=tuple(argv), env=env, workdir=workdir)


# rendering


def render_launchd(spec: ServiceSpec, log_path: Path) -> str:
    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": list(spec.argv),
        "RunAtLoad": True,
        # Restart after a crash or a signal, but not after a clean `runner stop`.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if spec.workdir:
        plist["WorkingDirectory"] = spec.workdir
    if spec.env:
        plist["EnvironmentVariables"] = dict(spec.env)
    return plistlib.dumps(plist).decode("utf-8")


def _systemd_quote(arg: str) -> str:
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"' if any(c in arg for c in " \t\"'\\") else escaped


def render_systemd(spec: ServiceSpec) -> str:
    exec_start = " ".join(_systemd_quote(a) for a in spec.argv)
    env_lines = "".join(
        f"Environment={_systemd_quote(f'{k}={v}')}\n" for k, v in sorted(spec.env.items())
    )
    workdir_line = f"WorkingDirectory={_systemd_quote(spec.workdir)}\n" if spec.workdir else ""
    prevent = " ".join(str(c) for c in NO_RESTART_EXIT_CODES)
    return (
        "[Unit]\n"
        "Description=Octopus runner\n"
        "Documentation=https://github.com/logos-messaging/octopus/blob/main/docs/install.md\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{workdir_line}"
        f"{env_lines}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"RestartPreventExitStatus={prevent}\n"
        "TimeoutStopSec=90\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_task_xml(spec: ServiceSpec) -> str:
    command, *args = spec.argv
    arguments = subprocess.list2cmdline(args)
    workdir = (
        f"    <WorkingDirectory>{escape(spec.workdir)}</WorkingDirectory>\n" if spec.workdir else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>Octopus runner</Description></RegistrationInfo>\n"
        "  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec>\n'
        f"    <Command>{escape(command)}</Command>\n"
        f"    <Arguments>{escape(arguments)}</Arguments>\n"
        f"{workdir}"
        "  </Exec></Actions>\n"
        "</Task>\n"
    )


# locations


def launchd_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "octopus" / "runner.log"


def systemd_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def current_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "win32"
    raise ServiceError(f"could not pick a service manager, unsupported platform: {sys.platform}")


def _check(proc: subprocess.CompletedProcess[str], what: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ServiceError(f"{what}: exit code: {proc.returncode}: {detail}")


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)


# install


def install(
    spec: ServiceSpec,
    platform: str | None = None,
    home: Path | None = None,
    run: Run = default_run,
    uid: int | None = None,
) -> ServiceResult:
    platform = platform or current_platform()
    home = home or Path.home()
    if spec.workdir:
        Path(spec.workdir).mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        return _install_launchd(spec, home, run, os.getuid() if uid is None else uid)
    if platform == "linux":
        return _install_systemd(spec, home, run)
    if platform == "win32":
        return _install_task(spec, home, run)
    raise ServiceError(f"could not install the service, unsupported platform: {platform}")


def _install_launchd(spec: ServiceSpec, home: Path, run: Run, uid: int) -> ServiceResult:
    path = launchd_path(home)
    log = launchd_log_path(home)
    log.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, render_launchd(spec, log))
    domain = f"gui/{uid}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"])  # not loaded yet is fine
    _check(run(["launchctl", "bootstrap", domain, str(path)]), "could not load the launchd job")
    return ServiceResult(
        path, (f"logs: {log}", "starts at login; `octopus runner stop` leaves it stopped")
    )


def _install_systemd(spec: ServiceSpec, home: Path, run: Run) -> ServiceResult:
    path = systemd_path(home)
    _atomic_write(path, render_systemd(spec))
    _check(run(["systemctl", "--user", "daemon-reload"]), "could not reload systemd")
    _check(
        run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT]),
        "could not enable the systemd unit",
    )
    notes = [f"logs: journalctl --user -u {SYSTEMD_UNIT} -f"]
    user = getpass.getuser()
    linger = run(["loginctl", "show-user", user, "-p", "Linger"])
    if linger.returncode != 0 or "Linger=yes" not in linger.stdout:
        notes.append(
            f"to start at boot without logging in, an admin must run: loginctl enable-linger {user}"
        )
    return ServiceResult(path, tuple(notes))


def _install_task(spec: ServiceSpec, home: Path, run: Run) -> ServiceResult:
    xml_path = home / ".octopus-runner-task.xml"
    _atomic_write(xml_path, render_task_xml(spec), encoding="utf-16")
    try:
        _check(
            run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]),
            "could not create the scheduled task",
        )
    finally:
        xml_path.unlink(missing_ok=True)
    _check(run(["schtasks", "/Run", "/TN", TASK_NAME]), "could not start the scheduled task")
    notes = ["starts at logon; a crashed runner is not restarted until the next logon"]
    if spec.env:
        notes.append(
            "environment variables are not passed to a scheduled task: set them for your user "
            f"with setx: {', '.join(sorted(spec.env))}"
        )
    return ServiceResult(Path(f"Task Scheduler\\{TASK_NAME}"), tuple(notes))


# uninstall


def uninstall(
    platform: str | None = None,
    home: Path | None = None,
    run: Run = default_run,
    uid: int | None = None,
) -> ServiceResult:
    platform = platform or current_platform()
    home = home or Path.home()
    if platform == "darwin":
        path = launchd_path(home)
        run(["launchctl", "bootout", f"gui/{os.getuid() if uid is None else uid}/{LABEL}"])
        path.unlink(missing_ok=True)
        return ServiceResult(path)
    if platform == "linux":
        path = systemd_path(home)
        run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        path.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
        return ServiceResult(path)
    if platform == "win32":
        run(["schtasks", "/End", "/TN", TASK_NAME])
        _check(run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]), "could not delete the task")
        return ServiceResult(Path(f"Task Scheduler\\{TASK_NAME}"))
    raise ServiceError(f"could not uninstall the service, unsupported platform: {platform}")


# status


def status(
    platform: str | None = None,
    home: Path | None = None,
    run: Run = default_run,
    uid: int | None = None,
) -> tuple[bool, str]:
    """(installed, one-line description)."""
    platform = platform or current_platform()
    home = home or Path.home()
    if platform == "darwin":
        if not launchd_path(home).exists():
            return False, "not installed"
        proc = run(["launchctl", "print", f"gui/{os.getuid() if uid is None else uid}/{LABEL}"])
        return True, "loaded" if proc.returncode == 0 else "installed, not loaded"
    if platform == "linux":
        if not systemd_path(home).exists():
            return False, "not installed"
        proc = run(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
        return True, (proc.stdout.strip() or "unknown")
    if platform == "win32":
        proc = run(["schtasks", "/Query", "/TN", TASK_NAME])
        return (proc.returncode == 0), ("installed" if proc.returncode == 0 else "not installed")
    raise ServiceError(f"could not query the service, unsupported platform: {platform}")
