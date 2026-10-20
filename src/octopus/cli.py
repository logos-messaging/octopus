from __future__ import annotations

from pathlib import Path

import typer

from . import __version__
from .cli_github import github_app

app = typer.Typer(no_args_is_help=True, help="Octopus: lend your laptop as a CI runner.")
runner_app = typer.Typer(no_args_is_help=True, help="Commands for runner hosts.")
coordinator_app = typer.Typer(no_args_is_help=True, help="Commands for coordinator hosts.")
app.add_typer(runner_app, name="runner")
app.add_typer(coordinator_app, name="coordinator")
app.add_typer(github_app, name="github")


def _todo(what: str) -> None:
    typer.echo(f"{what}: not implemented yet")
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the Octopus version."""
    typer.echo(__version__)


@app.command()
def jobs(failed: bool = typer.Option(False, "--failed", help="Only failed jobs")) -> None:
    """List jobs known to the coordinator."""
    _todo("jobs")


@app.command()
def logs(job_id: str, follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    """Show (and optionally follow) the logs of a job."""
    _todo("logs")


@app.command()
def explain(job_id: str) -> None:
    """Show the timeline and failure reason of a job."""
    _todo("explain")


@runner_app.command("start")
def runner_start() -> None:
    """Start offering this machine as a runner."""
    _todo("runner start")


@runner_app.command("stop")
def runner_stop() -> None:
    _todo("runner stop")


@runner_app.command("status")
def runner_status() -> None:
    _todo("runner status")


ConfigOption = typer.Option(
    None, "--config", help="Runner config file (default: the platform config dir)."
)


def _load_allowlist(config: Path | None):
    from .agent.trust import Allowlist, default_config_path

    path = config or default_config_path()
    try:
        return Allowlist.load(path), path
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _edit_allowlist(config: Path | None, target: str, add: bool) -> None:
    from .agent.trust import InvalidTarget

    allowlist, path = _load_allowlist(config)
    try:
        changed = allowlist.add(target) if add else allowlist.remove(target)
    except InvalidTarget as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=2) from e
    if changed:
        allowlist.save(path)
    verb = ("allowed", "already allowed") if add else ("denied", "was not allowed")
    typer.echo(f"{target}: {verb[0] if changed else verb[1]}")


@runner_app.command("allow")
def runner_allow(repo_or_org: str, config: Path | None = ConfigOption) -> None:
    """Allow jobs from a repo (org/name) or an org."""
    _edit_allowlist(config, repo_or_org, add=True)


@runner_app.command("deny")
def runner_deny(repo_or_org: str, config: Path | None = ConfigOption) -> None:
    """Stop accepting jobs from a repo (org/name) or an org."""
    _edit_allowlist(config, repo_or_org, add=False)


@runner_app.command("list")
def runner_list(config: Path | None = ConfigOption) -> None:
    """Show what this runner is allowed to run."""
    allowlist, _ = _load_allowlist(config)
    if not allowlist.orgs and not allowlist.repos:
        typer.echo("nothing allowed yet: this runner rejects every job (see: octopus runner allow)")
        return
    for org in sorted(allowlist.orgs):
        typer.echo(f"org   {org}")
    for repo in sorted(allowlist.repos):
        typer.echo(f"repo  {repo}")


@coordinator_app.command("start")
def coordinator_start() -> None:
    """Start the coordinator (webhooks, scheduler, bootstrap node)."""
    _todo("coordinator start")


@coordinator_app.command("status")
def coordinator_status() -> None:
    _todo("coordinator status")
