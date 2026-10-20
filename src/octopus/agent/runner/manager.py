from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...clock import Clock, SystemClock
from ...proto import FailureReason
from .download import default_data_dir, ensure_installed
from .process import OutputHandler, make_job_dir, run_runner
from .release import RunnerAsset, asset_for, platform_key

log = logging.getLogger(__name__)

DEFAULT_MAX_JOB_SECONDS = 6 * 60 * 60
_REDACTED = "***"


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    failure: FailureReason | None  # None means the job ran and the runner exited 0
    detail: str
    duration_s: float
    timed_out: bool = False


class RunnerManager:
    """Runs one ephemeral `actions/runner` per job from a verified, cached release."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        asset: RunnerAsset | None = None,
        clock: Clock | None = None,
        max_job_seconds: float = DEFAULT_MAX_JOB_SECONDS,
        client: httpx.Client | None = None,
        pass_jit_as_arg: bool = False,
    ) -> None:
        self._data_dir = data_dir or default_data_dir()
        self._asset = asset
        self._clock = clock or SystemClock()
        self._max_job_seconds = max_job_seconds
        self._client = client
        self._pass_jit_as_arg = pass_jit_as_arg
        self._stop = threading.Event()
        self._cache: Path | None = None
        self._lock = threading.Lock()

    def ensure_installed(self) -> Path:
        with self._lock:
            if self._cache is None:
                asset = self._asset or asset_for(platform_key())
                self._cache = ensure_installed(asset, self._data_dir, client=self._client)
            return self._cache

    def stop(self) -> None:
        """Ask a running job to stop; its process tree is killed."""
        self._stop.set()

    def run_job(
        self, job_id: str, jit_config: str, on_output: OutputHandler | None = None
    ) -> RunResult:
        started = self._clock.monotonic()

        def elapsed() -> float:
            return self._clock.monotonic() - started

        def scrub(text: str) -> str:
            return text.replace(jit_config, _REDACTED) if jit_config else text

        try:
            cache = self.ensure_installed()
        except Exception as e:  # noqa: BLE001 - any install failure maps to a reason
            reason = scrub(str(e))
            return RunResult(None, FailureReason.RUNNER_DOWNLOAD_FAILED, reason, elapsed())

        self._stop.clear()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        job_root = Path(tempfile.mkdtemp(prefix=f"job-{_safe(job_id)}-", dir=self._data_dir))
        job_dir = job_root / "runner"
        try:
            make_job_dir(cache, job_dir)

            def emit(line: str) -> None:
                if on_output is not None:
                    on_output(scrub(line))

            try:
                outcome = run_runner(
                    job_dir,
                    jit_config,
                    timeout_s=self._max_job_seconds,
                    stop=self._stop,
                    on_output=emit,
                    pass_as_arg=self._pass_jit_as_arg,
                )
            except OSError as e:
                detail = f"could not start runner: {scrub(str(e))}"
                return RunResult(None, FailureReason.RUNNER_START_FAILED, detail, elapsed())
            return self._result(outcome, elapsed(), scrub)
        except Exception as e:  # noqa: BLE001 - report instead of crashing the agent
            log.error("could not run job: %s: %s", job_id, scrub(str(e)))
            return RunResult(None, FailureReason.RUNNER_START_FAILED, scrub(str(e)), elapsed())
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    @staticmethod
    def _result(outcome, duration: float, scrub) -> RunResult:
        if outcome.timed_out:
            return RunResult(
                outcome.exit_code,
                FailureReason.JOB_FAILED,
                "job exceeded the maximum duration",
                duration,
                timed_out=True,
            )
        if outcome.stopped:
            return RunResult(
                outcome.exit_code, FailureReason.RUNNER_LOST, "runner was stopped", duration
            )
        if outcome.exit_code == 0:
            return RunResult(0, None, "", duration)
        last = scrub(outcome.last_line)
        detail = f"runner exited with code: {outcome.exit_code}" + (f": {last}" if last else "")
        return RunResult(outcome.exit_code, FailureReason.JOB_FAILED, detail, duration)


def _safe(job_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in job_id)[:40]

