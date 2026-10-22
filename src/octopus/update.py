"""`octopus update`: install a newer Octopus the same way the installer did."""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
from pathlib import Path

import httpx

REPO = "logos-messaging/octopus"
ARCHIVE = f"https://github.com/{REPO}/archive/{{ref}}.tar.gz"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
WINDOWS_HINT = "irm https://raw.githubusercontent.com/logos-messaging/octopus/main/scripts/install.ps1 | iex"

# A branch, tag or commit: no way to smuggle an option or another URL into the command.
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class UpdateError(RuntimeError):
    pass


def check_ref(ref: str) -> str:
    if not _REF.fullmatch(ref) or ".." in ref:
        raise UpdateError(f"invalid ref: {ref}")
    return ref


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` is a higher version than `current`. Unreadable versions are not newer."""
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def latest_release(client: httpx.Client | None = None) -> str:
    """The tag of the latest release (the pre-release `lib-latest` is not counted)."""
    own = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=True)
    try:
        resp = http.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        tag = str(resp.json()["tag_name"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise UpdateError(
            f"could not find the latest release: {e}; pass --ref <tag or branch> to pick one"
        ) from e
    finally:
        if own:
            http.close()
    return check_ref(tag)


def install_method(prefix: str | None = None) -> str:
    """How this Octopus was installed: `editable`, `pipx`, `uv` or `pip` (a plain virtualenv)."""
    if _is_editable():
        return "editable"
    parts = Path(prefix or sys.prefix).parts
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    if "uv" in parts and "tools" in parts:
        return "uv"
    return "pip"


def _is_editable() -> bool:
    try:
        raw = importlib.metadata.distribution("octopus").read_text("direct_url.json")
        return bool(raw and json.loads(raw).get("dir_info", {}).get("editable"))
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False


def upgrade_argv(method: str, ref: str, python: str | None = None) -> list[str]:
    """The command that installs `ref`, the way scripts/install.sh does."""
    python = python or sys.executable
    spec = f"octopus[logos] @ {ARCHIVE.format(ref=check_ref(ref))}"
    if method == "pipx":
        return ["pipx", "install", "--force", "--python", python, spec]
    if method == "uv":
        return ["uv", "tool", "install", "--force", "--python", python, spec]
    if method == "pip":
        return [
            python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", spec,
        ]
    raise UpdateError(
        "this is a development install (pip install -e): update it with git, "
        "for example: git pull && pip install -e '.[dev]'"
        if method == "editable"
        else f"unknown install method: {method}"
    )
