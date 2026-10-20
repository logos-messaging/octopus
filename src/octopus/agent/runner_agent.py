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

from ..crypto import encode_public_key, load_or_create_private_key
from ..proto import FailureReason, JobOffer, JobStatus
from ..transport import ChannelTransport
from .config import RunnerConfig
from .offers import OfferReceiver
from .presence import PresencePublisher, RunnerInfo
from .runner import RunnerManager, default_data_dir
from .trust import Allowlist, make_offer_handler

log = logging.getLogger(__name__)

STATE_FILE = "runner.state.json"
KEY_FILE = "node.key"

ManagerFactory = Callable[[], RunnerManager]


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / STATE_FILE


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
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
    it returns; the job itself runs on a worker thread.
    """

    def __init__(
        self,
        config: RunnerConfig,
        allowlist: Allowlist,
        transport: ChannelTransport,
        *,
        data_dir: Path | None = None,
        manager_factory: ManagerFactory | None = None,
    ) -> None:
        self._config = config
        self._allowlist = allowlist
        self._transport = transport
        self._data_dir = data_dir or default_data_dir()
        self._manager_factory = manager_factory or (lambda: RunnerManager(self._data_dir))
        self._lock = threading.Lock()
        self._active: dict[str, RunnerManager] = {}
        self._workers: list[threading.Thread] = []
        self._stopping = False
        self._stopped = threading.Event()
        self._started_at = 0.0
        self._receiver: OfferReceiver | None = None
        self._presence: PresencePublisher | None = None
        self.runner_id = ""

    def start(self) -> None:
        key = load_or_create_private_key(self._data_dir / KEY_FILE)
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
        try:
            result = manager.run_job(offer.job_id, offer.jit_config.get_secret_value())
            final = JobStatus(
                job_id=offer.job_id,
                runner_id=self.runner_id,
                state="finished",
                exit_code=result.exit_code,
                reason=result.failure,
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
        self._status(offer, final)
        self._busy_changed()

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
