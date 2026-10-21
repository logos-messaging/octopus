"""Register this machine as a plain GitHub self-hosted runner, by hand.

The owner copies a one-hour registration token from the GitHub UI and this module runs the
official `config.sh` / `run.sh` with it, so the token is typed once and never stored. The
runner is NOT driven by a coordinator: GitHub gives it jobs directly, with no scheduling
and no trust policy. It is for trying a workflow on a laptop (see docs/manual-runner.md).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .runner.download import RunnerDownloadError, ensure_installed
from .runner.process import IS_WINDOWS, make_job_dir
from .runner.release import RunnerAsset, UnsupportedPlatformError, asset_for, platform_key

TOKEN_ENV = "OCTOPUS_REGISTRATION_TOKEN"
REMOVAL_TOKEN_ENV = "OCTOPUS_REMOVAL_TOKEN"
# actions/runner reads its command-line arguments from ACTIONS_RUNNER_INPUT_<NAME>; the
# token goes there so it is not visible in `ps`.
_RUNNER_TOKEN_ENV = "ACTIONS_RUNNER_INPUT_TOKEN"
_SECRET_ENV = (TOKEN_ENV, REMOVAL_TOKEN_ENV, _RUNNER_TOKEN_ENV)
DEFAULT_LABELS = ("octopus",)
LABEL_FILE = ".octopus-label"
UNIQUE_LABEL_PREFIX = "octopus-"
_PART = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_TARGET = re.compile(rf"^(?P<owner>{_PART})(?:/(?P<repo>{_PART}))?$")
_URL = re.compile(rf"^(?P<base>https://[^/\s]+)/(?P<owner>{_PART})(?:/(?P<repo>{_PART}))?/?$")


class ManualRunnerError(RuntimeError):
    """The manual runner could not be set up, started or removed."""


@dataclass(frozen=True)
class Target:
    """What the runner serves: one repo, or a whole org."""

    owner: str
    repo: str | None
    base: str = "https://github.com"

    @property
    def url(self) -> str:
        return f"{self.base}/{self.owner}" + (f"/{self.repo}" if self.repo else "")

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}" if self.repo else self.owner

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}" if self.repo else self.owner

    @property
    def new_runner_page(self) -> str:
        scope = f"{self.owner}/{self.repo}" if self.repo else f"organizations/{self.owner}"
        return f"{self.base}/{scope}/settings/actions/runners/new"


def parse_target(text: str) -> Target:
    """`owner/repo`, `owner` (an org) or the https URL of either."""
    text = text.strip()
    url = _URL.match(text)
    if url:
        return Target(url["owner"], url["repo"], url["base"])
    short = _TARGET.match(text)
    if short:
        return Target(short["owner"], short["repo"])
    raise ManualRunnerError(
        f"could not read runner target: {text!r}: use owner/repo, owner, or a https URL"
    )


def runner_dir(data_dir: Path, target: Target) -> Path:
    return data_dir / "manual-runner" / target.slug


def is_configured(directory: Path) -> bool:
    return (directory / ".runner").is_file()


def default_name() -> str:
    return f"octopus-manual-{socket.gethostname().split('.')[0]}"


def unique_label(name: str) -> str:
    """The label that only this runner has (`octopus-<name>`), used by OIDC pick mode."""
    return name if name.startswith(UNIQUE_LABEL_PREFIX) else UNIQUE_LABEL_PREFIX + name


def saved_label(directory: Path) -> str | None:
    try:
        return (directory / LABEL_FILE).read_text().strip() or None
    except OSError:
        return None


def save_label(directory: Path, label: str) -> None:
    (directory / LABEL_FILE).write_text(label + "\n")


def prepare(
    data_dir: Path,
    target: Target,
    *,
    asset: RunnerAsset | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """The runner tree of this target: a copy of the verified, cached release.

    config.sh writes its credentials into the tree, so each target gets its own copy,
    kept between runs.
    """
    directory = runner_dir(data_dir, target)
    if directory.is_dir():
        return directory
    try:
        cache = ensure_installed(asset or asset_for(platform_key()), data_dir, client=client)
    except (RunnerDownloadError, UnsupportedPlatformError) as e:
        raise ManualRunnerError(f"could not prepare runner: {e}") from e
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Copy beside the final place and rename at the end, so a Ctrl-C or a full disk never
    # leaves a half tree that the next run would take for a good one.
    work = Path(tempfile.mkdtemp(prefix=".prepare-", dir=directory.parent))
    try:
        make_job_dir(cache, work / "tree")
        (work / "tree").rename(directory)
    except OSError as e:
        raise ManualRunnerError(f"could not copy runner: {directory}: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return directory


def _child_env() -> dict[str, str]:
    """The environment of the runner processes, without any token.

    Runner.Worker hands its environment to every job step, so a token left in it can be read by
    any workflow that runs on this runner.
    """
    return {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}


def _script(directory: Path, name: str) -> list[str]:
    if IS_WINDOWS:
        return ["cmd.exe", "/c", f"{name}.cmd"]
    return [str(directory / f"{name}.sh")]


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _tail(text: str, lines: int = 8) -> str:
    kept = [line for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _run_config(directory: Path, args: list[str], token: str) -> None:
    env = _child_env()
    env[_RUNNER_TOKEN_ENV] = token
    try:
        proc = subprocess.run(
            [*_script(directory, "config"), *args],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as e:
        raise ManualRunnerError(f"could not start config script: {directory}: {e}") from e
    if proc.returncode != 0:
        output = _scrub(_tail(proc.stdout + "\n" + proc.stderr), token)
        raise ManualRunnerError(
            f"could not configure runner: exit code {proc.returncode}: {output}"
        )


def configure(
    directory: Path,
    target: Target,
    token: str,
    *,
    name: str,
    labels: list[str],
    replace: bool = True,
) -> None:
    """Register the runner with GitHub (needs the one-hour registration token)."""
    args = [
        "--unattended",
        "--url",
        target.url,
        "--name",
        name,
        "--labels",
        ",".join(labels),
        "--work",
        "_work",
    ]
    if replace:
        args.append("--replace")
    _run_config(directory, args, token)


def remove(directory: Path, token: str) -> None:
    """Unregister the runner from GitHub (needs a removal token, also valid for one hour)."""
    _run_config(directory, ["remove"], token)


def start(directory: Path) -> int:
    """Run the runner in the foreground until it exits or Ctrl-C. Returns its exit code."""
    try:
        proc = subprocess.Popen(_script(directory, "run"), cwd=directory, env=_child_env())
    except OSError as e:
        raise ManualRunnerError(f"could not start run script: {directory}: {e}") from e
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C reached the runner too; let it finish the job it is closing.
        try:
            return proc.wait(timeout=60)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.terminate()
            return proc.wait()


def delete_tree(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
