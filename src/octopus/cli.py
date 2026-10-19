from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(no_args_is_help=True, help="Octopus: lend your laptop as a CI runner.")
runner_app = typer.Typer(no_args_is_help=True, help="Commands for runner hosts.")
coordinator_app = typer.Typer(no_args_is_help=True, help="Commands for coordinator hosts.")
app.add_typer(runner_app, name="runner")
app.add_typer(coordinator_app, name="coordinator")


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


@runner_app.command("allow")
def runner_allow(repo_or_org: str) -> None:
    """Allow jobs from a repo (org/name) or an org."""
    _todo("runner allow")


@coordinator_app.command("start")
def coordinator_start() -> None:
    """Start the coordinator (webhooks, scheduler, bootstrap node)."""
    _todo("coordinator start")


@coordinator_app.command("status")
def coordinator_status() -> None:
    _todo("coordinator status")
