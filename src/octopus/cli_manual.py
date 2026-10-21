"""`octopus runner register-manual` / `unregister-manual`: a plain GitHub runner, by hand.

A guided way to register this machine with a one-hour registration token. The token is
asked for with hidden input (or taken from OCTOPUS_REGISTRATION_TOKEN), never saved and
never put on a command line. No coordinator is involved: see docs/manual-runner.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from .agent import manual

TargetArg = typer.Argument(..., help="owner/repo, owner (an org), or the https URL of either.")
LabelsOpt = typer.Option(
    None, "--labels", help="Extra labels (repeatable). `octopus` is always added."
)
NameOpt = typer.Option(None, "--name", help="Runner name in GitHub.")
NoStartOpt = typer.Option(
    False, "--no-start", help="Register only; start it later with the same command."
)
ReconfigureOpt = typer.Option(
    False, "--reconfigure", help="Register again even if this target is configured."
)
KeepFilesOpt = typer.Option(
    False, "--keep-files", help="Unregister from GitHub but keep the local runner tree."
)
DataDirOpt = typer.Option(
    None, "--data-dir", help="Octopus data dir (default: OCTOPUS_DATA_DIR or the platform's)."
)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _data_dir(path: Path | None) -> Path:
    from .agent.runner import default_data_dir

    return path or default_data_dir()


def _target(text: str) -> manual.Target:
    try:
        return manual.parse_target(text)
    except manual.ManualRunnerError as e:
        raise _fail(str(e), 2) from e


def _token(what: str, page: str) -> str:
    """The token from the environment, or a hidden prompt. Empty is an error."""
    token = os.environ.get(manual.TOKEN_ENV, "").strip()
    if not token:
        typer.echo(f"Get a {what} token (valid for one hour) at:\n  {page}")
        token = str(typer.prompt(f"Paste the {what} token (input is hidden)", hide_input=True))
        token = token.strip()
    if not token:
        raise _fail(f"no {what} token given", 2)
    return token


def add_manual(runner_app: typer.Typer) -> None:
    @runner_app.command("register-manual")
    def register_manual(
        target: str = TargetArg,
        labels: list[str] | None = LabelsOpt,
        name: str | None = NameOpt,
        no_start: bool = NoStartOpt,
        reconfigure: bool = ReconfigureOpt,
        data_dir: Path | None = DataDirOpt,
    ) -> None:
        """Register this machine as a plain GitHub runner with a one-hour token, then run it.

        The runner takes jobs from GitHub directly: no coordinator, no scheduling and no
        Octopus trust policy. Use it to try a workflow on a laptop.
        """
        parsed = _target(target)
        base = _data_dir(data_dir)
        typer.echo(
            "note: this is a plain GitHub runner. Octopus does not schedule it or check who\n"
            "      triggered the job: any workflow in the target that matches its labels runs here."
        )
        try:
            directory = manual.prepare(base, parsed)
            if reconfigure or not manual.is_configured(directory):
                token = _token("registration", parsed.new_runner_page)
                all_labels = [*manual.DEFAULT_LABELS, *(labels or [])]
                runner_name = name or manual.default_name()
                manual.configure(directory, parsed, token, name=runner_name, labels=all_labels)
                typer.echo(
                    f"registered: {runner_name} for {parsed.url} "
                    f"(labels: self-hosted, {', '.join(all_labels)})"
                )
            else:
                typer.echo(f"already registered, using: {directory}")
            if no_start:
                typer.echo("start it with: octopus runner register-manual " + target)
                return
            typer.echo("starting the runner; Ctrl-C to stop (it stays registered)")
            code = manual.start(directory)
        except manual.ManualRunnerError as e:
            raise _fail(str(e)) from e
        if code != 0:
            raise _fail(f"runner exited: code: {code}")

    @runner_app.command("unregister-manual")
    def unregister_manual(
        target: str = TargetArg,
        keep_files: bool = KeepFilesOpt,
        data_dir: Path | None = DataDirOpt,
    ) -> None:
        """Remove a runner made by register-manual from GitHub, then delete its local files."""
        parsed = _target(target)
        directory = manual.runner_dir(_data_dir(data_dir), parsed)
        if not directory.is_dir():
            typer.echo(f"nothing registered here for: {parsed.url}")
            return
        try:
            if manual.is_configured(directory):
                page = parsed.new_runner_page.removesuffix("/new")
                token = _token("removal", page)
                manual.remove(directory, token)
                typer.echo(f"unregistered from: {parsed.url}")
        except manual.ManualRunnerError as e:
            raise _fail(str(e)) from e
        if not keep_files:
            manual.delete_tree(directory)
            typer.echo(f"deleted: {directory}")
