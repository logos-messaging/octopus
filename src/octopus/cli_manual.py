"""`octopus runner unregister-manual`, and the token prompt shared with `octopus init`.

A manual runner is registered by `octopus init --role manual` with a one-hour registration
token, asked for with hidden input (or taken from OCTOPUS_REGISTRATION_TOKEN), never saved
and never put on a command line. No coordinator is involved: see docs/manual-runner.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from .agent import manual

TargetArg = typer.Argument(..., help="owner/repo, owner (an org), or the https URL of either.")
KeepFilesOpt = typer.Option(
    False, "--keep-files", help="Unregister from GitHub but keep the local runner tree."
)
DataDirOpt = typer.Option(
    None, "--data-dir", help="Octopus data dir (default: OCTOPUS_DATA_DIR or the platform's)."
)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def data_dir_or_default(path: Path | None) -> Path:
    from .agent.runner import default_data_dir

    return path or default_data_dir()


def ask_token(what: str, page: str) -> str:
    """The token from the environment, or a hidden prompt. A blank one is an error."""
    token = os.environ.get(manual.TOKEN_ENV, "").strip()
    if not token:
        typer.echo(f"Get a {what} token (valid for one hour) at:\n  {page}")
        token = str(typer.prompt(f"Paste the {what} token (input is hidden)", hide_input=True))
        token = token.strip()
    if not token:
        raise manual.ManualRunnerError(f"no {what} token given")
    return token


def add_manual(runner_app: typer.Typer) -> None:
    @runner_app.command("unregister-manual")
    def unregister_manual(
        target: str = TargetArg,
        keep_files: bool = KeepFilesOpt,
        data_dir: Path | None = DataDirOpt,
    ) -> None:
        """Remove a runner made by `octopus init --role manual` from GitHub and delete its files."""
        try:
            parsed = manual.parse_target(target)
            directory = manual.runner_dir(data_dir_or_default(data_dir), parsed)
            if not directory.is_dir():
                typer.echo(f"nothing registered here for: {parsed.url}")
                return
            if manual.is_configured(directory):
                page = parsed.new_runner_page.removesuffix("/new")
                manual.remove(directory, ask_token("removal", page))
                typer.echo(f"unregistered from: {parsed.url}")
        except manual.ManualRunnerError as e:
            raise _fail(str(e), 2 if "could not read runner target" in str(e) else 1) from e
        if not keep_files:
            manual.delete_tree(directory)
            typer.echo(f"deleted: {directory}")
