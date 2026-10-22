"""`octopus ci` (nodes and the submit side of Octopus CI) and `octopus team` (membership)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import typer

from .github.setup import Check, Status

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
WaitOpt = typer.Option(45.0, "--wait", help="Seconds to wait for each leg to be taken.")
FollowOpt = typer.Option(False, "--follow", help="Print the logs of the legs taken.")
OrgOpt = typer.Option([], "--org", help="Add the members of this organization.")
RepoOpt = typer.Option([], "--repo", help="Add this repo's collaborators with write access.")
LoginsOpt = typer.Option([], "--login", help="Add this GitHub login.")
ForceOpt = typer.Option(False, "--force", help="Accept a list that shrinks sharply.")


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _home(data_dir: Path | None):
    from .ci.home import ci_home

    return ci_home(data_dir)


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
    """Join the team as a member, or as its admin: Octopus finds out which you are."""
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
    transport = LogosTransport(settings.node_config(home.node_dir))
    import platform

    try:
        runtime = CiRuntime(
            home,
            transport,
            Settings(
                os_name=os_name(),
                arch=platform.machine().lower(),
                name=platform.node().split(".")[0],
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
    follow: bool = FollowOpt,
) -> None:
    """Ask the fleet to run the legs of this workflow run; print what falls back to GitHub.

    Run by the hosted `submit` job of the reusable workflow. It writes `handled` and `unhandled`
    (JSON lists of OS names) to $GITHUB_OUTPUT. It exits 0 even when nobody answers: that is what
    the fallback is for.
    """
    from .ci.messages import leg_audience, make_leg_id
    from .ci.submit import Leg, Submitter
    from .ci.summary import render as render_summary
    from .crypto import decode_signing_public, encode_public_key, generate_private_key
    from .team import TeamState
    from .transport.config import runner_config
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
        transport = LogosTransport(
            {**runner_config(entry_nodes=entry_node), "mode": "Edge"}
        )
        transport.start()
        submitter = Submitter(transport, TeamState(decode_signing_public(team_key)), wait_s=wait)
        result = submitter.submit(legs)
        if follow and result.handled:
            submitter.follow(
                result, lambda o, line: typer.echo(f"[{o}] {line}"), timeout_s=6 * 3600
            )
    except Exception as e:  # noqa: BLE001 - the fleet is down or unreachable: fall back
        typer.echo(f"warning: could not reach the fleet: {type(e).__name__}", err=True)
        _fall_back_all(oses, f"could not reach the fleet ({type(e).__name__})")
        _exit_now()
        return
    for os_name, node in result.handled.items():
        typer.echo(f"{os_name}: taken by node {node}")
    for os_name in result.unhandled:
        typer.echo(f"{os_name}: no node took it, falls back to GitHub")
    _write_summary(render_summary(list(oses), result))
    _write_outputs(sorted(result.handled), result.unhandled)
    _exit_now()


def _exit_now() -> None:
    """Leave without stopping the node: the phase 0 spike measured `stop()` at ~23 s and the
    native library crashed at interpreter exit in Edge mode (exit code 139). The outputs are
    already written, and the job has nothing left to clean up."""
    import sys

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _fall_back_all(oses: list[str], reason: str) -> None:
    from .ci.submit import SubmitResult
    from .ci.summary import render as render_summary

    result = SubmitResult(unhandled=list(oses), reasons={o: reason for o in oses})
    _write_summary(render_summary(list(oses), result))
    _write_outputs([], list(oses))


def _write_summary(markdown: str) -> None:
    """Append to the job summary of the workflow run, when running in GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


def _write_outputs(handled: list[str], unhandled: list[str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [f"handled={json.dumps(handled)}", f"unhandled={json.dumps(unhandled)}"]
    if out:
        with open(out, "a") as f:
            f.write("\n".join(lines) + "\n")
    else:
        typer.echo("\n".join(lines))


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
    force: bool = ForceOpt,
    data_dir: Path | None = DataDirOpt,
) -> None:
    """Rebuild the signed member lists from GitHub (admins). The node repeats this daily."""
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
    for scope in scopes:
        try:
            ml = build_list(
                gh, scope, key, now, previous=lists.get(scope), extra=login, force=force
            )
            delegation = issue_delegation(key, delegate.public_key(), scope, now + DELEGATION_TTL_S)
        except TeamError as e:
            raise _fail(str(e), 1) from e
        lists[scope] = ml
        typer.echo(f"{scope}: {len(ml.logins)} members (version {ml.version})")
        home.save_delegate(key_to_text(delegate), delegation.model_dump_json())
    home.save_lists(list(lists.values()))
    home.save_scopes(sorted({*home.scopes(), *scopes}))


@team_app.command("show")
def team_show(data_dir: Path | None = DataDirOpt) -> None:
    """List the members on this machine's lists."""
    home = _home(data_dir)
    lists = home.lists()
    if not lists:
        typer.echo("no member list")
        raise typer.Exit(code=1)
    for ml in lists:
        typer.echo(f"{ml.scope} (version {ml.version}): {', '.join(ml.logins)}")


def add_ci(app: typer.Typer) -> None:
    app.add_typer(ci_app, name="ci")
    app.add_typer(team_app, name="team")


__all__ = ["Status", "add_ci", "ci_app", "team_app"]
