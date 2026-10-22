"""`octopus update`: install the latest release and restart the node."""

from __future__ import annotations

import subprocess
import sys

import typer

from . import __version__, update

# Imported now, not after the install: by then the files on disk are the new version's, and this
# process must keep running the code it started with.
from .agent import service


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _run(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError as e:
        typer.echo(f"error: could not run {argv[0]}: {e.strerror}", err=True)
        return 127


def add_update(app: typer.Typer) -> None:
    @app.command("update")
    def update_command(
        ref: str | None = typer.Option(
            None, "--ref", help="Tag, branch or commit to install (default: the latest release)."
        ),
        check: bool = typer.Option(False, "--check", help="Only say if a newer release exists."),
        no_lib: bool = typer.Option(False, "--no-lib", help="Do not refresh the native library."),
        no_restart: bool = typer.Option(
            False, "--no-restart", help="Do not restart the installed services."
        ),
    ) -> None:
        """Install the latest Octopus, refresh the native library and restart the node."""
        if sys.platform == "win32":
            raise _fail(f"on Windows, rerun the installer: {update.WINDOWS_HINT}")
        try:
            target = update.check_ref(ref) if ref else update.latest_release()
            method = update.install_method()
            if ref is None and not update.is_newer(target, __version__):
                typer.echo(f"up to date: {__version__}")
                return
            if check:
                typer.echo(f"update available: {__version__} -> {target}")
                raise typer.Exit(code=10)
            argv = update.upgrade_argv(method, target)
        except update.UpdateError as e:
            raise _fail(str(e)) from e

        typer.echo(f"updating {__version__} -> {target} ({method})")
        if _run(argv) != 0:
            raise _fail("could not install the update, see the output above", 4)
        # The new code is on disk now, but this process still runs the old one: ask a new one.
        _run([sys.executable, "-m", "octopus", "version"])
        if not no_lib and _run([sys.executable, "-m", "octopus", "lib", "install"]) != 0:
            typer.echo("warning: could not refresh the native library: octopus lib install", err=True)
        if not no_restart:
            _restart_services()


def _restart_services() -> None:
    try:
        names = service.installed_names()
    except service.ServiceError as e:
        typer.echo(f"warning: could not look for services: {e}", err=True)
        return
    if not names:
        typer.echo("no service installed: start the node again if it is running")
        return
    for name in names:
        try:
            service.restart(name)
        except service.ServiceError as e:
            typer.echo(f"warning: {e}", err=True)
        else:
            typer.echo(f"restarted: {name}")
