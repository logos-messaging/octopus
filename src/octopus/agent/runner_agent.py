from __future__ import annotations

import json
import logging
import os
import platform
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ..crypto import decode_signing_public, encode_public_key, load_or_create_private_key
from ..discovery import CoordinatorDirectory
from ..proto import FailureReason, JobOffer, JobStatus
from ..transport import ChannelTransport
from .config import RunnerConfig
from .logs import DEFAULT_DRAIN_S, LogStreamer, RunnerMarker
from .offers import OfferReceiver
from .presence import PresencePublisher, RunnerInfo
from .runner import RunnerManager, default_data_dir
from .trust import Allowlist, make_offer_handler

log = logging.getLogger(__name__)

STATE_FILE = "runner.state.json"
KEY_FILE = "node.key"
LOG_SPOOL_DIR = "logs"
# On shutdown a job's log gets at most this long to reach the coordinator.
STOP_DRAIN_S = 10.0

ManagerFactory = Callable[[], RunnerManager]


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / STATE_FILE


def _pid_alive_windows(pid: int) -> bool:
    # os.kill(pid, 0) is not a probe on Windows: signal 0 is CTRL_C_EVENT.
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_state(data_dir: Path | None = None) -> dict | None:
    """The state of a running agent, or None when none is running (stale files are ignored)."""
    try:
        state = json.loads(state_path(data_dir).read_text())
    except (OSError, ValueError):
        return None
    pid = state.get("pid") if isinstance(state, dict) else None
    return state if isinstance(pid, int) and pid_alive(pid) else None


class RunnerAgent:
    """Runner host: announces presence, accepts allowlisted offers and runs them.

    The offer handler only reserves a slot and returns, because the ack is sent right after
    it returns; the job itself runs on a worker thread. Its output streams to the offering
    coordinator as it runs; after the job the slot is freed first, then the rest of the log
    is delivered (up to `log_drain_s`).
    """

    def __init__(
        self,
        config: RunnerConfig,
        allowlist: Allowlist,
        transport: ChannelTransport,
        *,
        data_dir: Path | None = None,
        manager_factory: ManagerFactory | None = None,
        log_drain_s: float = DEFAULT_DRAIN_S,
    ) -> None:
        self._config = config
        self._allowlist = allowlist
        self._transport = transport
        self._data_dir = data_dir or default_data_dir()
        self._manager_factory = manager_factory or (lambda: RunnerManager(self._data_dir))
        self._log_drain_s = log_drain_s
        self._key: X25519PrivateKey | None = None
        self._lock = threading.Lock()
        self._active: dict[str, RunnerManager] = {}
        self._workers: list[threading.Thread] = []
        self._stopping = False
        self._stopped = threading.Event()
        self._started_at = 0.0
        self._receiver: OfferReceiver | None = None
        self._presence: PresencePublisher | None = None
        self._directory: CoordinatorDirectory | None = None
        self.runner_id = ""

    def start(self) -> None:
        key = load_or_create_private_key(self._data_dir / KEY_FILE)
        self._key = key
        self._clear_spools()
        self._transport.start()
        receiver = OfferReceiver(
            self._transport,
            key,
            self._config.trusted_coordinators(),
            make_offer_handler(self._allowlist, inner=self._accept),
        )
        self.runner_id = receiver.runner_id
        presence = PresencePublisher(
            self._transport,
            RunnerInfo(
                runner_id=self.runner_id,
                os=platform.system().lower(),
                arch=platform.machine().lower(),
                labels=tuple(self._config.labels),
                capacity=self._config.max_jobs,
                pubkey=encode_public_key(key.public_key()),
            ),
        )
        self._receiver, self._presence = receiver, presence
        receiver.start()
        if self._config.root_key:
            self._directory = CoordinatorDirectory(
                self._transport,
                decode_signing_public(self._config.root_key),
                on_coordinator=receiver.add_coordinator,
            )
            self._directory.start()
        presence.start()
        self._started_at = time.time()
        self._write_state()

    def wait(self) -> None:
        self._stopped.wait()

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            managers = list(self._active.values())
            workers = list(self._workers)
        for manager in managers:
            manager.stop()
        for worker in workers:
            worker.join(timeout=30)
        if self._directory is not None:
            self._directory.stop()
        if self._presence is not None:
            self._presence.stop()
        if self._receiver is not None:
            self._receiver.stop()
        self._transport.stop()
        state_path(self._data_dir).unlink(missing_ok=True)
        self._stopped.set()

    def install_signal_handlers(self) -> None:
        """Stop on SIGINT/SIGTERM. Main thread only."""

        def _handler(signum, frame):
            threading.Thread(target=self.stop, name="octopus-stop", daemon=True).start()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    @property
    def busy(self) -> int:
        with self._lock:
            return len(self._active)

    # offers

    def _accept(self, offer: JobOffer) -> FailureReason | None:
        with self._lock:
            if self._stopping or len(self._active) >= self._config.max_jobs:
                return FailureReason.NO_IDLE_RUNNER
            manager = self._manager_factory()
            self._active[offer.job_id] = manager
            worker = threading.Thread(
                target=self._run, args=(offer, manager), name=f"octopus-job-{offer.job_id}"
            )
            self._workers.append(worker)
        self._busy_changed()
        worker.start()
        return None

    def _run(self, offer: JobOffer, manager: RunnerManager) -> None:
        self._status(
            offer, JobStatus(job_id=offer.job_id, runner_id=self.runner_id, state="runner-started")
        )
        completed: list[RunnerMarker] = []
        streamer = self._open_log(offer, completed.append)
        try:
            result = manager.run_job(
                offer.job_id,
                offer.jit_config.get_secret_value(),
                on_output=streamer.write if streamer is not None else None,
            )
            reason, detail = result.failure, result.detail or None
            # actions/runner exits 0 when a job fails, so its console result decides.
            if reason is None and completed and completed[-1].result != "Succeeded":
                reason = FailureReason.JOB_FAILED
                detail = f"job {completed[-1].name} completed with result: {completed[-1].result}"
            final = JobStatus(
                job_id=offer.job_id,
                runner_id=self.runner_id,
                state="finished",
                exit_code=result.exit_code,
                reason=reason,
                detail=detail,
            )
        except Exception:
            log.exception("could not run job: job: %s", offer.job_id)
            final = JobStatus(
                job_id=offer.job_id,
                runner_id=self.runner_id,
                state="finished",
                reason=FailureReason.JOB_FAILED,
            )
        finally:
            with self._lock:
                self._active.pop(offer.job_id, None)
            if streamer is not None:
                streamer.finish()
        self._status(offer, final)
        self._busy_changed()
        if streamer is not None:
            self._drain_log(streamer)

    # logs

    def _open_log(
        self, offer: JobOffer, on_completed: Callable[[RunnerMarker], None]
    ) -> LogStreamer | None:
        """Start streaming the job's output. Logs are best-effort: a failure here is
        reported and the job runs without them. `on_completed` gets the runner's
        "Job <name> completed with result: <result>" markers."""
        pubkey = self._config.trusted_coordinators().get(offer.coordinator_id)
        if pubkey is None or self._key is None:
            log.warning("no log stream: unknown coordinator: job: %s", offer.job_id)
            return None

        def on_marker(marker: RunnerMarker) -> None:
            if marker.kind == "job-completed":
                on_completed(marker)
            elif marker.kind == "job-started":
                self._status(
                    offer,
                    JobStatus(
                        job_id=offer.job_id,
                        runner_id=self.runner_id,
                        state="step",
                        step=marker.name,
                    ),
                )

        try:
            streamer = LogStreamer(
                self._transport,
                self._key,
                offer.coordinator_id,
                pubkey,
                offer.job_id,
                self._data_dir / LOG_SPOOL_DIR,
                secrets=[offer.jit_config.get_secret_value()],
                on_marker=on_marker,
            )
            streamer.start()
        except Exception:
            log.warning("no log stream: could not open it: job: %s", offer.job_id, exc_info=True)
            return None
        return streamer

    def _drain_log(self, streamer: LogStreamer) -> None:
        """Wait for the coordinator to have the whole log; less long when stopping."""
        started = time.monotonic()
        while not streamer.complete:
            elapsed = time.monotonic() - started
            limit = min(STOP_DRAIN_S, self._log_drain_s) if self._stopping else self._log_drain_s
            if elapsed >= limit:
                break
            time.sleep(min(0.2, limit - elapsed))
        streamer.close(timeout_s=0)

    def _clear_spools(self) -> None:
        """Spool files left by a crash hold logs nobody will send; remove them."""
        for path in (self._data_dir / LOG_SPOOL_DIR).glob("*.spool"):
            path.unlink(missing_ok=True)

    def _status(self, offer: JobOffer, status: JobStatus) -> None:
        if self._receiver is None:
            return
        try:
            self._receiver.send_status(offer.coordinator_id, status)
        except Exception:
            log.warning("could not send job status: job: %s", offer.job_id, exc_info=True)

    def _busy_changed(self) -> None:
        if self._presence is not None:
            self._presence.set_busy(self.busy)
        self._write_state()

    def _write_state(self) -> None:
        state = {
            "pid": os.getpid(),
            "runner_id": self.runner_id,
            "started_at": self._started_at,
            "busy": self.busy,
            "max_jobs": self._config.max_jobs,
            "labels": self._config.labels,
        }
        path = state_path(self._data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
