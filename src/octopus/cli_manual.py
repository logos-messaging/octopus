"""`octopus runner unregister-manual`, and the token prompt shared with `octopus init`.

A manual runner is registered by `octopus init --role manual` with a one-hour registration
token, asked for with hidden input (or taken from OCTOPUS_REGISTRATION_TOKEN), never saved
and never put on a command line. No coordinator is involved: see docs/manual-runner.md.
"""

from __future__ import annotations

import os
import subprocess
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


def gh_token(kind: str, target: manual.Target) -> str | None:
    """A one-hour `registration` or `remove` token for `target`, from the user's `gh` login.

    GitHub's API issues it to anyone with admin rights on the target, so nothing is pasted.
    The token comes back on stdout and is never stored. None when gh is missing or refuses.
    """
    scope = f"repos/{target.owner}/{target.repo}" if target.repo else f"orgs/{target.owner}"
    host = target.base.removeprefix("https://")
    try:
        out = subprocess.run(
            ["gh", "api", "--hostname", host, "-X", "POST", f"{scope}/actions/runners/{kind}-token",
             "--jq", ".token"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = out.stdout.strip()
    return token if out.returncode == 0 and token else None


def ask_token(
    what: str,
    page: str,
    env_var: str,
    where: str = "",
    target: manual.Target | None = None,
    kind: str = "registration",
) -> str:
    """The token from `env_var`, then from `gh`, then a hidden prompt. A blank one is an error.

    Registration and removal tokens are different things, so each has its own variable.
    """
    token = os.environ.get(env_var, "").strip()
    if not token and target is not None:
        token = gh_token(kind, target) or ""
        if token:
            typer.echo(f"Got a one-hour {what} token with your gh login (it is not stored).")
    if not token:
        typer.echo(f"Get a {what} token (valid for one hour):\n  1. open {page}")
        typer.echo(
            where
            or "  2. in the Configure box, copy the value after --token in the ./config.sh line"
        )
        token = str(typer.prompt(f"Paste the {what} token (input is hidden)", hide_input=True))
        token = token.strip()
    if not token:
        raise manual.ManualRunnerError(f"no {what} token given")
    return token


def run_manual(directory: Path, base: Path, announce: bool, target: manual.Target) -> int:
    """Run the GitHub runner; with `announce`, also publish its label and target over logos.

    The target (the repo or org the runner is registered on) lets a coordinator in pick mode
    give the runner only jobs of that repo or org.
    """
    from contextlib import ExitStack

    from .agent import manual_presence

    label = manual.saved_label(directory)
    with ExitStack() as stack:
        if announce and label is not None:
            try:
                runner_id = stack.enter_context(manual_presence.announce(label, base, target=target.name))
                typer.echo(f"announcing label {label} as runner: {runner_id}")
            except Exception as e:  # noqa: BLE001 - no node library or network: the runner works
                typer.echo(f"note: could not announce the label: {e}", err=True)
        return manual.start(directory)


def add_manual(runner_app: typer.Typer) -> None:
    @runner_app.command("run-manual")
    def run_manual_cmd(
        target: str = TargetArg,
        data_dir: Path | None = DataDirOpt,
    ) -> None:
        """Run a runner made by `octopus init owner/repo` and announce it. The service runs this."""
        try:
            parsed = manual.parse_target(target)
            base = data_dir_or_default(data_dir)
            directory = manual.runner_dir(base, parsed)
            if not manual.is_configured(directory):
                raise _fail(f"not registered here: run: octopus init {parsed.name}", 1)
            code = run_manual(directory, base, announce=True, target=parsed)
        except manual.ManualRunnerError as e:
            raise _fail(str(e), 1) from e
        raise typer.Exit(code=code)

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
                manual.remove(
                    directory,
                    ask_token(
                        "removal",
                        page,
                        manual.REMOVAL_TOKEN_ENV,
                        "  2. open the runner's menu, choose Remove, and copy the value after "
                        "--token in the ./config.sh remove line",
                        parsed,
                        "remove",
                    ),
                )
                typer.echo(f"unregistered from: {parsed.url}")
        except manual.ManualRunnerError as e:
            raise _fail(str(e), 2 if "could not read runner target" in str(e) else 1) from e
        if not keep_files:
            manual.delete_tree(directory)
            typer.echo(f"deleted: {directory}")


def add_invite(app: typer.Typer) -> None:
    @app.command("invite")
    def invite(target: str = TargetArg) -> None:
        """Print the command that lets a teammate register their machine as a runner for TARGET.

        Run it as a repo admin (org owner for an org): the one-hour registration token comes from
        your own gh login and is only printed, never stored. Send the line privately.
        """
        try:
            parsed = manual.parse_target(target)
        except manual.ManualRunnerError as e:
            raise _fail(str(e), 2) from e
        token = gh_token("registration", parsed)
        if token is None:
            raise _fail(
                f"could not get a token for: {parsed.url}: you need admin rights there and a gh "
                f"login; or copy the token from {parsed.new_runner_page}",
                1,
            )
        typer.echo(
            "Send this to your teammate, privately. It works for one hour and only lets them "
            "register a runner:\n"
        )
        typer.echo(f"  {manual.TOKEN_ENV}={token} octopus init {target.strip()}")
