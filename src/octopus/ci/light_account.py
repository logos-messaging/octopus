"""Creating, checking and removing the dedicated `octopus` account (macOS and Linux).

Everything here is a list of explicit steps so the wizard can show the exact commands before
anything runs, and so tests can run them against a fake. The steps need the machine's admin
password (`sudo`); nothing is done silently.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .light import (
    ACCOUNT,
    BUBBLEWRAP,
    GROUP,
    LINUX_HOME,
    LINUX_WORK_ROOT,
    WORK_ROOT,
    Light,
    LightError,
    default_backend,
    pure_path,
    valid_account,
)

SUDOERS_FILE = "/etc/sudoers.d/octopus-ci"
UID_RANGE = (300, 399)  # below 500: hidden from the login window
STAFF_GID = "20"
ADMIN_GROUPS = ("sudo", "wheel", "admin")

# What the account must not be able to read, relative to the owner's home.
SECRET_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".gitconfig",
    ".config/gh",
    ".zsh_history",
    ".bash_history",
    "Library/Keychains",
    "Documents",
    "Desktop",
    "Downloads",
)


class LightSetupError(RuntimeError):
    """Why the account could not be set up or removed."""


@dataclass(frozen=True)
class Step:
    label: str
    argv: tuple[str, ...]
    sudo: bool = True
    stdin: str | None = None

    def command(self) -> str:
        return " ".join(("sudo", *self.argv) if self.sudo else self.argv)


@dataclass(frozen=True)
class AccountState:
    exists: bool
    uid: int | None = None
    admin: bool = False
    sudo_ok: bool = False


Run = Callable[[list[str]], subprocess.CompletedProcess]
Exec = Callable[[Step], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL
    )


def pick_uid(listing: str, low: int = UID_RANGE[0], high: int = UID_RANGE[1]) -> int:
    """A free uid from `dscl . -list /Users UniqueID`."""
    taken = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            taken.add(int(parts[-1]))
    for uid in range(low, high + 1):
        if uid not in taken:
            return uid
    raise LightSetupError(f"no free uid between {low} and {high}")


def sudoers_line(owner: str, account: str) -> str:
    """The owner may run commands as the account only: down in privilege, never up."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]{0,63}", owner):
        raise LightError(f"invalid user name for sudoers: {owner}")
    return f"{owner} ALL=({account}) NOPASSWD: ALL\n"


def inspect(
    owner: str, account: str = ACCOUNT, run: Run = _run, backend: str | None = None
) -> AccountState:
    if (backend or default_backend()) == BUBBLEWRAP:
        return _inspect_linux(account, run)
    try:
        read = run(["dscl", ".", "-read", f"/Users/{account}", "UniqueID"])
    except (OSError, subprocess.SubprocessError):
        return AccountState(False)
    if read.returncode != 0:
        return AccountState(False)
    uid = None
    match = re.search(r"UniqueID:\s*(\d+)", read.stdout)
    if match:
        uid = int(match.group(1))
    member = run(["dseditgroup", "-o", "checkmember", "-m", account, "admin"])
    admin = member.stdout.strip().lower().startswith("yes")
    who = run(["sudo", "-n", "-u", account, "/usr/bin/id", "-un"])
    return AccountState(True, uid, admin, who.returncode == 0 and who.stdout.strip() == account)


def _inspect_linux(account: str, run: Run) -> AccountState:
    try:
        entry = run(["getent", "passwd", account])
    except (OSError, subprocess.SubprocessError):
        return AccountState(False)
    if entry.returncode != 0:
        return AccountState(False)
    fields = entry.stdout.strip().split(":")
    uid = int(fields[2]) if len(fields) > 2 and fields[2].isdigit() else None
    groups = run(["id", "-nG", account]).stdout.split()
    admin = any(g in ADMIN_GROUPS for g in groups)
    who = run(["sudo", "-n", "-u", account, "/usr/bin/id", "-un"])
    return AccountState(True, uid, admin, who.returncode == 0 and who.stdout.strip() == account)


def install_steps(
    owner: str,
    uid: int,
    sudoers_tmp: str,
    *,
    account: str = ACCOUNT,
    group: str = GROUP,
    work_root: str = WORK_ROOT,
    exists: bool = False,
) -> list[Step]:
    """The commands that create the account, its group, the work root and the sudo rule."""
    if not valid_account(account):
        raise LightError(f"invalid account name: {account}")
    if account == owner:
        raise LightError("the dedicated account cannot be the machine owner")
    user = f"/Users/{account}"
    steps: list[Step] = [
        Step(f"create the group {group}", ("dseditgroup", "-o", "create", group)),
    ]
    if not exists:
        steps += [
            Step("create the account", ("dscl", ".", "-create", user)),
            Step("set its shell", ("dscl", ".", "-create", user, "UserShell", "/bin/bash")),
            Step("set its name", ("dscl", ".", "-create", user, "RealName", "Octopus CI")),
            Step("set its uid", ("dscl", ".", "-create", user, "UniqueID", str(uid))),
            Step("set its group", ("dscl", ".", "-create", user, "PrimaryGroupID", STAFF_GID)),
            Step("set its home", ("dscl", ".", "-create", user, "NFSHomeDirectory", user)),
            Step("hide it from the login window", ("dscl", ".", "-create", user, "IsHidden", "1")),
            Step("disable password login", ("dscl", ".", "-create", user, "Password", "*")),
            Step("create its home", ("mkdir", "-p", user)),
            Step("give it its home", ("chown", f"{account}:staff", user)),
            Step("keep its home private", ("chmod", "700", user)),
        ]
    steps += [
        Step(
            f"add the account to {group}",
            ("dseditgroup", "-o", "edit", "-a", account, "-t", "user", group),
        ),
        Step("create the work root", ("mkdir", "-p", work_root)),
        Step("share it with the account", ("chown", f"{owner}:{group}", work_root)),
        Step("set its mode", ("chmod", "2770", work_root)),
        Step(
            "write the sudo rule (a temporary file)",
            ("tee", sudoers_tmp),
            sudo=False,
            stdin=sudoers_line(owner, account),
        ),
        Step("check the sudo rule", ("visudo", "-cf", sudoers_tmp)),
        Step(
            "install the sudo rule",
            ("install", "-m", "440", "-o", "root", "-g", "wheel", sudoers_tmp, SUDOERS_FILE),
        ),
        Step("remove the temporary file", ("rm", "-f", sudoers_tmp), sudo=False),
    ]
    return steps


def install_steps_linux(
    owner: str,
    sudoers_tmp: str,
    *,
    account: str = ACCOUNT,
    group: str = GROUP,
    work_root: str = LINUX_WORK_ROOT,
    exists: bool = False,
) -> list[Step]:
    """Linux: a system account without login, its group, the work root and the sudo rule."""
    if not valid_account(account):
        raise LightError(f"invalid account name: {account}")
    if account == owner:
        raise LightError("the dedicated account cannot be the machine owner")
    home = f"/var/lib/{account}" if account != ACCOUNT else LINUX_HOME
    steps: list[Step] = [
        Step(f"create the group {group}", ("groupadd", "--system", "--force", group)),
    ]
    if not exists:
        steps += [
            Step(
                "create the account",
                (
                    "useradd",
                    "--system",
                    "--create-home",
                    "--home-dir",
                    home,
                    "--gid",
                    group,
                    "--shell",
                    "/usr/sbin/nologin",
                    "--comment",
                    "Octopus CI",
                    account,
                ),
            ),
            Step("keep its home private", ("chmod", "700", home)),
        ]
    steps += [
        Step("create the work root", ("mkdir", "-p", work_root)),
        Step("share it with the account", ("chown", f"{owner}:{group}", work_root)),
        Step("set its mode", ("chmod", "2770", work_root)),
        Step(
            "write the sudo rule (a temporary file)",
            ("tee", sudoers_tmp),
            sudo=False,
            stdin=sudoers_line(owner, account),
        ),
        Step("check the sudo rule", ("visudo", "-cf", sudoers_tmp)),
        Step(
            "install the sudo rule",
            ("install", "-m", "440", "-o", "root", "-g", "root", sudoers_tmp, SUDOERS_FILE),
        ),
        Step("remove the temporary file", ("rm", "-f", sudoers_tmp), sudo=False),
    ]
    return steps


def uninstall_steps_linux(
    *,
    account: str = ACCOUNT,
    group: str = GROUP,
    work_root: str = LINUX_WORK_ROOT,
) -> list[Step]:
    """Linux: remove what `install_steps_linux` made."""
    if not valid_account(account):
        raise LightError(f"invalid account name: {account}")
    root = pure_path(work_root)
    if len(root.parts) < 4 or ".." in root.parts or not root.name.startswith("octopus"):
        raise LightError(f"refusing to delete this work root: {work_root}")
    return [
        Step("remove the sudo rule", ("rm", "-f", SUDOERS_FILE)),
        Step("stop its processes", ("pkill", "-9", "-u", account)),
        Step("delete the account and its home", ("userdel", "--remove", account)),
        Step("delete the group", ("groupdel", group)),
        Step("delete the work root", ("rm", "-rf", work_root)),
    ]


def uninstall_for(light: Light | None) -> list[Step]:
    """The removal steps for the configured level, or for this host's default when none is."""
    light = light or Light(
        backend=default_backend(),
        work_root=LINUX_WORK_ROOT if default_backend() == BUBBLEWRAP else WORK_ROOT,
    )
    if light.backend == BUBBLEWRAP:
        return uninstall_steps_linux(
            account=light.account, work_root=light.work_root
        )
    return uninstall_steps(
        f"/Users/{light.account}", account=light.account, work_root=light.work_root
    )


def uninstall_steps(
    home_dir: str,
    *,
    account: str = ACCOUNT,
    group: str = GROUP,
    work_root: str = WORK_ROOT,
) -> list[Step]:
    """The commands that remove everything `install_steps` made."""
    if not valid_account(account):
        raise LightError(f"invalid account name: {account}")
    expected = f"/Users/{account}"
    if home_dir != expected:
        raise LightError(f"refusing to delete a home that is not {expected}: {home_dir}")
    root = pure_path(work_root)
    if len(root.parts) < 3 or ".." in root.parts or not root.name.startswith("octopus"):
        raise LightError(f"refusing to delete this work root: {work_root}")
    return [
        Step("remove the sudo rule", ("rm", "-f", SUDOERS_FILE)),
        Step("stop its processes", ("pkill", "-9", "-u", account)),
        Step("delete the account", ("dscl", ".", "-delete", f"/Users/{account}")),
        Step("delete its home", ("rm", "-rf", home_dir)),
        Step("delete the group", ("dseditgroup", "-o", "delete", group)),
        Step("delete the work root", ("rm", "-rf", work_root)),
    ]


def run_steps(
    steps: list[Step],
    execute: Exec,
    say: Callable[[str], object] = lambda _text: None,
    *,
    tolerate_failure: bool = False,
) -> None:
    """Run the steps in order. The first failure stops them, unless failures are tolerated."""
    for step in steps:
        say(f"{step.label}: {step.command()}")
        try:
            done = execute(step)
        except (OSError, subprocess.SubprocessError) as e:
            if tolerate_failure:
                continue
            raise LightSetupError(f"could not {step.label}: {e}") from e
        if done.returncode != 0 and not tolerate_failure:
            detail = (done.stderr or "").strip().splitlines()[-1:] or [f"exit {done.returncode}"]
            raise LightSetupError(f"could not {step.label}: {detail[0]}")


def execute_step(step: Step) -> subprocess.CompletedProcess:
    """Run one step for real. `sudo` asks for the admin password on the terminal."""
    argv = ["sudo", *step.argv] if step.sudo else list(step.argv)
    if step.stdin is None:
        return subprocess.run(argv, check=False)
    return subprocess.run(argv, input=step.stdin, text=True, capture_output=True, check=False)


def install(
    owner: str,
    *,
    execute: Exec = execute_step,
    run: Run = _run,
    say: Callable[[str], object] = lambda _text: None,
    sudoers_tmp: str,
    account: str = ACCOUNT,
    work_root: str | None = None,
    backend: str | None = None,
) -> Light:
    """Create the account and its surroundings; undo what was made if a step fails."""
    backend = backend or default_backend()
    linux = backend == BUBBLEWRAP
    work_root = work_root or (LINUX_WORK_ROOT if linux else WORK_ROOT)
    state = inspect(owner, account, run, backend)
    if state.admin:
        raise LightSetupError(f"{account} is an administrator: remove it from the admin group")
    if linux:
        steps = install_steps_linux(
            owner, sudoers_tmp, account=account, work_root=work_root, exists=state.exists
        )
        undo = uninstall_steps_linux(account=account, work_root=work_root)
    else:
        if state.exists and state.uid is not None:
            uid = state.uid
        else:
            uid = pick_uid(run(["dscl", ".", "-list", "/Users", "UniqueID"]).stdout)
        steps = install_steps(
            owner, uid, sudoers_tmp, account=account, work_root=work_root, exists=state.exists
        )
        undo = uninstall_steps(f"/Users/{account}", account=account, work_root=work_root)
    try:
        run_steps(steps, execute, say)
    except LightSetupError:
        if not state.exists:
            run_steps(undo, execute, tolerate_failure=True)
        raise
    return Light(account=account, work_root=work_root, backend=backend)


def readable_secrets(
    owner_home: str, account: str = ACCOUNT, run: Run = _run, exists=os.path.lexists
) -> list[str]:
    """Paths of the owner's that the account can read, which it must not (empty is good)."""
    found = []
    for rel in SECRET_PATHS:
        path = os.path.join(owner_home, rel)
        if not exists(path):
            continue
        done = run(["sudo", "-n", "-u", account, "/bin/test", "-r", path])
        if done.returncode == 0:
            found.append(path)
    return found
