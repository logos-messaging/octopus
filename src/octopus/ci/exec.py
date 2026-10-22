"""Running a leg on this machine, and reporting it to GitHub with the owner's own login."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .engine import ExecResult, LegInfo
from .sandbox import Sandbox

log = logging.getLogger(__name__)

SCRIPT = ".octopus/ci.sh"
# Never handed to a job: the owner's GitHub credentials and SSH agent. This hides them from the
# environment only: a job runs as the owner and can still read the `gh` config, the keychain
# and the SSH keys (see docs/decentralized-ci.md).
_SECRET_ENV = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "OCTOPUS_TEAM_PASSPHRASE",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
)
STATUS_CONTEXT = "octopus/ci"
MAX_DESCRIPTION = 140


class GitExecutor:
    """Checks out the commit in a fresh directory and runs `.octopus/ci.sh` there.

    The checkout uses the machine owner's git credentials (`gh auth setup-git`), so it works for
    any repo the owner can read. The directory is deleted afterwards. Without a `sandbox` this is
    not one: the job runs as the owner (see docs/decentralized-ci.md). With one, the script runs in
    a throwaway container that sees only the checkout, and the owner's credentials stay outside.
    """

    def __init__(
        self,
        work_root: Path,
        *,
        url: str = "https://github.com/{repo}.git",
        timeout_s: float = 6 * 3600.0,
        poll_s: float = 0.5,
        sandbox: Sandbox | None = None,
    ) -> None:
        self._root = work_root
        self._url = url
        self._sandbox = sandbox
        self._timeout_s = timeout_s
        self._poll_s = poll_s

    def run(
        self, leg: LegInfo, on_line: Callable[[str], None], should_stop: Callable[[], bool]
    ) -> ExecResult:
        self._root.mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha256(leg.leg_id.encode()).hexdigest()[:12]
        work = Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=self._root))
        try:
            fail = self._checkout(leg, work, on_line, should_stop)
            if fail is not None:
                return fail
            script = work / SCRIPT
            if not script.is_file():
                return ExecResult(127, f"no {SCRIPT} in the repo")
            if self._sandbox is not None:
                return self._run_sandboxed(leg, tag, work, on_line, should_stop)
            return self._stream(
                ["bash", str(script)], work, self._job_env(leg), on_line, should_stop
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run_sandboxed(self, leg, tag, work, on_line, should_stop) -> ExecResult:
        assert self._sandbox is not None
        why = Sandbox.supported()
        if why is not None:
            return ExecResult(126, f"could not sandbox the job: {why}")
        name = f"octopus-{tag}"
        argv = self._sandbox.run_argv(name, str(work), SCRIPT, self._leg_vars(leg))
        host_env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        try:
            return self._stream(argv, work, host_env, on_line, should_stop)
        finally:
            self._remove_container(name)

    def _remove_container(self, name: str) -> None:
        """Make sure nothing outlives the leg, whatever happened to the client process."""
        assert self._sandbox is not None
        try:
            subprocess.run(
                self._sandbox.kill_argv(name),
                capture_output=True,
                timeout=30,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            log.warning("could not remove the container: %s", name)

    def _checkout(self, leg, work, on_line, should_stop) -> ExecResult | None:
        url = self._url.format(repo=leg.repo)
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        env["GIT_TERMINAL_PROMPT"] = "0"
        for cmd in (
            ["git", "init", "-q"],
            ["git", "fetch", "-q", "--depth", "1", url, leg.sha],
            ["git", "checkout", "-q", "FETCH_HEAD"],
        ):
            code = self._stream(cmd, work, env, on_line, should_stop)
            if code.exit_code != 0:
                return ExecResult(code.exit_code, f"checkout failed: {cmd[1]}")
        return None

    @staticmethod
    def _leg_vars(leg: LegInfo) -> dict[str, str]:
        """What a job is told about its leg; the only variables a sandboxed job gets."""
        return {
            "CI": "true",
            "OCTOPUS_CI": "true",
            "OCTOPUS_REPO": leg.repo,
            "OCTOPUS_SHA": leg.sha,
            "OCTOPUS_REF": leg.ref,
            "OCTOPUS_EVENT": leg.event,
            "OCTOPUS_OS": leg.os,
            "OCTOPUS_RUN_ID": leg.run_id,
        }

    @classmethod
    def _job_env(cls, leg: LegInfo) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        env.update(cls._leg_vars(leg))
        return env

    def _stream(self, cmd, cwd, env, on_line, should_stop) -> ExecResult:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as e:
            return ExecResult(127, f"could not start {cmd[0]}: {e.strerror}")
        assert proc.stdout is not None
        reader = threading.Thread(
            target=lambda: [on_line(line.rstrip("\n")) for line in proc.stdout],  # type: ignore[union-attr]
            daemon=True,
        )
        reader.start()
        deadline = time.monotonic() + self._timeout_s
        while proc.poll() is None:
            if should_stop() or time.monotonic() > deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader.join(timeout=5)
                return ExecResult(143, "stopped" if should_stop() else "timed out")
            time.sleep(self._poll_s)
        reader.join(timeout=5)
        return ExecResult(int(proc.returncode))


class GhStatusPoster:
    """Commit statuses with the machine owner's `gh` login (write access is enough)."""

    def __init__(self, gh: str | None = None) -> None:
        self._gh = gh or shutil.which("gh") or "gh"

    def status_sha(self, leg: LegInfo) -> str:
        """The commit to put the status on.

        On a pull request the token's commit is GitHub's synthetic merge commit, which no PR
        shows. The PR head, the one the PR page lists statuses for, is its second parent.
        """
        if leg.event != "pull_request":
            return leg.sha
        try:
            proc = subprocess.run(
                [self._gh, "api", f"repos/{leg.repo}/git/commits/{leg.sha}", "--jq", ".parents[].sha"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return leg.sha
        parents = proc.stdout.split() if proc.returncode == 0 else []
        if len(parents) != 2:
            log.warning(
                "could not find the PR head of %s: the status of %s goes to the merge commit, "
                "which no PR shows",
                leg.sha,
                leg.leg_id,
            )
            return leg.sha
        return parents[1]

    def post(self, leg: LegInfo, state: str, description: str) -> None:
        proc = subprocess.run(
            [
                self._gh,
                "api",
                "-X",
                "POST",
                f"repos/{leg.repo}/statuses/{self.status_sha(leg)}",
                "-f",
                f"state={state}",
                "-f",
                f"context={STATUS_CONTEXT} ({leg.os})",
                "-f",
                f"description={description[:MAX_DESCRIPTION]}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"could not post the commit status: exit code: {proc.returncode}")


class GhRunProbe:
    """Asks GitHub, with the owner's `gh`, whether the run of a running leg is still wanted.

    The Actions run of a handed-off leg completes (success) as soon as the submit job exits, long
    before the leg ends, so `completed` means nothing. A run is cancelled when its attempt
    concludes `cancelled`. A completed run can no longer be cancelled by a newer push, so a pull
    request leg is also stale when the PR was closed or its head is no longer the one the leg was
    started for. Push legs are never superseded by a newer commit: each commit gets its own
    result. Any error, rate limit or doubt answers None: the leg keeps running.
    """

    def __init__(self, gh: str | None = None, timeout_s: float = 20.0) -> None:
        self._gh = gh or shutil.which("gh") or "gh"
        self._timeout_s = timeout_s
        # A completed attempt never changes, so it is asked about once per leg.
        self._completed: set[str] = set()
        self._heads: dict[str, str] = {}

    def stale(self, leg: LegInfo) -> str | None:
        try:
            return self._stale(leg)
        except Exception:
            log.debug("could not probe the run: %s", leg.leg_id, exc_info=True)
            return None

    def _stale(self, leg: LegInfo) -> str | None:
        if leg.leg_id not in self._completed:
            out = self._api(
                f"repos/{leg.repo}/actions/runs/{leg.run_id}/attempts/{leg.attempt}",
                "[.status, .conclusion] | @tsv",
            )
            if out is None:
                return None
            status, _, conclusion = out.strip().partition("\t")
            if conclusion == "cancelled":
                return "the run was cancelled"
            if status != "completed":
                return None  # still running: a newer push cancels it if the workflow says so
            if len(self._completed) >= 1000:
                self._completed.clear()
            self._completed.add(leg.leg_id)
        return self._pull_request_stale(leg) if leg.event == "pull_request" else None

    def _pull_request_stale(self, leg: LegInfo) -> str | None:
        match = re.fullmatch(r"refs/pull/(\d+)/merge", leg.ref)
        if match is None:
            return None
        head = self._heads.get(leg.leg_id)
        if head is None:
            out = self._api(f"repos/{leg.repo}/git/commits/{leg.sha}", ".parents[].sha")
            parents = (out or "").split()
            if len(parents) != 2:
                return None
            head = parents[1]
            if len(self._heads) >= 1000:
                self._heads.clear()
            self._heads[leg.leg_id] = head
        out = self._api(f"repos/{leg.repo}/pulls/{match.group(1)}", "[.state, .head.sha] | @tsv")
        if out is None:
            return None
        state, _, current = out.strip().partition("\t")
        if state == "closed":
            return "the pull request was closed"
        if current and current != head:
            return "the pull request has a newer commit"
        return None

    def _api(self, path: str, jq: str) -> str | None:
        try:
            proc = subprocess.run(
                [self._gh, "api", path, "--jq", jq],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None
