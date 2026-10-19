"""GitHub App authentication: app JWT, then per-installation access tokens (cached).

Tokens and the private key are secrets: they never appear in logs, reprs or exceptions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx
import jwt

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub allows a JWT to live at most 10 minutes; iat is backdated for clock drift.
JWT_LIFETIME_S = 9 * 60
JWT_BACKDATE_S = 60
# Installation tokens live 1 hour; refresh this long before they expire.
TOKEN_REFRESH_MARGIN_S = 5 * 60


class GitHubApiError(Exception):
    """A GitHub API call failed. Carries the status and GitHub's message, never credentials."""

    def __init__(self, method: str, path: str, status: int, message: str = "") -> None:
        self.method = method
        self.path = path
        self.status = status
        super().__init__(f"{method} {path} -> {status}{': ' + message if message else ''}")


@dataclass(frozen=True)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: float  # unix time


def repo_path(repo: str) -> str:
    """`/repos/{owner}/{name}` for "owner/name", each segment URL-quoted."""
    owner, sep, name = repo.partition("/")
    if not owner or not sep or not name:
        raise ValueError(f"expected owner/name, got {repo!r}")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


class GitHubApp:
    """Authenticates as a GitHub App and makes installation-scoped API calls.

    `client` is injectable for tests (e.g. `httpx.Client(transport=httpx.MockTransport(...))`).
    Thread-safe.
    """

    def __init__(
        self,
        app_id: str | int,
        private_key: str | bytes,
        *,
        api_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # The App ID or the Client ID; GitHub accepts either as the JWT issuer.
        self._issuer = str(app_id)
        self._key = private_key
        self._clock = clock
        self._client = client or httpx.Client(timeout=10.0)
        self._client.base_url = api_url.rstrip("/")
        self._client.headers.update(
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        )
        self._lock = Lock()
        self._jwt: tuple[str, float] | None = None  # (token, expires_at)
        self._tokens: dict[int, InstallationToken] = {}

    @classmethod
    def from_key_file(cls, app_id: str | int, key_path: str | Path, **kw: Any) -> GitHubApp:
        return cls(app_id, Path(key_path).read_bytes(), **kw)

    def __repr__(self) -> str:
        return f"GitHubApp(issuer={self._issuer!r}, api_url={str(self._client.base_url)!r})"

    def close(self) -> None:
        self._client.close()

    def app_jwt(self) -> str:
        """A JWT authenticating as the App itself (for `/app/...` endpoints)."""
        with self._lock:
            now = self._clock()
            if self._jwt is None or self._jwt[1] - now < JWT_BACKDATE_S:
                iat = int(now) - JWT_BACKDATE_S
                exp = int(now) + JWT_LIFETIME_S
                claims = {"iat": iat, "exp": exp, "iss": self._issuer}
                self._jwt = (jwt.encode(claims, self._key, algorithm="RS256"), float(exp))
            return self._jwt[0]

    def installation_token(self, installation_id: int) -> str:
        """An installation access token, from cache unless it is close to expiry."""
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - self._clock() > TOKEN_REFRESH_MARGIN_S:
                return cached.token
        token = self._create_installation_token(installation_id)
        with self._lock:
            self._tokens[installation_id] = token
        return token.token

    def _create_installation_token(self, installation_id: int) -> InstallationToken:
        path = f"/app/installations/{int(installation_id)}/access_tokens"
        resp = self._client.post(path, headers={"Authorization": f"Bearer {self.app_jwt()}"})
        if resp.status_code != 201:
            raise GitHubApiError("POST", path, resp.status_code, _error_message(resp))
        try:
            data = resp.json()
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            return InstallationToken(token=data["token"], expires_at=expires_at.timestamp())
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise GitHubApiError("POST", path, resp.status_code, "malformed token response") from e

    def request(self, method: str, path: str, installation_id: int, **kw: Any) -> httpx.Response:
        """An API call authenticated as `installation_id`. The caller checks the status.

        On 401 the cached token is dropped and the call retried once (revoked token).
        """
        extra_headers = kw.pop("headers", None) or {}
        for attempt in range(2):
            token = self.installation_token(installation_id)
            headers = {**extra_headers, "Authorization": f"Bearer {token}"}
            resp = self._client.request(method, path, headers=headers, **kw)
            if resp.status_code != 401 or attempt:
                return resp
            with self._lock:
                self._tokens.pop(installation_id, None)
        return resp

    def get_json(self, path: str, installation_id: int) -> Any:
        resp = self.request("GET", path, installation_id)
        if resp.status_code != 200:
            raise GitHubApiError("GET", path, resp.status_code, _error_message(resp))
        return resp.json()

    def repo_installation_id(self, repo: str) -> int:
        """The installation id covering `owner/name` (when a payload did not carry one)."""
        path = f"{repo_path(repo)}/installation"
        resp = self._client.get(path, headers={"Authorization": f"Bearer {self.app_jwt()}"})
        if resp.status_code != 200:
            raise GitHubApiError("GET", path, resp.status_code, _error_message(resp))
        return int(resp.json()["id"])
