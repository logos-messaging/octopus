"""The light isolation level: jobs run as a dedicated `octopus` account under a Seatbelt profile.

The node stays trusted: it clones the commit with the owner's git credentials, copies the tree
(without `.git`) into a work root both accounts can use, and runs the script as the `octopus`
account through one `sudo` rule. That account has no admin rights, no keychain and no access to
the owner's files, and the profile (light_profile.py) narrows what the process can touch. It
shares the host kernel, so it is a policy and not a boundary like a VM (docs/threat-model.md).
macOS hosts only for now.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from .light_profile import NETWORKS, render

ACCOUNT = "octopus"
GROUP = "octopus-ci"
WORK_ROOT = "/Users/Shared/octopus-work"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
JOB_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
_RESERVED = frozenset({"root", "admin", "daemon", "nobody", "wheel", "staff", "_www"})
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Run = Callable[[list[str]], subprocess.CompletedProcess]


class LightError(ValueError):
    pass


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def valid_account(name: str) -> bool:
    return bool(_ACCOUNT.fullmatch(name)) and name not in _RESERVED


@dataclass(frozen=True)
class Light:
    """How a job is run as the dedicated account. All choices are the machine owner's."""

    account: str = ACCOUNT
    work_root: str = WORK_ROOT
    network: str = "internet"  # "none" cuts the job off from the network
    max_processes: int = 2048
    max_files: int = 8192
    max_cpu_s: int = 6 * 3600

    level: ClassVar[str] = "light"

    def __post_init__(self) -> None:
        if not valid_account(self.account):
            raise LightError(f"invalid account name: {self.account}")
        root = Path(self.work_root)
        if (
            not root.is_absolute()
            or len(root.parts) < 3
            or ".." in root.parts
            or not root.name.startswith("octopus")
        ):
            raise LightError(f"invalid work root: {self.work_root}")
        if self.network not in NETWORKS:
            raise LightError(f"invalid network: {self.network}: use one of: {', '.join(NETWORKS)}")
        for name in ("max_processes", "max_files", "max_cpu_s"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise LightError(f"invalid {name}: {getattr(self, name)}")

    def supported(self, run: Run = _run) -> str | None:
        """Why this host cannot run jobs this way, or None."""
        if sys.platform != "darwin":
            return "the light level exists for macOS hosts only"
        if not os.path.exists(SANDBOX_EXEC):
            return f"{SANDBOX_EXEC} is missing"
        if not Path(self.work_root).is_dir():
            return f"no work root {self.work_root}: run: octopus ci light install"
        try:
            done = run(["sudo", "-n", "-u", self.account, "/usr/bin/id", "-un"])
        except (OSError, subprocess.SubprocessError) as e:
            return f"could not run a job as {self.account}: {e}"
        if done.returncode != 0 or done.stdout.strip() != self.account:
            return (
                f"cannot run jobs as {self.account} without a password: "
                "run: octopus ci light install"
            )
        return None

    def describe(self) -> str:
        return f"light (account {self.account}, network {self.network})"

    def prepare(self, work: Path, tag: str) -> str:
        """Copy the checkout, without `.git`, where the account can use it."""
        dest = Path(self.work_root) / f"job-{tag}"
        if dest.exists() or dest.is_symlink():
            raise LightError(f"job directory exists: {dest}")
        shutil.copytree(work, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for sub in (".home", ".tmp"):
            (dest / sub).mkdir(exist_ok=True)
        _share_with_group(dest)
        return str(dest)

    def cleanup(self, job_dir: str) -> None:
        """Delete what the job left, as the account that owns it."""
        path = Path(job_dir)
        if path.parent != Path(self.work_root) or not path.name.startswith("job-"):
            return
        try:
            _run(["sudo", "-n", "-u", self.account, "/bin/rm", "-rf", str(path)])
            os.rmdir(path)
        except (OSError, subprocess.SubprocessError):
            pass

    def reap(self, run: Run = _run) -> None:
        """Kill whatever the account still runs. A node serves one job at a time in this level."""
        try:
            run(["sudo", "-n", "-u", self.account, "/usr/bin/pkill", "-9", "-u", self.account])
        except (OSError, subprocess.SubprocessError):
            pass

    def kill_argv(self, name: str) -> list[str]:
        return ["sudo", "-n", "-u", self.account, "/usr/bin/pkill", "-9", "-u", self.account]

    def run_argv(self, name: str, work: str, script: str, env: dict[str, str]) -> list[str]:
        """The command that runs `script` (a path below `work`) as the account, sandboxed."""
        for key in env:
            if not _ENV_KEY.fullmatch(key):
                raise LightError(f"invalid environment variable name: {key}")
        job_env = {
            "HOME": f"{work}/.home",
            "TMPDIR": f"{work}/.tmp",
            "PATH": JOB_PATH,
            "LANG": "en_US.UTF-8",
            **env,
        }
        limits = (
            f"ulimit -n {self.max_files} 2>/dev/null; ulimit -u {self.max_processes} 2>/dev/null; "
            f'ulimit -t {self.max_cpu_s} 2>/dev/null; exec /bin/bash "$1"'
        )
        return [
            "sudo",
            "-n",
            "-u",
            self.account,
            "-H",
            "/usr/bin/env",
            "-i",
            *[f"{k}={v}" for k, v in sorted(job_env.items())],
            SANDBOX_EXEC,
            "-D",
            f"WORK={work}",
            "-p",
            render(self.network),
            "/bin/bash",
            "-c",
            limits,
            "octopus-job",
            f"{work}/{script}",
        ]

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> Light:
        try:
            return cls(**json.loads(text))
        except (ValueError, TypeError) as e:
            raise LightError(f"could not read the light settings: {e}") from e


def _share_with_group(root: Path) -> None:
    """Give the account (a member of the work root's group) read and write on the copy."""
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = os.path.join(current, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                continue
            extra = stat.S_IRGRP | stat.S_IWGRP
            if stat.S_ISDIR(mode) or mode & stat.S_IXUSR:
                extra |= stat.S_IXGRP
            os.chmod(path, stat.S_IMODE(mode) | extra)
    os.chmod(root, 0o2770)
