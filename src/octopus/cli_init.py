"""`octopus init`: a guided setup for a runner host or a coordinator host (#32).

Each step shows what is set now and asks only for what is missing or what the user wants
to change. A step that has nothing to do reports `ok`. So the wizard is safe to run again,
and the user can stop it at any prompt and continue later.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from .github.setup import Check, Status

ROLES = ("runner", "coordinator", "manual")


def _show(check: Check) -> Check:
    typer.echo(f"[{check.status.value:>7}] {check.name}  {check.detail}")
    return check


def _client(token, api_url: str):
    """The API client; replaced in tests."""
    from .github.token import TokenClient

    return TokenClient(token, api_url=api_url)


def _ask(text: str, default: str = "") -> str:
    """A prompt where an empty answer is allowed (it means: skip)."""
    return str(typer.prompt(text, default=default, show_default=bool(default))).strip()


# --- runner ---------------------------------------------------------------------------


def _runner_trust(settings) -> Check:
    from .agent.config import ConfigError

    have = len(settings.coordinators) + (1 if settings.root_key else 0)
    if have and not typer.confirm("Add another coordinator or root key?", default=False):
        return Check("trust", Status.OK, f"{_trust_text(settings)}")
    typer.echo(
        "A runner takes jobs only from coordinators it trusts. Give the public key that\n"
        "`octopus coordinator start` prints, or the root key of your team (it lets the\n"
        "runner find the coordinators that the root key authorises)."
    )
    while True:
        kind = _ask("Key type: coordinator or root (empty: skip)").lower()
        if kind not in ("coordinator", "root"):
            if kind:
                typer.echo("answer coordinator or root")
                continue
            status = Status.OK if have else Status.WARN
            return Check("trust", status, _trust_text(settings) or "no coordinator trusted yet")
        key = _ask(f"{kind.capitalize()} public key (empty: skip)")
        if not key:
            continue
        before = (list(settings.coordinators), settings.root_key)
        if kind == "root":
            settings.root_key = key
        elif key not in settings.coordinators:
            settings.coordinators.append(key)
        try:
            settings.validate()
        except ConfigError as e:
            settings.coordinators, settings.root_key = before
            typer.echo(f"error: {e}")
            continue
        return Check("trust", Status.CHANGED, _trust_text(settings))


def _trust_text(settings) -> str:
    parts = []
    if settings.coordinators:
        parts.append(f"{len(settings.coordinators)} coordinator key(s)")
    if settings.root_key:
        parts.append("a root key")
    return " and ".join(parts)


def _runner_entry_nodes(settings) -> Check:
    if settings.entry_nodes:
        return Check("entry nodes", Status.OK, ", ".join(settings.entry_nodes))
    node = _ask("Entry node multiaddr of a coordinator (empty: only the default fleet)")
    if not node:
        return Check("entry nodes", Status.SKIPPED, "the default fleet only")
    settings.entry_nodes.append(node)
    return Check("entry nodes", Status.CHANGED, node)


def _runner_allowlist(allowlist) -> Check:
    from .agent.trust import InvalidTarget

    added = []
    typer.echo("This runner runs jobs only from the repos and orgs that you allow.")
    for entry in sorted(allowlist.orgs) + sorted(allowlist.repos):
        typer.echo(f"  allowed: {entry}")
    while True:
        target = _ask("Repo (org/name) or org to allow (empty: done)")
        if not target:
            break
        try:
            if allowlist.add(target):
                added.append(target)
        except InvalidTarget as e:
            typer.echo(f"error: {e}")
    total = len(allowlist.orgs) + len(allowlist.repos)
    if added:
        return Check("allowlist", Status.CHANGED, f"added {', '.join(added)} ({total} in all)")
    if total:
        return Check("allowlist", Status.OK, f"{total} allowed")
    return Check("allowlist", Status.WARN, "nothing allowed: the runner rejects every job")


def _runner_max_jobs(settings) -> Check:
    from .agent.config import ConfigError, format_max_jobs, parse_max_jobs

    current = format_max_jobs(settings.max_jobs)
    answer = typer.prompt("Jobs to run in parallel (a number, or unlimited)", default=current)
    try:
        value = parse_max_jobs(answer)
    except ConfigError as e:
        return Check("max jobs", Status.WARN, f"kept {current}: {e}")
    if value == settings.max_jobs:
        return Check("max jobs", Status.OK, format_max_jobs(value))
    settings.max_jobs = value
    return Check("max jobs", Status.CHANGED, format_max_jobs(value))


def _runner_service(config: Path | None) -> Check:
    from .agent import service

    try:
        installed, description = service.status()
    except service.ServiceError as e:
        return Check("service", Status.WARN, f"could not query it: {e}")
    if installed:
        return Check("service", Status.OK, description)
    if not typer.confirm("Start the runner automatically at login?", default=True):
        return Check("service", Status.SKIPPED, "start it with: octopus runner start")
    try:
        result = service.install(service.runner_spec(config))
    except service.ServiceError as e:
        return Check("service", Status.FAIL, f"could not install it: {e}")
    for note in result.notes:
        typer.echo(f"note: {note}")
    return Check("service", Status.CHANGED, f"installed: {result.path}")


def init_runner(config: Path | None) -> list[Check]:
    from .agent.config import ConfigError, RunnerConfig, settings_path
    from .agent.trust import Allowlist, default_config_path

    allow_path = config or default_config_path()
    path = settings_path(allow_path)
    try:
        settings = RunnerConfig.load(path)
        allowlist = Allowlist.load(allow_path)
    except (ConfigError, ValueError) as e:
        return [_show(Check("settings", Status.FAIL, str(e)))]
    typer.echo(f"settings: {path}")
    checks = [
        _show(_runner_trust(settings)),
        _show(_runner_entry_nodes(settings)),
        _show(_runner_max_jobs(settings)),
    ]
    try:
        settings.save(path)
    except ConfigError as e:
        return [*checks, _show(Check("settings", Status.FAIL, str(e)))]
    checks.append(_show(_runner_allowlist(allowlist)))
    allowlist.save(allow_path)
    checks.append(_show(_runner_service(config)))
    ready = settings.coordinators or settings.root_key
    if ready and (allowlist.orgs or allowlist.repos):
        typer.echo("next: octopus runner start (or log in again if the service is installed)")
        typer.echo("      octopus runner status shows whether it is online")
    else:
        typer.echo("next: run octopus init again to finish the steps marked warn")
    return checks


# --- manual runner --------------------------------------------------------------------


def _manual_target():
    from .agent import manual

    while True:
        text = _ask("Repo (owner/name) or org that this runner serves")
        if not text:
            return None
        try:
            return manual.parse_target(text)
        except manual.ManualRunnerError as e:
            typer.echo(f"error: {e}")


def _parse_target(text: str):
    from .agent import manual

    try:
        return manual.parse_target(text)
    except manual.ManualRunnerError as e:
        typer.echo(f"error: {e}")
        return None


def _manual_register(base: Path, target, guided: bool = True) -> tuple[Check, Path | None]:
    from .agent import manual
    from .cli_manual import ask_token

    try:
        directory = manual.prepare(base, target)
        if manual.is_configured(directory):
            return Check("registration", Status.OK, f"already registered: {target.url}"), directory
        raw = _ask("Extra labels, comma separated (empty: only octopus)") if guided else ""
        name = (
            _ask("Runner name in GitHub", manual.default_name())
            if guided
            else manual.default_name()
        )
        unique = manual.unique_label(name)
        extra = [x.strip() for x in raw.split(",") if x.strip()]
        labels = [*manual.DEFAULT_LABELS, unique, *extra]
        token = ask_token("registration", target.new_runner_page, manual.TOKEN_ENV, target=target)
        manual.configure(directory, target, token, name=name, labels=labels)
        manual.save_label(directory, unique)
    except manual.ManualRunnerError as e:
        return Check("registration", Status.FAIL, str(e)), None
    detail = f"{name} for {target.url} (labels: self-hosted, {', '.join(labels)})"
    return Check("registration", Status.CHANGED, detail), directory


def init_manual_runner(
    data_dir: Path | None, target_text: str | None = None, foreground: bool = False
) -> list[Check]:
    """Register this machine as a plain GitHub runner with a one-hour token, then run it."""
    from .agent import manual
    from .cli_manual import data_dir_or_default, run_manual

    base = data_dir_or_default(data_dir)
    typer.echo(
        "A manual runner is a plain GitHub runner: no coordinator, no scheduling and no Octopus\n"
        "trust policy. Any workflow in the target that matches its labels runs on this machine.\n"
        "See docs/manual-runner.md."
    )
    guided = target_text is None
    target = _manual_target() if guided else _parse_target(target_text)
    if target is None:
        if not guided:
            return [_show(Check("target", Status.FAIL, f"could not use: {target_text}"))]
        return [_show(Check("target", Status.SKIPPED, "nothing to register"))]
    checks = [_show(Check("target", Status.OK, target.url))]
    check, directory = _manual_register(base, target, guided)
    checks.append(_show(check))
    if directory is None:
        return checks
    label = manual.saved_label(directory)
    announce = label is not None and _manual_announce_setup(guided)
    if label is not None:
        typer.echo(
            f"Use it in a workflow with: runs-on: [self-hosted, {label}]\n"
            "  (or let the pick job choose it: docs/oidc-pick.md)"
        )
    if guided and not typer.confirm(
        "Run it now (foreground, Ctrl-C to stop; it stays registered)?", default=True
    ):
        typer.echo("next: run octopus init --role manual again to start it")
        typer.echo(f"      octopus runner unregister-manual {target.name} removes it")
        return checks
    if not guided and not foreground:
        checks.append(_show(_manual_service(target, directory, base)))
        typer.echo(f"next: octopus runner unregister-manual {target.name} removes it")
        return checks
    try:
        code = run_manual(directory, base, announce, target)
    except manual.ManualRunnerError as e:
        return [*checks, _show(Check("runner", Status.FAIL, str(e)))]
    status = Status.OK if code == 0 else Status.FAIL
    checks.append(_show(Check("runner", status, f"exited with code: {code}")))
    return checks


def _free_port(preferred: int) -> int:
    """`preferred` if nothing listens there (a coordinator on this machine may), else a free one."""
    import socket

    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    return preferred


def _manual_announce_setup(interactive: bool = True) -> bool:
    """Whether the runner announces itself over logos-delivery; save what that needs.

    Without questions (`octopus init owner/repo`) it always does, on the shared fleet unless a
    multiaddr is already saved.
    """
    from .agent.config import ConfigError, RunnerConfig, settings_path

    if interactive and not typer.confirm(
        "Let a coordinator in OIDC pick mode offer this runner (docs/oidc-pick.md)?", default=True
    ):
        return False
    path = settings_path()
    try:
        settings = RunnerConfig.load(path)
    except ConfigError as e:
        typer.echo(f"error: {e}")
        return False
    if interactive and not settings.entry_nodes:
        typer.echo(
            "Runners and coordinators find each other through the shared logos fleet. If they do\n"
            "not meet (the coordinator shows no runner), give the coordinator's multiaddr: the\n"
            "'multiaddr' line of `octopus coordinator status`."
        )
        while True:
            node = _ask("Coordinator multiaddr, like /ip4/<ip>/tcp/60000/p2p/<id> (empty: shared fleet)")
            if not node or node.startswith(("/", "enr:", "enrtree:")):
                break
            typer.echo("error: a multiaddr starts with / (or enr: / enrtree:)")
        if node:
            settings.entry_nodes.append(node)
    port = _free_port(settings.tcp_port)
    if port != settings.tcp_port:
        typer.echo(f"note: port {settings.tcp_port} is in use, this runner's node uses {port}")
        settings.tcp_port = port
    try:
        settings.save(path)
    except ConfigError as e:
        typer.echo(f"error: {e}")
        return False
    return True


def _manual_service(target, directory: Path, base: Path) -> Check:
    """Run the runner in the background from now on, and at login."""
    from .agent import service

    try:
        result = service.install(service.manual_spec(target.name, target.slug, base))
    except service.ServiceError as e:
        return Check(
            "service",
            Status.FAIL,
            f"{e}; run it in the foreground instead: octopus init {target.name} --foreground",
        )
    for note in result.notes:
        typer.echo(f"note: {note}")
    return Check("service", Status.CHANGED, f"installed and started: {result.path}")


# --- coordinator ----------------------------------------------------------------------


def _coordinator_repo(env_file: Path, api_url: str) -> Check:
    from pydantic import SecretStr

    from .github.setup import (
        ENV_REPO,
        ENV_TOKEN,
        ENV_WEBHOOK_URL,
        SetupOptions,
        read_env_file,
        setup,
    )
    from .github.token import TokenError, resolve_token

    saved = read_env_file(env_file)
    repo = _ask("Repository (owner/name)", saved.get(ENV_REPO, ""))
    url = _ask("Public HTTPS URL of the webhook", saved.get(ENV_WEBHOOK_URL, ""))
    if not repo or not url:
        return Check("repo setup", Status.SKIPPED, "no repo or webhook URL")
    if not url.startswith("https://"):
        return Check("repo setup", Status.FAIL, f"the webhook URL must be https://, got: {url}")
    try:
        token = resolve_token(None, os.environ, api_url=api_url)
    except TokenError as e:
        return Check("repo setup", Status.FAIL, str(e))
    coordinator_token = None if token.source == "gh" else token.value
    if coordinator_token is None and not saved.get(ENV_TOKEN):
        # The gh login reaches every repo of the user, so it is never written to the file.
        typer.echo('The coordinator needs its own token (docs/github-setup.md, "Tokens").')
        given = str(
            typer.prompt(
                "Coordinator token (empty: add it later)",
                default="",
                show_default=False,
                hide_input=True,
            )
        ).strip()
        coordinator_token = SecretStr(given) if given else None
    example = typer.confirm("Open a PR with an example workflow (a test job)?", default=True)
    opts = SetupOptions(
        repo=repo,
        webhook_url=url,
        env_file=env_file,
        example_pr=example,
        # The ping comes later, in the webhook health step, when the coordinator runs.
        ping=False,
        coordinator_token=coordinator_token,
    )
    client = _client(token.value, api_url)
    try:
        checks = setup(client, opts)
    finally:
        client.close()
    for check in checks:
        if check.status is not Status.SKIPPED:
            _show(check)
    failed = [c for c in checks if c.failed]
    if failed:
        return Check("repo setup", Status.FAIL, f"{failed[0].name}: {failed[0].detail}")
    return Check("repo setup", Status.OK, f"{repo}, settings in {env_file}")


def _coordinator_public_ip(env_file: Path) -> Check:
    from .github.setup import read_env_file, write_env_file
    from .transport.config import PUBLIC_IP_ENV

    saved = read_env_file(env_file)
    if saved.get(PUBLIC_IP_ENV):
        return Check("public IP", Status.OK, saved[PUBLIC_IP_ENV])
    ip = _ask("Public IP address of this host (runners connect to it)")
    if not ip:
        return Check("public IP", Status.WARN, f"set {PUBLIC_IP_ENV} before the start")
    write_env_file(env_file, {**saved, PUBLIC_IP_ENV: ip})
    return Check("public IP", Status.CHANGED, ip)


def _coordinator_hints(env_file: Path) -> None:
    from .github.setup import ENV_WEBHOOK_URL, read_env_file

    url = read_env_file(env_file).get(ENV_WEBHOOK_URL, "https://<your host>/github/webhook")
    typer.echo(
        "\nTLS and DNS:\n"
        f"  - Point the DNS name of {url} at this host.\n"
        "  - Put a TLS proxy in front of the webhook endpoint (127.0.0.1:8080 by default),\n"
        "    for example Caddy with deploy/Caddyfile.\n"
        "  - Open the node port (60000/tcp by default) to the runners.\n"
        "Service: deploy/octopus-coordinator.service (systemd) or deploy/docker-compose.yml,\n"
        "  see docs/coordinator-ops.md.\n"
    )


def _coordinator_health(env_file: Path, data_dir: Path | None, api_url: str) -> Check:
    from .coordinator.config import coordinator_dir
    from .coordinator.service import read_state
    from .github.app import repo_path
    from .github.setup import ENV_REPO, ENV_WEBHOOK_URL, RepoSetup, read_env_file
    from .github.token import TokenError, resolve_token

    state = read_state(coordinator_dir(data_dir))
    if state is None:
        return Check(
            "webhook health",
            Status.SKIPPED,
            f"start it: octopus coordinator start --env-file {env_file}, then run init again",
        )
    typer.echo(f"runners trust this coordinator with: --coordinator {state['public_key']}")
    saved = read_env_file(env_file)
    repo, url = saved.get(ENV_REPO), saved.get(ENV_WEBHOOK_URL)
    if not repo or not url:
        return Check("webhook health", Status.SKIPPED, "no repo set up")
    try:
        token = resolve_token(None, os.environ, api_url=api_url)
    except TokenError as e:
        return Check("webhook health", Status.FAIL, str(e))
    client = _client(token.value, api_url)
    try:
        rs = RepoSetup(client, repo)
        hooks = client.get_json(f"{repo_path(repo)}/hooks")
        hook = next((h for h in hooks if h.get("config", {}).get("url") == url), None)
        if hook is None:
            return Check("webhook health", Status.FAIL, f"no webhook for: {url}")
        ping = rs.ping(int(hook["id"]), 30.0)
    finally:
        client.close()
    return Check("webhook health", ping.status, ping.detail)


def init_coordinator(env_file: Path, data_dir: Path | None, api_url: str) -> list[Check]:
    checks = [_show(_coordinator_repo(env_file, api_url))]
    if checks[-1].failed:
        return checks
    checks.append(_show(_coordinator_public_ip(env_file)))
    _coordinator_hints(env_file)
    checks.append(_show(_coordinator_health(env_file, data_dir, api_url)))
    if checks[-1].status is Status.OK:
        typer.echo(
            "next: merge the example workflow PR (or push to it) to run a test job, then:\n"
            "      octopus jobs, and octopus explain <job> if it fails"
        )
    return checks


RoleOption = typer.Option(
    None, "--role", help="runner, coordinator or manual (a plain GitHub runner); else asked."
)
ConfigOption = typer.Option(
    None, "--config", help="Runner config file (default: the platform config dir)."
)
EnvFileOption = typer.Option(
    Path("coordinator.env"), "--env-file", help="Coordinator env file (mode 0600)."
)
DataDirOption = typer.Option(
    None, "--data-dir", help="Octopus data dir (default: OCTOPUS_DATA_DIR or the platform's)."
)
ApiUrlOption = typer.Option("https://api.github.com", "--api-url", help="GitHub API URL.")


ForegroundOption = typer.Option(
    False, "--foreground", help="With a repo argument: run in this terminal, not as a service."
)
TargetArgument = typer.Argument(
    None,
    help="owner/repo or org: register this machine as a GitHub runner for it, no questions asked.",
)


def add_init(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        target: str | None = TargetArgument,
        role: str = RoleOption,
        foreground: bool = ForegroundOption,
        config: Path | None = ConfigOption,
        env_file: Path = EnvFileOption,
        data_dir: Path | None = DataDirOption,
        api_url: str = ApiUrlOption,
    ) -> None:
        """Set up this host as a runner or a coordinator, step by step. Safe to run again.

        `octopus init owner/repo` registers this machine as a GitHub runner for that repo and
        runs it, using your gh login for the one-hour token.
        """
        if target is not None:
            if role not in (None, "manual"):
                typer.echo("error: a repo or org argument goes with the manual role", err=True)
                raise typer.Exit(code=2)
            role = "manual"
        while role not in ROLES:
            role = _ask(
                "Role of this host: runner, coordinator or manual (a plain GitHub runner)"
            ).lower()
        if role == "runner":
            checks = init_runner(config)
        elif role == "manual":
            checks = init_manual_runner(data_dir, target, foreground)
        else:
            checks = init_coordinator(env_file, data_dir, api_url)
        if any(c.failed for c in checks):
            raise typer.Exit(code=1)
