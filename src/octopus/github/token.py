"""Token auth: a repo-scoped personal access token (or the `gh` login) instead of the App.

`TokenClient` has the same call surface as `GitHubApp` (`GitHubClient`), so `JitRunners`
and `resolve_trust_inputs` work with either. The token is a `SecretStr`: it never appears
in logs, reprs or exceptions.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from .app import API_VERSION, DEFAULT_API_URL, GitHubApiError, _error_message

TOKEN_ENV = "GITHUB_TOKEN"
NO_TOKEN_HELP = "no GitHub token: pass --token, set GITHUB_TOKEN or log in with `gh auth login`"


class TokenError(Exception):
    """No usable token. The message never contains a token."""


@dataclass(frozen=True)
class ResolvedToken:
    value: SecretStr = field(repr=False)
    # Where it came from: "--token", "GITHUB_TOKEN" or "gh" (the GitHub CLI login).
    source: str


def api_hostname(api_url: str) -> str:
    """The `gh` hostname for an API URL: api.github.com -> github.com, GHES host as is."""
    host = urlsplit(api_url).hostname or "github.com"
    return "github.com" if host == "api.github.com" else host


def resolve_token(
    explicit: str | None,
    env: Mapping[str, str],
    *,
    api_url: str = DEFAULT_API_URL,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> ResolvedToken:
    """`--token`, then `GITHUB_TOKEN`, then `gh auth token`."""
    run = run or subprocess.run
    if explicit:
        return ResolvedToken(SecretStr(explicit.strip()), "--token")
    if env.get(TOKEN_ENV, "").strip():
        return ResolvedToken(SecretStr(env[TOKEN_ENV].strip()), TOKEN_ENV)
    try:
        out = run(
            ["gh", "auth", "token", "--hostname", api_hostname(api_url)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        raise TokenError(NO_TOKEN_HELP) from None
    token = (out.stdout or "").strip()
    if out.returncode != 0 or not token:
        raise TokenError(NO_TOKEN_HELP)
    return ResolvedToken(SecretStr(token), "gh")


class TokenClient:
    """API calls authenticated with a token. `installation_id` arguments are ignored.

    `client` is injectable for tests (e.g. `httpx.Client(transport=httpx.MockTransport(...))`).
    """

    def __init__(
        self,
        token: SecretStr | str,
        *,
        api_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token if isinstance(token, SecretStr) else SecretStr(token)
        self._client = client or httpx.Client(timeout=15.0)
        self._client.base_url = api_url.rstrip("/")
        self._client.headers.update(
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        )

    def __repr__(self) -> str:
        return f"TokenClient(api_url={str(self._client.base_url)!r})"

    def close(self) -> None:
        self._client.close()

    def repo_installation_id(self, repo: str) -> int:
        return 0  # a token has no installation

    def request(
        self, method: str, path: str, installation_id: int = 0, **kw: Any
    ) -> httpx.Response:
        headers = {
            **(kw.pop("headers", None) or {}),
            "Authorization": f"Bearer {self._token.get_secret_value()}",
        }
        return self._client.request(method, path, headers=headers, **kw)

    def get_json(self, path: str, installation_id: int = 0) -> Any:
        resp = self.request("GET", path)
        if resp.status_code != 200:
            raise GitHubApiError("GET", path, resp.status_code, _error_message(resp))
        return resp.json()
