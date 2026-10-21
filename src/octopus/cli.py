from __future__ import annotations

import os
from pathlib import Path

import typer

from . import __version__
from .cli_github import github_app
from .cli_init import add_init
from .cli_keys import add_coordinator_keygen, root_app
from .cli_lib import lib_app

app = typer.Typer(no_args_is_help=True, help="Octopus: lend your laptop as a CI runner.")
runner_app = typer.Typer(no_args_is_help=True, help="Commands for runner hosts.")
coordinator_app = typer.Typer(no_args_is_help=True, help="Commands for coordinator hosts.")
app.add_typer(runner_app, name="runner")
app.add_typer(coordinator_app, name="coordinator")
app.add_typer(github_app, name="github")
app.add_typer(root_app, name="root")
app.add_typer(lib_app, name="lib")
add_coordinator_keygen(coordinator_app)
add_init(app)


@app.command()
def version() -> None:
    """Print the Octopus version."""
    typer.echo(__version__)


DataDirOption = typer.Option(
    None, "--data-dir", help="Octopus data dir (default: OCTOPUS_DATA_DIR or the platform's)."
)
JsonOption = typer.Option(False, "--json", help="Machine-readable output.")


@app.command()
def jobs(
    failed: bool = typer.Option(False, "--failed", help="Only failed jobs."),
    running: bool = typer.Option(False, "--running", help="Only jobs being worked on."),
    repo: str | None = typer.Option(None, "--repo", help="Only this repo (owner/name)."),
    limit: int = typer.Option(50, "--limit", min=1, help="At most this many, newest first."),
    as_json: bool = JsonOption,
    data_dir: Path | None = DataDirOption,
) -> None:
    """List the coordinator's jobs (run on the coordinator host)."""
    import json
    import time

    from .coordinator.inspect import render_jobs

    inspector = _inspector(data_dir)
    try:
        rows = inspector.list_jobs(failed=failed, running=running, repo=repo, limit=limit)
    finally:
        inspector.close()
    typer.echo(json.dumps(rows, indent=2) if as_json else render_jobs(rows, time.time()))


@app.command()
def logs(
    job_id: str,
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing as it arrives."),
    runner: str | None = typer.Option(
        None, "--runner", help="Runner id of an earlier attempt (default: the latest)."
    ),
    as_json: bool = typer.Option(False, "--json", help="One JSON object per line."),
    data_dir: Path | None = DataDirOption,
) -> None:
    """Show a job's log (and follow it with -f)."""
    import json

    inspector = _inspector(data_dir)
    try:
        if inspector.jobs.get(inspector.jobs.resolve(job_id)) is None:
            raise _fail(f"unknown job: {job_id}", 1)
        if follow:
            try:
                for text in inspector.follow(job_id, runner_id=runner):
                    typer.echo(json.dumps({"data": text}) if as_json else text, nl=as_json)
            except KeyboardInterrupt:
                return
            return
        printed = False
        for chunk in inspector.log_chunks(job_id, runner):
            printed = True
            if as_json:
                typer.echo(chunk.model_dump_json(include={"seq", "data", "final"}))
            else:
                typer.echo(chunk.data, nl=False)
        if not printed:
            typer.echo(f"no log received for job {job_id}", err=True)
    finally:
        inspector.close()


@app.command()
def explain(
    job_id: str,
    lines: int = typer.Option(20, "--lines", "-n", min=0, help="Log lines to show."),
    as_json: bool = JsonOption,
    data_dir: Path | None = DataDirOption,
) -> None:
    """Show a job's timeline, why it failed, and the end of its log."""
    import json
    import time

    from .coordinator.inspect import render_explain

    inspector = _inspector(data_dir)
    try:
        data = inspector.explain(job_id, lines)
    finally:
        inspector.close()
    if data is None:
        raise _fail(f"unknown job: {job_id}", 1)
    typer.echo(json.dumps(data, indent=2) if as_json else render_explain(data, time.time()))


def _inspector(data_dir: Path | None):
    from .coordinator.config import coordinator_dir
    from .coordinator.inspect import Inspector, NoData

    try:
        return Inspector(coordinator_dir(data_dir))
    except NoData as e:
        raise _fail(f"{e} (run this on the coordinator host, or pass --data-dir)", EXIT_BAD_CONFIG) from e


# The node's own state (reliable channels), inside the data dir of each role.
NODE_DIR = "node"

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

    transport = LogosTransport(settings.node_config(data_dir / NODE_DIR))
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


@runner_app.command("install-service")
def runner_install_service(config: Path | None = ConfigOption) -> None:
    """Start the runner automatically at login (launchd, systemd --user or Task Scheduler)."""
    from .agent import service

    settings, _ = _resolve_settings(config, None, None, None, None)
    allowlist, _ = _load_allowlist(config)
    if not settings.coordinators and not settings.root_key:
        typer.echo(
            "warning: no trusted coordinator yet, the service will exit until you run: "
            "octopus runner setup --coordinator <key> or --root-key <key>",
            err=True,
        )
    if not allowlist.orgs and not allowlist.repos:
        typer.echo(
            "warning: nothing is allowed yet, run: octopus runner allow <org/repo>", err=True
        )
    try:
        result = service.install(service.runner_spec(config))
    except service.ServiceError as e:
        raise _fail(f"could not install the service: {e}", 1) from e
    typer.echo(f"service installed: {result.path}")
    for note in result.notes:
        typer.echo(f"note: {note}")


@runner_app.command("uninstall-service")
def runner_uninstall_service() -> None:
    """Remove the automatic start. Settings, keys and the allowlist are kept."""
    from .agent import service

    try:
        result = service.uninstall()
    except service.ServiceError as e:
        raise _fail(f"could not uninstall the service: {e}", 1) from e
    typer.echo(f"service removed: {result.path}")


@runner_app.command("service-status")
def runner_service_status() -> None:
    """Show whether the automatic start is installed and active."""
    from .agent import service

    try:
        installed, description = service.status()
    except service.ServiceError as e:
        raise _fail(f"could not query the service: {e}", 1) from e
    typer.echo(f"service: {description}")
    if not installed:
        raise typer.Exit(code=EXIT_NOT_RUNNING)


EnvFileOption = typer.Option(
    None, "--env-file", help="OCTOPUS_* settings, e.g. the coordinator.env from github setup."
)


@coordinator_app.command("start")
def coordinator_start(
    env_file: Path | None = EnvFileOption,
    listen: str | None = typer.Option(
        None, "--listen", help="Webhook HTTP address, host:port (default: 127.0.0.1:8080)."
    ),
    data_dir: Path | None = DataDirOption,
    connect_timeout: float = typer.Option(60.0, help="Seconds to wait for the network."),
) -> None:
    """Start the coordinator: webhook endpoint, scheduler, log collection and the node.

    Runs in the foreground until Ctrl-C. Settings come from OCTOPUS_* variables: the
    environment over --env-file.
    """
    from .coordinator.config import (
        WEBHOOK_LISTEN_ENV,
        ConfigError,
        CoordinatorConfig,
        coordinator_dir,
        merged_env,
    )
    from .coordinator.service import Coordinator, read_state
    from .transport.logos import LogosError, LogosLibraryError, LogosTransport

    if env_file is not None and not env_file.is_file():
        raise _fail(f"no such env file: {env_file}", EXIT_USAGE)
    try:
        env = merged_env(env_file)
        if listen:
            env[WEBHOOK_LISTEN_ENV] = listen
        config = CoordinatorConfig.from_env(env)
    except ConfigError as e:
        raise _fail(str(e), EXIT_BAD_CONFIG) from e
    directory = coordinator_dir(data_dir)
    running = read_state(directory)
    if running:
        raise _fail(f"already running: pid: {running['pid']}", 1)
    github = _github_client(config)
    transport = LogosTransport(config.node_config(directory / NODE_DIR))
    coordinator = Coordinator(config, transport, github, directory)
    try:
        coordinator.start()
    except LogosLibraryError as e:
        raise _fail(f"could not load liblogosdelivery, see the README: {e}", EXIT_TRANSPORT) from e
    except LogosError as e:
        raise _fail(f"could not start the node: {e}", EXIT_TRANSPORT) from e
    except OSError as e:
        address = f"{config.listen_host}:{config.listen_port}"
        raise _fail(f"could not listen for webhooks on {address}: {e}", EXIT_TRANSPORT) from e
    coordinator.install_signal_handlers()
    if not transport.wait_connected(connect_timeout):
        typer.echo("warning: not connected to the network yet, still trying", err=True)
    _print_coordinator(coordinator.state())
    typer.echo("Ctrl-C to stop")
    coordinator.wait()
    typer.echo("coordinator stopped")


@coordinator_app.command("status")
def coordinator_status(as_json: bool = JsonOption, data_dir: Path | None = DataDirOption) -> None:
    """Show whether the coordinator runs, how runners reach it, and its job counts."""
    import json

    from .coordinator.config import coordinator_dir
    from .coordinator.inspect import Inspector, NoData
    from .coordinator.service import read_state

    directory = coordinator_dir(data_dir)
    state = read_state(directory)
    try:
        inspector = Inspector(directory)
        counts = inspector.jobs.counts()
        inspector.close()
    except NoData:
        counts = {}
    if as_json:
        typer.echo(json.dumps({"running": state is not None, "state": state, "jobs": counts}, indent=2))
    elif state is None:
        typer.echo("not running")
    else:
        _print_coordinator(state)
        typer.echo(f"pid             {state['pid']}")
    if not as_json and counts:
        typer.echo("jobs            " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if state is None:
        raise typer.Exit(code=EXIT_NOT_RUNNING)


def _github_client(config):
    from .github.app import GitHubApp
    from .github.token import TokenClient

    if config.auth_mode == "token":
        return TokenClient(config.github_token, api_url=config.api_url)
    try:
        app_client = GitHubApp.from_key_file(config.app_id, config.app_key_file, api_url=config.api_url)
        app_client.app_jwt()  # fails now, not at the first job, if the key is unusable
    except Exception as e:  # any key problem is a config error
        raise _fail(f"could not use the GitHub App key: {type(e).__name__}", EXIT_BAD_CONFIG) from e
    return app_client


def _print_coordinator(state: dict) -> None:
    typer.echo(f"coordinator     {state['coordinator_id']}")
    typer.echo(f"public key      {state['public_key']}")
    typer.echo(f"                runners: octopus runner setup --coordinator {state['public_key']}")
    for addr in state.get("multiaddrs") or []:
        typer.echo(f"entry node      {addr}")
    if state.get("cluster_key"):
        peers = ", ".join(state.get("peers") or []) or "none configured"
        typer.echo(f"cluster key     {state['cluster_key']}")
        typer.echo(f"                peers: {peers}; give this key to the others as OCTOPUS_COORDINATOR_PEERS={state['coordinator_id']}=<key>")
    webhook = state.get("webhook") or "-"
    public = state.get("webhook_url")
    typer.echo(f"webhook         {webhook}" + (f" (public: {public})" if public else ""))
    allowed = state.get("allowed_repos", []) + [f"{o}/*" for o in state.get("allowed_orgs", [])]
    typer.echo(f"allowed         {', '.join(allowed) or '-'}  (auth: {state.get('auth', '?')})")
    import time

    age = ""
    if state.get("updated_at"):
        age = f" (as of {max(0, int(time.time() - state['updated_at']))}s ago)"
    typer.echo(f"runners online  {state.get('runners_online', 0)}{age}")
