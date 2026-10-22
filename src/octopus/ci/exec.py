"""Running a leg on this machine, and reporting it to GitHub with the owner's own login."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .engine import ExecResult, LegInfo

SCRIPT = ".octopus/ci.sh"
# Never handed to a job: the owner's GitHub credentials.
_SECRET_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "OCTOPUS_TEAM_PASSPHRASE")
STATUS_CONTEXT = "octopus/ci"
MAX_DESCRIPTION = 140


class GitExecutor:
    """Checks out the commit in a fresh directory and runs `.octopus/ci.sh` there.

    The checkout uses the machine owner's git credentials (`gh auth setup-git`), so it works for
    any repo the owner can read. The directory is deleted afterwards. This is not a sandbox: the
    job runs as the owner (see docs/decentralized-ci.md).
    """

    def __init__(
        self,
        work_root: Path,
        *,
        url: str = "https://github.com/{repo}.git",
        timeout_s: float = 6 * 3600.0,
        poll_s: float = 0.5,
    ) -> None:
        self._root = work_root
        self._url = url
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
            return self._stream(
                ["bash", str(script)], work, self._job_env(leg), on_line, should_stop
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
    def _job_env(leg: LegInfo) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        env.update(
            CI="true",
            OCTOPUS_CI="true",
            OCTOPUS_REPO=leg.repo,
            OCTOPUS_SHA=leg.sha,
            OCTOPUS_REF=leg.ref,
            OCTOPUS_EVENT=leg.event,
            OCTOPUS_OS=leg.os,
            OCTOPUS_RUN_ID=leg.run_id,
        )
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

    def post(self, leg: LegInfo, state: str, description: str) -> None:
        proc = subprocess.run(
            [
                self._gh,
                "api",
                "-X",
                "POST",
                f"repos/{leg.repo}/statuses/{leg.sha}",
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
