"""`octopus github setup|teardown`: configure one repo for Octopus with a token, no App."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

github_app = typer.Typer(
    no_args_is_help=True, help="Set up a single repo for Octopus with a token (no GitHub App)."
)

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_BAD_CONFIG = 3
EXIT_NETWORK = 4

DEFAULT_ENV_FILE = Path("coordinator.env")

RepoOption = typer.Option(None, "--repo", help="Repository, owner/name.")
WebhookUrlOption = typer.Option(
    None,
    "--webhook-url",
    help="Public HTTPS URL of the coordinator's webhook, "
    "e.g. https://octopus.example.org/github/webhook.",
)
TokenOption = typer.Option(
    None,
    "--token",
    help="Token for the setup (default: GITHUB_TOKEN, then the gh login).",
    show_default=False,
)
EnvFileOption = typer.Option(
    DEFAULT_ENV_FILE, "--env-file", help="Coordinator env file to write (mode 0600)."
)
YesOption = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")
ApiUrlOption = typer.Option(
    "https://api.github.com",
    "--api-url",
    help="GitHub API URL (GitHub Enterprise Server: https://HOST/api/v3).",
)


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _value(given: str | None, saved: str | None, flag: str, prompt: str) -> str:
    """A flag, else the env file's value, else a prompt (interactive only)."""
    if given:
        return given.strip()
    if saved:
        return saved
    if _interactive():
        return str(typer.prompt(prompt)).strip()
    raise _fail(f"missing {flag}", EXIT_USAGE)


def _check_repo(repo: str) -> None:
    from .github.app import repo_path

    try:
        repo_path(repo)
    except ValueError as e:
        raise _fail(f"bad --repo: {e}", EXIT_USAGE) from e


def _check_webhook_url(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise _fail(f"--webhook-url must be a public https:// URL, got: {url}", EXIT_USAGE)


def _token(token: str | None, api_url: str):
    from .github.token import TokenError, resolve_token

    try:
        return resolve_token(token, os.environ, api_url=api_url)
    except TokenError as e:
        raise _fail(str(e), EXIT_BAD_CONFIG) from e


def _client(token, api_url: str):
    """The API client; replaced in tests."""
    from .github.token import TokenClient

    return TokenClient(token, api_url=api_url)


def _confirm(yes: bool, summary: list[tuple[str, str]], question: str) -> None:
    width = max(len(k) for k, _ in summary)
    for key, value in summary:
        typer.echo(f"  {key:<{width}}  {value}")
    if yes:
        return
    if not _interactive():
        raise _fail("pass --yes to run without a prompt", EXIT_USAGE)
    typer.confirm(question, abort=True)


def _print_checks(checks) -> None:
    width = max(len(c.name) for c in checks)
    for c in checks:
        typer.echo(f"[{c.status.value:>7}] {c.name:<{width}}  {c.detail}")


def _run(fn, client):
    import httpx

    try:
        return fn()
    except httpx.HTTPError as e:
        raise _fail(f"could not reach GitHub: {e}", EXIT_NETWORK) from e
    finally:
        client.close()


@github_app.command("setup")
def github_setup(
    repo: str | None = RepoOption,
    webhook_url: str | None = WebhookUrlOption,
    token: str | None = TokenOption,
    coordinator_token: str | None = typer.Option(
        None,
        "--coordinator-token",
        help="Token the coordinator uses, written to the env file "
        "(default: --token or GITHUB_TOKEN; the gh login is never written).",
        show_default=False,
    ),
    env_file: Path = EnvFileOption,
    example_pr: bool = typer.Option(
        True, "--example-pr/--no-example-pr", help="Open a PR adding an example workflow."
    ),
    ping: bool = typer.Option(
        True, "--ping/--no-ping", help="Ping the webhook and check the coordinator answers."
    ),
    ping_timeout: float = typer.Option(30.0, "--ping-timeout", help="Seconds to wait for it."),
    rotate_secret: bool = typer.Option(
        False, "--rotate-secret", help="Make a new webhook secret even if one is known."
    ),
    yes: bool = YesOption,
    api_url: str = ApiUrlOption,
) -> None:
    """Configure one repo: webhook, coordinator env file, example workflow PR, ping check.

    Safe to rerun: what is already set up is left as is.
    """
    from pydantic import SecretStr

    from .github.setup import ENV_REPO, ENV_WEBHOOK_URL, SetupOptions, read_env_file, setup

    saved = read_env_file(env_file)
    repo = _value(repo, saved.get(ENV_REPO), "--repo", "Repository (owner/name)")
    _check_repo(repo)
    webhook_url = _value(
        webhook_url, saved.get(ENV_WEBHOOK_URL), "--webhook-url", "Webhook URL (https://...)"
    )
    _check_webhook_url(webhook_url)
    tok = _token(token, api_url)
    if coordinator_token:
        coord, coord_desc = SecretStr(coordinator_token), "from --coordinator-token"
    elif tok.source != "gh":
        coord, coord_desc = tok.value, f"the {tok.source} token"
    else:
        coord, coord_desc = None, "none: the gh login is not written (see --coordinator-token)"
    _confirm(
        yes,
        [
            ("repo", repo),
            ("webhook URL", webhook_url),
            ("env file", str(env_file)),
            ("token", f"from {tok.source}"),
            ("coordinator token", coord_desc),
            ("example PR", "yes" if example_pr else "no"),
            ("ping", "yes" if ping else "no"),
        ],
        f"Set up {repo}?",
    )
    opts = SetupOptions(
        repo=repo,
        webhook_url=webhook_url,
        env_file=env_file,
        example_pr=example_pr,
        ping=ping,
        ping_timeout_s=ping_timeout,
        rotate_secret=rotate_secret,
        coordinator_token=coord,
    )
    client = _client(tok.value, api_url)
    checks = _run(lambda: setup(client, opts), client)
    _print_checks(checks)
    if any(c.name == "ping" and c.failed for c in checks):
        typer.echo(f"next: start the coordinator with {env_file}, then rerun this command")
    if any(c.failed for c in checks):
        raise typer.Exit(code=EXIT_FAILED)


@github_app.command("teardown")
def github_teardown(
    repo: str | None = RepoOption,
    webhook_url: str | None = WebhookUrlOption,
    token: str | None = TokenOption,
    env_file: Path = EnvFileOption,
    yes: bool = YesOption,
    api_url: str = ApiUrlOption,
) -> None:
    """Remove the webhook, idle octopus-* runners and the open example PR from a repo."""
    from .github.setup import ENV_REPO, ENV_WEBHOOK_URL, read_env_file, teardown

    saved = read_env_file(env_file)
    repo = _value(repo, saved.get(ENV_REPO), "--repo", "Repository (owner/name)")
    _check_repo(repo)
    if not webhook_url and saved.get(ENV_REPO, "").casefold() == repo.casefold():
        webhook_url = saved.get(ENV_WEBHOOK_URL)
    if not webhook_url and _interactive():
        webhook_url = str(typer.prompt("Webhook URL to remove (https://...)")).strip()
    tok = _token(token, api_url)
    _confirm(
        yes,
        [
            ("repo", repo),
            ("webhook URL", webhook_url or "unknown"),
            ("token", f"from {tok.source}"),
        ],
        f"Remove Octopus from {repo}?",
    )
    client = _client(tok.value, api_url)
    checks = _run(lambda: teardown(client, repo, webhook_url), client)
    _print_checks(checks)
    if env_file.exists():
        typer.echo(f"note: {env_file} was left in place; delete it if it is no longer needed")
    if any(c.failed for c in checks):
        raise typer.Exit(code=EXIT_FAILED)
