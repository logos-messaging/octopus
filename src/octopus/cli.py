from __future__ import annotations

import os
from pathlib import Path

import typer

from . import __version__
from .cli_github import github_app
from .cli_keys import add_coordinator_keygen, root_app

app = typer.Typer(no_args_is_help=True, help="Octopus: lend your laptop as a CI runner.")
runner_app = typer.Typer(no_args_is_help=True, help="Commands for runner hosts.")
coordinator_app = typer.Typer(no_args_is_help=True, help="Commands for coordinator hosts.")
app.add_typer(runner_app, name="runner")
app.add_typer(coordinator_app, name="coordinator")
app.add_typer(github_app, name="github")
app.add_typer(root_app, name="root")
add_coordinator_keygen(coordinator_app)


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


EXIT_USAGE = 2
EXIT_BAD_CONFIG = 3
EXIT_TRANSPORT = 4
EXIT_NOT_RUNNING = 5

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


EntryNodeOption = typer.Option(
    None, "--entry-node", help="Extra logos-delivery entry node multiaddr (repeatable)."
)
CoordinatorOption = typer.Option(
    None,
    "--coordinator",
    help="Public key of a coordinator to trust (repeatable). Printed by the coordinator.",
)
RootKeyOption = typer.Option(
    None,
    "--root-key",
    help="Pinned root public key: discover the coordinators it authorises (or set OCTOPUS_ROOT_KEY).",
)
LabelsOption = typer.Option(None, "--labels", help="Extra runner labels (repeatable).")
MaxJobsOption = typer.Option(None, "--max-jobs", min=1, help="Jobs to run at the same time.")


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _resolve_settings(
    config: Path | None,
    entry_nodes: list[str] | None,
    coordinators: list[str] | None,
    labels: list[str] | None,
    max_jobs: int | None,
    root_key: str | None = None,
):
    from .agent.config import ConfigError, RunnerConfig, settings_path
    from .agent.trust import default_config_path

    path = settings_path(config or default_config_path())
    try:
        saved = RunnerConfig.load(path)
        merged = saved.merged(
            os.environ,
            entry_nodes=entry_nodes or (),
            coordinators=coordinators or (),
            labels=labels or (),
            max_jobs=max_jobs,
            root_key=root_key,
        )
    except ConfigError as e:
        raise _fail(str(e), EXIT_BAD_CONFIG) from e
    return merged, path


@runner_app.command("setup")
def runner_setup(
    config: Path | None = ConfigOption,
    entry_node: list[str] | None = EntryNodeOption,
    coordinator: list[str] | None = CoordinatorOption,
    labels: list[str] | None = LabelsOption,
    max_jobs: int | None = MaxJobsOption,
    root_key: str | None = RootKeyOption,
) -> None:
    """Save this runner's settings (entry nodes, trusted coordinators, labels, max jobs)."""
    from .agent.config import ConfigError

    settings, path = _resolve_settings(config, entry_node, coordinator, labels, max_jobs, root_key)
    try:
        settings.save(path)
    except ConfigError as e:
        raise _fail(str(e), EXIT_BAD_CONFIG) from e
    typer.echo(f"saved settings: {path}")
    if not settings.coordinators and not settings.root_key:
        typer.echo(
            "note: no coordinator trusted yet; add one with: octopus runner setup --coordinator <key> or --root-key <key>"
        )


@runner_app.command("start")
def runner_start(
    config: Path | None = ConfigOption,
    entry_node: list[str] | None = EntryNodeOption,
    coordinator: list[str] | None = CoordinatorOption,
    labels: list[str] | None = LabelsOption,
    max_jobs: int | None = MaxJobsOption,
    root_key: str | None = RootKeyOption,
    allow_empty: bool = typer.Option(
        False, "--allow-empty", help="Start even if nothing is allowed (rejects every job)."
    ),
    connect_timeout: float = typer.Option(60.0, help="Seconds to wait for the network."),
) -> None:
    """Offer this machine as a runner (runs in the foreground until Ctrl-C)."""
    from .agent.runner import default_data_dir
    from .agent.runner_agent import RunnerAgent, read_state
    from .transport.logos import LogosError, LogosLibraryError, LogosTransport

    settings, _ = _resolve_settings(config, entry_node, coordinator, labels, max_jobs, root_key)
    allowlist, _ = _load_allowlist(config)
    if not settings.coordinators and not settings.root_key:
        raise _fail(
            "no trusted coordinator: run: octopus runner setup --coordinator <key> or --root-key <key>",
            EXIT_BAD_CONFIG,
        )
    if not allowlist.orgs and not allowlist.repos and not allow_empty:
        raise _fail(
            "nothing is allowed, this runner would reject every job: "
            "run: octopus runner allow <org/repo> (or pass --allow-empty)",
            EXIT_BAD_CONFIG,
        )
    data_dir = default_data_dir()
    running = read_state(data_dir)
    if running:
        raise _fail(f"already running: pid: {running['pid']}", 1)

    transport = LogosTransport(settings.node_config())
    agent = RunnerAgent(settings, allowlist, transport, data_dir=data_dir)
    try:
        agent.start()
    except LogosLibraryError as e:
        raise _fail(f"could not load liblogosdelivery, see the README: {e}", EXIT_TRANSPORT) from e
    except LogosError as e:
        raise _fail(f"could not start the node: {e}", EXIT_TRANSPORT) from e
    agent.install_signal_handlers()
    if not transport.wait_connected(connect_timeout):
        typer.echo("warning: not connected to the network yet, still trying", err=True)
    typer.echo(f"runner online: {agent.runner_id} (max jobs: {settings.max_jobs}); Ctrl-C to stop")
    agent.wait()
    typer.echo("runner stopped")


@runner_app.command("status")
def runner_status() -> None:
    """Show whether the runner is running and how busy it is."""
    from .agent.runner import default_data_dir
    from .agent.runner_agent import read_state

    state = read_state(default_data_dir())
    if state is None:
        typer.echo("not running")
        raise typer.Exit(code=EXIT_NOT_RUNNING)
    typer.echo(
        f"running: pid {state['pid']}, runner {state['runner_id']}, "
        f"busy {state['busy']}/{state['max_jobs']}"
    )


@runner_app.command("stop")
def runner_stop(timeout: float = typer.Option(60.0, help="Seconds to wait for shutdown.")) -> None:
    """Stop the running runner (it finishes shutting down running jobs first)."""
    import signal
    import time

    from .agent.runner import default_data_dir
    from .agent.runner_agent import pid_alive, read_state

    state = read_state(default_data_dir())
    if state is None:
        typer.echo("not running")
        raise typer.Exit(code=EXIT_NOT_RUNNING)
    pid = state["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        raise _fail(f"could not stop runner: pid: {pid}: {e}", 1) from e
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() > deadline:
            raise _fail(f"runner did not stop in time: pid: {pid}", 1)
        time.sleep(0.2)
    typer.echo("stopped")


@coordinator_app.command("start")
def coordinator_start() -> None:
    """Start the coordinator (webhooks, scheduler, bootstrap node)."""
    _todo("coordinator start")


@coordinator_app.command("status")
def coordinator_status() -> None:
    _todo("coordinator status")
