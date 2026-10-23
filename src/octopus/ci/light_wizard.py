"""The `octopus ci init` step that sets up the light level: the dedicated `octopus` account.

It says what the account is and shows every command before anything runs. Creating it needs the
machine's admin password, so it only runs with a terminal: with none it points at
`octopus ci light install` and changes nothing.
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import typer

from ..github.setup import Check, Status
from . import light_account
from .home import Home
from .light import ACCOUNT, WORK_ROOT, Light, LightError

EXPLANATION = f"""Jobs will run as a separate account, not as you.
Octopus creates a hidden, non-admin account called "{ACCOUNT}" on this Mac and runs every CI job
as that account, in a throwaway folder, under a sandbox profile. It has no admin rights, no
keychain and no access to your files, SSH keys or GitHub login, so a job cannot read them.
This needs your admin password (sudo) once. You will see each command first. To remove
everything later: octopus ci uninstall.
It is a policy on a shared kernel, not a virtual machine: see docs/threat-model.md."""

LIGHT_NOTICE = (
    "Jobs of {name} run as the account {account} under a sandbox profile: they cannot read your\n"
    "files, SSH keys, keychain or GitHub login. With network {network} they still reach the\n"
    "internet. See docs/decentralized-ci.md for what the profile does and does not stop."
)


def has_terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def offer_light(
    home: Home,
    *,
    assume_yes: bool,
    say: Callable[[str], object] = typer.echo,
    confirm: Callable[..., bool] = typer.confirm,
    execute: light_account.Exec = light_account.execute_step,
    run: light_account.Run = light_account._run,
    terminal: Callable[[], bool] = has_terminal,
    owner: str | None = None,
    owner_home: str | None = None,
) -> Check:
    """Offer the light level on a Mac, set it up when the user agrees."""
    try:
        current = home.light()
    except LightError as e:
        return Check("isolation", Status.FAIL, f"{e}: run: octopus ci light disable")
    if current is not None:
        why = current.supported()
        if why is not None:
            return Check("isolation", Status.FAIL, f"{why}")
        return Check("isolation", Status.OK, current.describe())
    if not terminal():
        return Check(
            "isolation",
            Status.SKIPPED,
            "needs the admin password in a terminal: run: octopus ci light install",
        )
    owner = owner or getpass.getuser()
    say(EXPLANATION)
    tmp = _temp_name()
    try:
        state = light_account.inspect(owner, ACCOUNT, run)
        uid = state.uid if state.uid is not None else light_account.pick_uid(
            run(["dscl", ".", "-list", "/Users", "UniqueID"]).stdout
        )
        steps = light_account.install_steps(owner, uid, tmp, exists=state.exists)
    except (LightError, light_account.LightSetupError) as e:
        return Check("isolation", Status.FAIL, str(e))
    say("These commands will run:")
    for step in steps:
        say(f"  {step.command()}")
    if not assume_yes and not confirm("Create the account and run these commands?", default=True):
        return Check("isolation", Status.SKIPPED, "jobs run as you: octopus ci light install")
    try:
        light = light_account.install(
            owner, execute=execute, run=run, say=say, sudoers_tmp=tmp
        )
    except (LightError, light_account.LightSetupError) as e:
        return Check("isolation", Status.FAIL, f"{e}; nothing was left half-installed")
    finally:
        Path(tmp).unlink(missing_ok=True)
    home.save_light(light)
    exposed = light_account.readable_secrets(owner_home or str(Path.home()), ACCOUNT, run)
    for path in exposed:
        say(f"warning: the account can read {path}: run: chmod -R go-rwx {path}")
    say("To remove the account and its folders later: octopus ci uninstall")
    return Check("isolation", Status.CHANGED, f"jobs run as {ACCOUNT} ({light.describe()})")


def _temp_name() -> str:
    fd, name = tempfile.mkstemp(prefix="octopus-sudoers-")
    os.close(fd)
    return name


__all__ = ["LIGHT_NOTICE", "WORK_ROOT", "Light", "offer_light"]
