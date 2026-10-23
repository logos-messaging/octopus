"""`octopus ci` (nodes and the submit side of Octopus CI) and `octopus team` (membership)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import typer

from .github.setup import Check, Status
from .process import leave

ci_app = typer.Typer(no_args_is_help=True, help="Octopus CI: team machines run Linux/macOS legs.")
team_app = typer.Typer(no_args_is_help=True, help="The team: who may run CI legs.")

DataDirOpt = typer.Option(
    None, "--data-dir", help="Octopus data dir (default: OCTOPUS_DATA_DIR or the platform's)."
)
SshKeyOpt = typer.Option(
    None, "--ssh-key", help="Your Ed25519 SSH key (default: ~/.ssh/id_ed25519)."
)
LoginOpt = typer.Option(None, "--login", help="Your GitHub login (default: gh's).")
NoServiceOpt = typer.Option(False, "--no-service", help="Do not install the service.")
YesOpt = typer.Option(
    False, "--yes", help="Do not ask to confirm that jobs run as you (the notice is still shown)."
)
OsOpt = typer.Option(..., "--os", help="An OS to ask for (repeatable).")
TeamKeyOpt = typer.Option(
    "", "--team-key", envvar="OCTOPUS_TEAM_KEY", help="The team's public key."
)
EntryNodeOpt = typer.Option([], "--entry-node", help="Extra entry node multiaddr.")
WaitOpt = typer.Option(
    45.0, "--wait", help="Seconds to wait for an idle machine to take each leg."
)
BusyWaitOpt = typer.Option(
    240.0, "--busy-wait", help="Seconds to wait for a busy machine of an OS to be free."
)
FollowOpt = typer.Option(
    False,
    "--follow",
    help="Stay until every leg taken ends: print its log, report its result, fall back when "
    "its machine goes silent.",
)
HostedOpt = typer.Option(
    [], "--hosted", help="An OS that always runs on GitHub (repeatable); only shown in the summary."
)
SilentOpt = typer.Option(
    90.0, "--silent-timeout", help="With --follow: seconds of silence after which a machine is lost."
)
MaxRunOpt = typer.Option(
    5400.0, "--max-run", help="With --follow: seconds to wait for a leg to end before it times out."
)
OrgOpt = typer.Option([], "--org", help="Add the members of this organization.")
RepoOpt = typer.Option([], "--repo", help="Add this repo's collaborators with write access.")
LoginsOpt = typer.Option(
    [], "--login", help="Add this GitHub login to these scopes, now and in every later sync."
)
ForgetLoginOpt = typer.Option(
    [], "--forget-login", help="Stop adding this login to these scopes (it leaves at this sync)."
)
ForceOpt = typer.Option(False, "--force", help="Accept a list that shrinks sharply.")


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _home(data_dir: Path | None):
    from .ci.home import ci_home

    return ci_home(data_dir)


def _isolation_line(home) -> str:
    from .ci.isolation import HOST_DESCRIPTION
    from .ci.light import LightError
    from .ci.sandbox import SandboxError

    try:
        sandbox = home.sandbox()
        light = home.light()
    except (SandboxError, LightError) as e:
        return f"unreadable settings: {e}"
    chosen = light or sandbox
    if chosen is None:
        return HOST_DESCRIPTION
    why = chosen.supported()
    return chosen.describe() + (f" (NOT USABLE: {why})" if why else "")


def _show(check: Check) -> Check:
    typer.echo(f"[{check.status.value:>7}] {check.name}  {check.detail}")
    return check


# --- ci -------------------------------------------------------------------------------


@ci_app.command("init")
def ci_init(
    target: str = typer.Argument(..., help="owner/repo (or an org) whose team to join."),
    ssh_key: Path | None = SshKeyOpt,
    login: str | None = LoginOpt,
    no_service: bool = NoServiceOpt,
    yes: bool = YesOpt,
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Join the team as a member, or as its admin: Octopus finds out which you are.

    This does not register a GitHub runner: for that, run `octopus init owner/repo`.
    """
    from .agent.runner import default_data_dir
    from .ci.gh import Gh
    from .ci.wizard import init_ci

    base = data_dir or default_data_dir()
    checks = init_ci(
        target,
        _home(base),
        Gh(),
        now=time.time(),
        ssh_key=ssh_key,
        login=login,
        data_dir=base,
        show=_show,
        start_service=not no_service,
        assume_yes=yes,
    )
    if any(c.failed for c in checks):
        raise typer.Exit(code=1)
    if no_service:
        typer.echo("next: octopus ci node")


@ci_app.command("node")
def ci_node(
    connect_timeout: float = typer.Option(60.0, help="Seconds to wait for the network."),
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Run this machine as a CI node (what the service runs)."""
    import logging
    import signal
    import threading

    from .agent.config import ConfigError, RunnerConfig, settings_path
    from .ci.home import ci_home
    from .ci.runtime import CiRuntime, RuntimeError_, Settings
    from .ci.wizard import os_name
    from .transport.logos import LogosError, LogosLibraryError, LogosTransport

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    home = ci_home(data_dir)
    try:
        settings = RunnerConfig.load(settings_path())
    except ConfigError as e:
        raise _fail(str(e), 3) from e
    try:
        node_config = settings.node_config(home.node_dir)
    except ValueError as e:  # e.g. an invalid OCTOPUS_LOG_LEVEL
        raise _fail(str(e), 3) from e
    transport = LogosTransport(node_config, fast_stop=True)
    import platform

    try:
        runtime = CiRuntime(
            home,
            transport,
            Settings(
                os_name=os_name(),
                arch=platform.machine().lower(),
                name=home.node_name() or platform.node().split(".")[0],
            ),
        )
        runtime.start()
    except RuntimeError_ as e:
        raise _fail(str(e), 3) from e
    except LogosLibraryError as e:
        raise _fail(f"could not load liblogosdelivery, see the README: {e}", 4) from e
    except LogosError as e:
        raise _fail(f"could not start the node: {e}", 4) from e
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if not transport.wait_connected(connect_timeout):
        typer.echo("warning: not connected to the network yet, still trying", err=True)
    typer.echo(f"CI node online: {runtime.node.node_id}; Ctrl-C to stop")
    stopped.wait()
    runtime.stop()
    typer.echo("CI node stopped")
    leave()


def _mint_token(request_url: str, request_token: str, audience: str) -> str:
    import httpx

    resp = httpx.get(
        f"{request_url}&audience={quote(audience, safe='')}",
        headers={"Authorization": f"bearer {request_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return str(resp.json()["value"])


@ci_app.command("submit")
def ci_submit(
    oses: list[str] = OsOpt,
    team_key: str = TeamKeyOpt,
    entry_node: list[str] = EntryNodeOpt,
    wait: float = WaitOpt,
    busy_wait: float = BusyWaitOpt,
    follow: bool = FollowOpt,
    silent_timeout: float = SilentOpt,
    max_run: float = MaxRunOpt,
    hosted: list[str] = HostedOpt,
) -> None:
    """Ask the fleet to run the legs of this workflow run; print what falls back to GitHub.

    Run by the hosted `submit` job of the reusable workflow. It writes `handled`, `unhandled`
    and `failed` (JSON lists of OS names) to $GITHUB_OUTPUT. It exits 0 even when nobody answers:
    that is what the fallback is for. With --follow it stays until every leg taken ends; a leg
    whose machine went silent joins `unhandled` (it runs on GitHub), and a leg that failed is in
    `failed` (the workflow fails the job on it).
    """
    from .ci.messages import leg_audience, make_leg_id
    from .ci.submit import Leg, Submitter
    from .ci.summary import render as render_summary
    from .ci.summary import render_text
    from .crypto import decode_signing_public, encode_public_key, generate_private_key
    from .team import TeamState
    from .transport.config import DEFAULT_LOG_LEVEL, env_log_level, runner_config
    from .transport.logos import LogosTransport

    env = os.environ
    needed = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_REPOSITORY")
    missing = [n for n in needed if not env.get(n)]
    if missing or not team_key:
        raise _fail(
            "missing: " + ", ".join([*missing, *([] if team_key else ["--team-key"])]) +
            " (the job needs permissions: id-token: write)",
            2,
        )
    repo = env["GITHUB_REPOSITORY"]
    run_id, attempt = env.get("GITHUB_RUN_ID", "0"), env.get("GITHUB_RUN_ATTEMPT", "1")
    legs = []
    for os_name in oses:
        reply = generate_private_key()
        aud = leg_audience(encode_public_key(reply.public_key()), os_name)
        try:
            token = _mint_token(
                env["ACTIONS_ID_TOKEN_REQUEST_URL"], env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], aud
            )
        except Exception as e:  # noqa: BLE001 - any failure means: fall back to GitHub
            typer.echo(f"warning: could not get an OIDC token: {type(e).__name__}", err=True)
            _fall_back_all(oses, "could not get an OIDC token (does the job have id-token: write?)")
            return
        legs.append(
            Leg(os_name, make_leg_id(repo, run_id, attempt, os_name), token, reply, repo)
        )
    try:
        level = env_log_level()
    except ValueError as e:  # a bad setting must not send every leg to GitHub
        typer.echo(f"warning: {e}; using {DEFAULT_LOG_LEVEL}", err=True)
        level = DEFAULT_LOG_LEVEL
    try:
        transport = LogosTransport(
            {**runner_config(entry_nodes=entry_node, log_level=level), "mode": "Edge"}
        )
        transport.start()
        submitter = Submitter(
            transport,
            TeamState(decode_signing_public(team_key)),
            wait_s=wait,
            busy_wait_s=busy_wait,
        )
        result = submitter.submit(legs)
        if follow and result.handled:
            from .ci.summary import machine

            sha = env.get("GITHUB_SHA", "")[:7]
            typer.echo(f"Octopus CI: {repo} @ {sha or '?'}, run {run_id}")
            for os_name in oses:
                if os_name in result.handled:
                    typer.echo(f"  {os_name}: taken by {machine(result, os_name)}")
                else:
                    typer.echo(
                        f"  {os_name}: no team machine, runs on GitHub: "
                        f"{result.reasons.get(os_name, 'not taken')}"
                    )
            typer.echo("The log of each leg follows, as [leg machine] lines.")
            submitter.follow(
                result,
                lambda o, line: typer.echo(f"[{o} {_short(machine(result, o))}] {line}"),
                timeout_s=max_run,
                silent_s=silent_timeout,
            )
    except Exception as e:  # noqa: BLE001 - the fleet is down or unreachable: fall back
        typer.echo(f"warning: could not reach the fleet: {type(e).__name__}", err=True)
        _fall_back_all(oses, f"could not reach the fleet ({type(e).__name__})")
        _exit_now()
        return
    failed = _settle(result)
    if not follow:
        from .ci.summary import machine

        for os_name in oses:
            if os_name in result.handled:
                typer.echo(f"{os_name}: taken by {machine(result, os_name)}")
    typer.echo(render_text(list(oses), result))
    for os_name, outcome in result.outcomes.items():
        if outcome.get("state") != "cancelled" and outcome.get("detail"):
            typer.echo(f"  {os_name}: {_describe(outcome)}")
    _write_summary(render_summary(list(oses), result, context=_context(), hosted=hosted))
    _write_outputs(sorted(result.handled), result.unhandled, failed)
    _exit_now()


FAILED_STATES = ("failure", "error", "timeout")


def _settle(result) -> list[str]:
    """Sort the legs the nodes reported: a lost machine's leg falls back; a failed one fails.

    A `cancelled` leg (its run was superseded) is neither: GitHub already stopped wanting it.
    """
    failed = []
    for os_name, outcome in result.outcomes.items():
        state = outcome.get("state")
        if state == "lost":
            result.handled.pop(os_name, None)
            result.unhandled.append(os_name)
            result.reasons[os_name] = str(outcome.get("detail", "the machine was lost"))
        elif state in FAILED_STATES:
            failed.append(os_name)
    return sorted(failed)


def _context() -> dict[str, str]:
    """Repo, commit and run of the Actions environment, for the title of the summary."""
    e = os.environ
    return {
        "repo": e.get("GITHUB_REPOSITORY", ""),
        "sha": e.get("GITHUB_SHA", ""),
        "run": e.get("GITHUB_RUN_ID", ""),
        "attempt": e.get("GITHUB_RUN_ATTEMPT", ""),
        "server": e.get("GITHUB_SERVER_URL", "https://github.com"),
    }


def _short(text: str) -> str:
    """A machine for the prefix of a log line: the name, without the login."""
    return text.split(" (", 1)[0]


def _describe(outcome: dict) -> str:
    state = str(outcome.get("state", "?"))
    detail = str(outcome.get("detail", ""))
    exit_code = outcome.get("exit")
    extra = f" (exit code: {exit_code})" if state in ("failure",) and exit_code not in (None, -1) else ""
    return f"{state}{extra}" + (f": {detail}" if detail else "")


def _exit_now() -> None:
    """Leave without stopping the node: the phase 0 spike measured `stop()` at ~23 s and the
    native library crashed at interpreter exit in Edge mode (exit code 139). The outputs are
    already written, and the job has nothing left to clean up."""
    from .process import exit_now

    exit_now(0)


def _fall_back_all(oses: list[str], reason: str) -> None:
    from .ci.submit import SubmitResult
    from .ci.summary import render as render_summary

    result = SubmitResult(unhandled=list(oses), reasons={o: reason for o in oses})
    _write_summary(render_summary(list(oses), result, context=_context()))
    _write_outputs([], list(oses), [])


def _write_summary(markdown: str) -> None:
    """Append to the job summary of the workflow run, when running in GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        # GitHub reads the summary as UTF-8; Windows would write it as cp1252 and fail on the icons
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(markdown + "\n")


def _write_outputs(handled: list[str], unhandled: list[str], failed: list[str] | None = None) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"handled={json.dumps(handled)}",
        f"unhandled={json.dumps(unhandled)}",
        f"failed={json.dumps(failed or [])}",
    ]
    if out:
        with open(out, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    else:
        typer.echo("\n".join(lines))


NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,39}")


@ci_app.command("name")
def ci_name(
    name: str | None = typer.Argument(None, help="The name to show; none: print the current one."),
    reset: bool = typer.Option(False, "--reset", help="Go back to the host name."),
    data_dir: Path | None = DataDirOpt,
) -> None:
    """The name of this machine in the logs and the summary of Octopus CI runs.

    Default is the host name. Takes effect when the node restarts.
    """
    import platform

    home = _home(data_dir)
    if reset:
        home.save_node_name("")
        typer.echo("name: back to the host name, after the node restarts")
        return
    if name is None:
        typer.echo(home.node_name() or platform.node().split(".")[0])
        return
    if not NAME_PATTERN.fullmatch(name.strip()):
        raise _fail("a name is 1 to 40 letters, digits, spaces and . _ ( ) + -", 2)
    home.save_node_name(name.strip())
    typer.echo(f"name: {name.strip()}, after the node restarts (the service: restart it)")


@ci_app.command("status")
def ci_status(data_dir: Path | None = DataDirOpt) -> None:
    """Show what this machine knows: its proof, the team key and the member lists."""
    home = _home(data_dir)
    proof = home.proof()
    typer.echo(f"proof       {proof.login if proof else 'none: run octopus ci init'}")
    typer.echo(f"team key    {home.team_pub() or 'none'}")
    allow = home.allow()
    typer.echo(f"allowed     {', '.join([*allow.orgs, *allow.repos]) or 'nothing'}")
    for ml in home.lists():
        left = (ml.not_after - time.time()) / 3600
        typer.echo(f"members     {ml.scope}: {len(ml.logins)} (v{ml.version}, {left:.0f} h left)")
    typer.echo(f"daily sync  {', '.join(home.scopes()) or 'off'}")
    typer.echo(f"isolation   {_isolation_line(home)}")
    import platform

    typer.echo(
        f"name        {home.node_name() or platform.node().split('.')[0] + ' (host name)'}"
    )


# --- team -----------------------------------------------------------------------------


@team_app.command("init")
def team_init(data_dir: Path | None = DataDirOpt) -> None:
    """Create the team key (admins): encrypted with a passphrase, used only to sign."""
    from .ci.wizard import passphrase
    from .crypto import encode_signing_public, generate_signing_key
    from .team.keyfile import save_team_key

    home = _home(data_dir)
    if home.team_key_path.exists():
        raise _fail(f"a team key exists: {home.team_key_path}", 1)
    key = generate_signing_key()
    save_team_key(key, home.team_key_path, passphrase("Passphrase for the team key", confirm=True))
    pub = encode_signing_public(key.public_key())
    home.save_team_pub(pub)
    typer.echo(f"team public key: {pub}")
    typer.echo("commit it as .octopus/team.pub in the repo so members can pin it")


@team_app.command("sync")
def team_sync(
    org: list[str] = OrgOpt,
    repo: list[str] = RepoOpt,
    login: list[str] = LoginsOpt,
    forget_login: list[str] = ForgetLoginOpt,
    force: bool = ForceOpt,
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Rebuild the signed member lists from GitHub (admins). The node repeats this daily.

    Logins given with --login stay on the list of their scopes in every later sync, also the
    daily one, until --forget-login removes them.
    """
    from .ci.gh import Gh
    from .ci.wizard import passphrase
    from .crypto import generate_signing_key
    from .team import TeamError, issue_delegation
    from .team.keyfile import KeyFileError, load_team_key
    from .team.state import DELEGATION_TTL_S
    from .team.sync import build_list, key_to_text

    home = _home(data_dir)
    scopes = [*(f"org:{o}" for o in org), *(f"repo:{r}" for r in repo)] or home.scopes()
    if not scopes:
        raise _fail("nothing to sync: give --org or --repo", 2)
    try:
        key = load_team_key(home.team_key_path, passphrase("Team key passphrase"))
    except KeyFileError as e:
        raise _fail(str(e), 1) from e
    now = time.time()
    lists = {ml.scope: ml for ml in home.lists()}
    gh = Gh()
    delegate = generate_signing_key()
    extras = home.extras()
    added = {lg.strip().lower() for lg in login if lg.strip()}
    dropped = {lg.strip().lower() for lg in forget_login if lg.strip()}
    for scope in scopes:
        extras[scope] = sorted(({*extras.get(scope, []), *added}) - dropped)
        try:
            ml = build_list(
                gh, scope, key, now, previous=lists.get(scope), extra=extras[scope], force=force
            )
        except TeamError as e:
            raise _fail(str(e), 1) from e
        lists[scope] = ml
        typer.echo(f"{scope}: {len(ml.logins)} members (version {ml.version})")
    every_scope = sorted({*home.scopes(), *scopes})
    try:
        # a delegation names one scope and one delegated key: renew all of them with the new key
        delegations = [
            issue_delegation(key, delegate.public_key(), s, now + DELEGATION_TTL_S).model_dump_json()
            for s in every_scope
        ]
    except TeamError as e:
        raise _fail(str(e), 1) from e
    home.save_delegate(key_to_text(delegate), delegations)
    home.save_lists(list(lists.values()))
    home.save_extras(extras)
    home.save_scopes(every_scope)


@team_app.command("show")
def team_show(data_dir: Path | None = DataDirOpt) -> None:
    """List the members on this machine's lists."""
    home = _home(data_dir)
    lists = home.lists()
    if not lists:
        typer.echo("no member list")
        raise typer.Exit(code=1)
    extras = home.extras()
    for ml in lists:
        typer.echo(f"{ml.scope} (version {ml.version}): {', '.join(ml.logins)}")
        if extras.get(ml.scope):
            typer.echo(f"  added by hand, kept by every sync: {', '.join(extras[ml.scope])}")


sandbox_app = typer.Typer(
    no_args_is_help=True, help="Run jobs in a throwaway container, not as you (Linux hosts)."
)
ci_app.add_typer(sandbox_app, name="sandbox")


@sandbox_app.command("enable")
def sandbox_enable(
    image: str = typer.Option(
        ..., "--image", help="Container image with the toolchain your jobs need, pinned by digest "
        "(e.g. python:3.12-slim@sha256:...). Your choice, never the repo's.",
    ),
    runtime: str | None = typer.Option(
        None, "--runtime", help="podman or docker (default: podman if installed, else docker)."
    ),
    network: str = typer.Option("bridge", "--network", help="Container network; none cuts it off."),
    memory: str = typer.Option("4g", "--memory", help="Memory limit, e.g. 4g."),
    cpus: str = typer.Option("2", "--cpus", help="CPU limit, e.g. 2."),
    pull: bool = typer.Option(True, "--pull/--no-pull", help="Pull the image now."),
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Run every job in a container that sees only its checkout. Restart the node afterwards."""
    from .ci.sandbox import Sandbox, SandboxError
    from .ci.sandbox_setup import SandboxSetupError, enable, find_runtime

    try:
        sandbox = Sandbox(image=image, runtime=runtime or find_runtime() or "docker", network=network, memory=memory, cpus=cpus)
    except SandboxError as e:
        raise _fail(str(e), 2) from e
    try:
        warnings = enable(_home(data_dir), sandbox, pull=pull, say=typer.echo)
    except SandboxSetupError as e:
        raise _fail(str(e), e.code) from e
    typer.echo(f"jobs now run in {sandbox.image} ({sandbox.runtime}, network {sandbox.network})")
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo("restart the node for it to take effect: octopus runner install-service")


@sandbox_app.command("disable")
def sandbox_disable(data_dir: Path | None = DataDirOpt) -> None:
    """Run jobs as you again (no sandbox)."""
    if _home(data_dir).clear_sandbox():
        typer.echo("sandbox off: jobs run as you after the node restarts")
    else:
        typer.echo("sandbox was already off")


@sandbox_app.command("status")
def sandbox_status(data_dir: Path | None = DataDirOpt) -> None:
    """Show how jobs are run on this machine."""
    from .ci.sandbox import SandboxError

    try:
        sandbox = _home(data_dir).sandbox()
    except SandboxError as e:
        raise _fail(str(e), 3) from e
    if sandbox is None:
        typer.echo("no sandbox: jobs run as you (see: octopus ci sandbox enable --image <image>)")
        return
    typer.echo(
        f"sandbox: {sandbox.image} via {sandbox.runtime}, network {sandbox.network}, "
        f"memory {sandbox.memory}, cpus {sandbox.cpus}"
    )


light_app = typer.Typer(
    no_args_is_help=True, help="Run jobs as a dedicated 'octopus' account on this Mac."
)
ci_app.add_typer(light_app, name="light")


@light_app.command("install")
def light_install(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before running the commands."),
    network: str = typer.Option("internet", "--network", help="internet, or none to cut jobs off."),
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Create the account and its folders (asks for the admin password), then turn it on."""
    from .ci import light_wizard
    from .ci.light import Light, LightError

    home = _home(data_dir)
    try:
        Light(network=network)
    except LightError as e:
        raise _fail(str(e), 2) from e
    check = light_wizard.offer_light(home, assume_yes=yes)
    _show(check)
    if check.status == Status.CHANGED and network != "internet":
        home.save_light(Light(network=network))
    if check.failed or check.status == Status.SKIPPED:
        raise typer.Exit(code=3)
    typer.echo("restart the node for it to take effect: octopus runner install-service")


@light_app.command("status")
def light_status(data_dir: Path | None = DataDirOpt) -> None:
    """Show whether jobs run as the dedicated account, and whether that works."""
    from .ci.light import LightError

    try:
        light = _home(data_dir).light()
    except LightError as e:
        raise _fail(str(e), 3) from e
    if light is None:
        typer.echo("light level off: see: octopus ci light install")
        return
    why = light.supported()
    typer.echo(f"{light.describe()}; work root {light.work_root}")
    if why:
        typer.echo(f"NOT USABLE: {why}", err=True)
        raise typer.Exit(code=3)
    typer.echo("usable: the account exists and sudo works")


@light_app.command("disable")
def light_disable(data_dir: Path | None = DataDirOpt) -> None:
    """Stop using the account (jobs run as you again). The account stays; see: ci uninstall."""
    if _home(data_dir).clear_light():
        typer.echo("light level off: jobs run as you after the node restarts")
    else:
        typer.echo("light level was already off")


@ci_app.command("uninstall")
def ci_uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before running the commands."),
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Remove the 'octopus' account, its home, the sudo rule and the work root."""
    from .ci import light_account
    from .ci.light import ACCOUNT, WORK_ROOT, LightError

    home = _home(data_dir)
    try:
        light = home.light()
        steps = light_account.uninstall_steps(
            f"/Users/{light.account if light else ACCOUNT}",
            account=light.account if light else ACCOUNT,
            work_root=light.work_root if light else WORK_ROOT,
        )
    except LightError as e:
        raise _fail(str(e), 3) from e
    typer.echo("These commands will run (admin password needed):")
    for step in steps:
        typer.echo(f"  {step.command()}")
    if not yes and not typer.confirm("Remove the account and its folders?", default=False):
        raise typer.Exit(code=1)
    home.clear_light()
    try:
        light_account.run_steps(steps, light_account.execute_step, tolerate_failure=True)
    except light_account.LightSetupError as e:
        raise _fail(str(e), 4) from e
    typer.echo("removed. jobs run as you again after the node restarts")


def add_ci(app: typer.Typer) -> None:
    app.add_typer(ci_app, name="ci")
    app.add_typer(team_app, name="team")


__all__ = ["Status", "add_ci", "ci_app", "team_app"]
