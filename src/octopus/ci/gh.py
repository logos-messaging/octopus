"""What Octopus CI asks GitHub through the user's own `gh` login. Nothing here is stored."""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Callable

Run = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


class Gh:
    def __init__(self, run: Run | None = None) -> None:
        self._run = run or _default_run

    def _api(self, *args: str) -> str | None:
        try:
            proc = self._run(["gh", "api", *args])
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def login(self) -> str | None:
        out = self._api("user", "--jq", ".login")
        return out.strip() or None if out is not None else None

    def is_admin(self, repo: str) -> bool | None:
        """Whether the logged-in user has admin rights on `repo`; None when unknown."""
        out = self._api(f"repos/{repo}", "--jq", ".permissions.admin")
        return None if out is None else out.strip() == "true"

    def is_org_admin(self, org: str) -> bool | None:
        out = self._api(f"user/memberships/orgs/{org}", "--jq", ".role")
        return None if out is None else out.strip() == "admin"

    def owner_is_org(self, owner: str) -> bool | None:
        out = self._api(f"users/{owner}", "--jq", ".type")
        return None if out is None else out.strip() == "Organization"

    def org_members(self, org: str) -> list[str] | None:
        out = self._api("--paginate", f"orgs/{org}/members", "--jq", ".[].login")
        return None if out is None else out.split()

    def collaborators(self, repo: str) -> list[str] | None:
        """Logins with write access (push) to `repo`."""
        out = self._api(
            "--paginate",
            f"repos/{repo}/collaborators",
            "--jq",
            ".[] | select(.permissions.push) | .login",
        )
        return None if out is None else out.split()

    def repo_file(self, repo: str, path: str) -> str | None:
        out = self._api(f"repos/{repo}/contents/{path}", "--jq", ".content")
        if out is None:
            return None
        try:
            return base64.b64decode(out.strip()).decode()
        except ValueError:
            return None
