"""`octopus ci init owner/repo`: join the team of a repo, as a member or as its admin.

The wizard finds out by itself which one the user is: it asks `gh` whether the login has admin
rights on the target and says so. A member needs a team key to pin (published by an admin in
`.octopus/team.pub` of the repo) and proves a GitHub login with an SSH key. An admin can create
the team key and add the members of the organization, which also turns on the daily sync.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable
from pathlib import Path

import typer

from ..agent import manual, service
from ..crypto import encode_public_key, encode_signing_public, generate_signing_key
from ..github.setup import Check, Status
from ..team import (
    NODE_PROOF_NAMESPACE,
    TeamError,
    issue_delegation,
    make_node_proof,
)
from ..team.keyfile import PASSPHRASE_ENV, KeyFileError, load_team_key, save_team_key
from ..team.sshsig import SshSigError, parse_authorized_keys, ssh_keygen_sign
from ..team.state import DELEGATION_TTL_S
from ..team.sync import build_list, key_to_text
from . import light_wizard
from .gh import Gh
from .home import Allow, Home
from .light import Light
from .sandbox import Sandbox, SandboxError
from .sandbox_setup import SandboxSetupError, enable, find_runtime

TEAM_FILE = ".octopus/team.pub"
DEFAULT_SSH_KEY = Path("~/.ssh/id_ed25519")
Install = Callable[[service.ServiceSpec], service.ServiceResult]


def os_name() -> str:
    system = platform.system().lower()
    return {"darwin": "macos", "linux": "linux"}.get(system, system)


def passphrase(prompt: str, *, confirm: bool = False) -> str:
    given = os.environ.get(PASSPHRASE_ENV, "")
    if given:
        return given
    return str(typer.prompt(prompt, hide_input=True, confirmation_prompt=confirm))


def init_ci(
    target_text: str,
    home: Home,
    gh: Gh,
    *,
    now: float,
    ssh_key: Path | None = None,
    login: str | None = None,
    data_dir: Path,
    install: Install | None = None,
    show: Callable[[Check], Check] = lambda c: c,
    start_service: bool = True,
    assume_yes: bool = False,
) -> list[Check]:
    checks: list[Check] = []

    def add(check: Check) -> Check:
        checks.append(show(check))
        return check

    try:
        target = manual.parse_target(target_text)
    except manual.ManualRunnerError as e:
        add(Check("target", Status.FAIL, str(e)))
        return checks
    name = target.name
    add(Check("target", Status.OK, name))
    if os_name() not in ("linux", "macos"):
        add(Check("platform", Status.FAIL, f"only Linux and macOS nodes: {os_name()}"))
        return checks
    if add(_gh_login_check(gh)).failed:
        return checks
    home.identity()
    admin = _is_admin(gh, target)
    if admin:
        add(Check("role", Status.OK, f"ADMIN of {name}: you can create and sync the team"))
    elif admin is False:
        add(Check("role", Status.OK, f"member of {name}"))
    else:
        add(Check("role", Status.WARN, "could not tell if you are an admin: continuing as a member"))
    if add(_team_key(home, gh, target, bool(admin))).failed:
        return checks
    if admin and home.team_key_path.exists():
        add(_sync_team(home, gh, target, now))
    add(_prove(home, gh, now, ssh_key, login))
    if checks[-1].failed:
        return checks
    if sys.platform == "darwin":
        if add(light_wizard.offer_light(home, assume_yes=assume_yes)).failed:
            return checks
    else:
        if add(_sandbox(home, assume_yes)).failed:
            return checks
        if (
            sys.platform.startswith("linux")
            and home.sandbox() is None
            and add(light_wizard.offer_light(home, assume_yes=assume_yes)).failed
        ):
            return checks
    if add(_job_access_notice(name, assume_yes, home.sandbox(), home.light())).failed:
        return checks
    allow = home.allow()
    entry = target.name
    if target.repo and entry not in allow.repos:
        allow.repos.append(entry)
    elif not target.repo and target.owner not in allow.orgs:
        allow.orgs.append(target.owner)
    home.save_allow(Allow(allow.repos, allow.orgs))
    add(Check("allowlist", Status.OK, ", ".join([*allow.orgs, *allow.repos])))
    if start_service:
        add(_install(install or service.install, data_dir))
    return checks


def _gh_login_check(gh: Gh) -> Check:
    if gh.login():
        return Check("gh login", Status.OK, "logged in")
    return Check(
        "gh login",
        Status.FAIL,
        "gh is not logged in: run gh auth login, then run this again",
    )


def _is_admin(gh: Gh, target: manual.Target) -> bool | None:
    if target.repo:
        return gh.is_admin(target.name)
    return gh.is_org_admin(target.owner)


def _team_key(home: Home, gh: Gh, target: manual.Target, admin: bool) -> Check:
    if home.team_pub():
        return Check("team key", Status.OK, "pinned")
    if target.repo:
        text = gh.repo_file(target.name, TEAM_FILE)
        if text and text.strip():
            home.save_team_pub(text)
            return Check("team key", Status.CHANGED, f"pinned from {TEAM_FILE} in {target.name}")
    if not admin:
        return Check(
            "team key",
            Status.FAIL,
            f"no {TEAM_FILE} in {target.name}: ask an admin to run: octopus ci init {target.name}",
        )
    if not typer.confirm(f"No team yet for {target.name}. Create the team key now?", default=True):
        return Check("team key", Status.FAIL, "no team key")
    key = generate_signing_key()
    save_team_key(
        key, home.team_key_path, passphrase("Passphrase for the team key", confirm=True)
    )
    pub = encode_signing_public(key.public_key())
    home.save_team_pub(pub)
    typer.echo(
        f"Commit this public key to the repo so members can pin it:\n"
        f"  mkdir -p .octopus && echo '{pub}' > {TEAM_FILE} && git add {TEAM_FILE}"
    )
    return Check("team key", Status.CHANGED, f"created: {home.team_key_path}")


def _scope(target: manual.Target, gh: Gh) -> tuple[str, str]:
    """(scope, human text) of who joins the team: the org of the target, or its collaborators."""
    is_org = gh.owner_is_org(target.owner)
    if is_org:
        return f"org:{target.owner}", f'the members of the "{target.owner}" organization'
    return f"repo:{target.name}", f"the collaborators with write access to {target.name}"


def _sync_team(home: Home, gh: Gh, target: manual.Target, now: float) -> Check:
    scope, text = _scope(target, gh)
    if scope in home.scopes():
        return Check("team", Status.OK, f"{scope} is synced daily")
    if not typer.confirm(f"Add {text} to the team?", default=True):
        return Check("team", Status.SKIPPED, "nobody added")
    try:
        key = load_team_key(home.team_key_path, passphrase("Team key passphrase"))
        ml = build_list(gh, scope, key, now, extra=home.extras().get(scope))
        delegate = generate_signing_key()
        every_scope = sorted({*home.scopes(), scope})
        # a delegation names one scope and one delegated key: renew all of them with the new key
        delegations = [
            issue_delegation(key, delegate.public_key(), s, now + DELEGATION_TTL_S).model_dump_json()
            for s in every_scope
        ]
    except (KeyFileError, TeamError) as e:
        return Check("team", Status.FAIL, str(e))
    home.save_lists([*[x for x in home.lists() if x.scope != scope], ml])
    home.save_scopes(every_scope)
    home.save_delegate(key_to_text(delegate), delegations)
    return Check("team", Status.CHANGED, f"{len(ml.logins)} members from {scope}; synced daily")


def _prove(home: Home, gh: Gh, now: float, ssh_key: Path | None, login: str | None) -> Check:
    who = login or gh.login()
    if not who:
        return Check("membership proof", Status.FAIL, "no GitHub login: pass --login")
    key = (ssh_key or DEFAULT_SSH_KEY).expanduser()
    pub = Path(f"{key}.pub")
    try:
        keys = parse_authorized_keys(pub.read_text())
    except OSError:
        keys = []
    if not keys:
        return Check(
            "membership proof",
            Status.FAIL,
            f"no Ed25519 SSH key at {key}: create and add one to GitHub: ssh-keygen -t ed25519",
        )
    enc, sign = home.identity()
    try:
        proof = make_node_proof(
            who,
            encode_public_key(enc.public_key()),
            encode_signing_public(sign.public_key()),
            now,
            lambda msg: ssh_keygen_sign(msg, key, NODE_PROOF_NAMESPACE),
        )
    except (SshSigError, TeamError) as e:
        return Check("membership proof", Status.FAIL, str(e))
    home.save_proof(proof)
    return Check("membership proof", Status.CHANGED, f"{who}, valid for 30 days")


JOB_ACCESS_NOTICE = (
    "A CI job runs on this machine as you, in a fresh directory but not in a sandbox.\n"
    "A job from any branch of {name} can read your GitHub login (the gh token or the keychain),\n"
    "your SSH keys and agent, and everything else your user can read. Only serve repos whose\n"
    "writers you would trust with your account. On Linux, run jobs in a container instead:\n"
    "octopus ci sandbox enable --image <image>. Or run them as a separate, sandboxed account:\n"
    "octopus ci light install."
)


SANDBOX_NOTICE = (
    "Jobs of {name} run in a container ({image}) that sees only the checkout: not your GitHub\n"
    "login, SSH keys or home directory. With network {network} they still reach the internet; see\n"
    "docs/decentralized-ci.md for what else the container network gives them."
)


def _sandbox(home: Home, assume_yes: bool) -> Check:
    """Offer to run the jobs in a container (Linux, with Podman or Docker installed)."""
    try:
        current = home.sandbox()
    except SandboxError as e:
        return Check("sandbox", Status.FAIL, f"{e}: run: octopus ci sandbox disable")
    if current is not None:
        return Check("sandbox", Status.OK, f"jobs run in {current.image} ({current.runtime})")
    why = Sandbox.supported()
    if why is not None:
        return Check("sandbox", Status.SKIPPED, why)
    runtime = find_runtime()
    if runtime is None:
        return Check(
            "sandbox",
            Status.SKIPPED,
            "no podman or docker: install rootless Podman, then: octopus ci sandbox enable",
        )
    if assume_yes:
        return Check("sandbox", Status.SKIPPED, "not asked with --yes: octopus ci sandbox enable")
    if not typer.confirm(
        f"Run jobs in a {runtime} container, so they cannot read your GitHub login or SSH keys?",
        default=True,
    ):
        return Check("sandbox", Status.SKIPPED, "jobs run as you")
    image = typer.prompt(
        "Container image with the toolchain your jobs need, pinned by digest "
        "(e.g. python:3.12-slim@sha256:...)"
    )
    try:
        warnings = enable(home, Sandbox(image=image.strip(), runtime=runtime), say=typer.echo)
    except (SandboxError, SandboxSetupError) as e:
        return Check("sandbox", Status.FAIL, str(e))
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    return Check("sandbox", Status.CHANGED, f"jobs run in {image.strip()} ({runtime})")


def _job_access_notice(
    name: str, assume_yes: bool, sandbox: Sandbox | None = None, light: Light | None = None
) -> Check:
    """Say what a job can reach, and make the owner agree before the repo is allowed."""
    who = "as you"
    if sandbox is not None:
        typer.echo(SANDBOX_NOTICE.format(name=name, image=sandbox.image, network=sandbox.network))
    elif light is not None:
        typer.echo(
            light_wizard.LIGHT_NOTICE.format(
                name=name, account=light.account, network=light.network
            )
        )
        who = f"as {light.account}"
    else:
        typer.echo(JOB_ACCESS_NOTICE.format(name=name))
    if assume_yes:
        return Check("job access", Status.OK, "acknowledged with --yes")
    if not typer.confirm(f"Allow jobs of {name} to run on this machine, {who}?", default=False):
        return Check("job access", Status.FAIL, "not allowed: nothing was changed from here")
    return Check("job access", Status.OK, "acknowledged")


def _install(install: Install, data_dir: Path) -> Check:
    try:
        result = install(service.ci_spec(data_dir))
    except service.ServiceError as e:
        return Check("service", Status.FAIL, f"{e}; run it in the foreground: octopus ci node")
    for note in result.notes:
        typer.echo(f"note: {note}")
    return Check("service", Status.CHANGED, f"installed and started: {result.path}")
