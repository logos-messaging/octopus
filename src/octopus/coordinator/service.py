"""The coordinator process: what `octopus coordinator start` runs.

Puts the pieces together: the logos-delivery node (transport), the presence registry, the
scheduler with its offer sender and JIT runner provider, the log collector, and the GitHub
webhook endpoint. A queued `workflow_job` goes: webhook (answered at once) -> trust worker
(GitHub API: fork check, sender) -> scheduler -> encrypted offer to a runner -> statuses
and the log back to the stores that `octopus jobs`, `logs` and `explain` read.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from ..crypto import encode_public_key, load_or_create_private_key, node_id
from ..crypto.signing import (
    decode_signing_public,
    encode_signing_public,
    load_or_create_signing_key,
)
from ..github.app import GitHubApiError, GitHubClient
from ..github.runners import JitRunnerError, JitRunners, make_jit_provider
from ..github.trust import resolve_trust_inputs
from ..github.webhooks import OCTOPUS_LABEL, WebhookReceiver, WorkflowJobEvent
from ..oidc import OidcVerifier
from ..policy import TrustPolicy
from ..proto import FailureReason
from ..transport import ChannelTransport
from .cluster import ClusterConfig, CoordinatorCluster, to_coord_job, to_job_context
from .config import CoordinatorConfig
from .jobs import SqliteJobStore, State
from .logs import LogCollector, LogStore
from .offers import OfferSender
from .pick import PickService
from .picker import PICK_LABEL_PREFIX, Picker
from .registry import Registry
from .scheduler import Scheduler, SchedulerConfig
from .webhook_server import WebhookServer

log = logging.getLogger(__name__)

KEY_FILE = "coordinator.key"
SIGNING_KEY_FILE = "cluster.key"
JOBS_DB = "jobs.db"
LOGS_DB = "logs.db"
STATE_FILE = "coordinator.state.json"
ACTOR = "coordinator"
QUEUE_SIZE = 1000
TRUST_ATTEMPTS = 3
# How often the state file (runners online, for `coordinator status`) is refreshed.
MAINTENANCE_S = 10.0
CLUSTER_TICK_S = 1.0
SWEEP_EVERY_S = 300.0
PURGE_EVERY_S = 24 * 3600.0
# Repos seen within this long get their orphaned runners swept.
SWEEP_WINDOW_S = 24 * 3600.0
_STOP = object()


def state_path(directory: Path) -> Path:
    return directory / STATE_FILE


def read_state(directory: Path) -> dict | None:
    """The state of a running coordinator, or None (no file, or its process is gone)."""
    from ..agent.runner_agent import pid_alive

    try:
        state = json.loads(state_path(directory).read_text())
    except (OSError, ValueError):
        return None
    pid = state.get("pid") if isinstance(state, dict) else None
    return state if isinstance(pid, int) and pid_alive(pid) else None


class Coordinator:
    """One coordinator. `start()`, then `wait()` until `stop()` (or a signal)."""

    def __init__(
        self,
        config: CoordinatorConfig,
        transport: ChannelTransport,
        github: GitHubClient | None,
        directory: Path,
        *,
        scheduler_config: SchedulerConfig | None = None,
        cluster_config: ClusterConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delay_s: float = 2.0,
    ) -> None:
        self._config = config
        self._transport = transport
        self._github = github
        self._dir = directory
        self._scheduler_config = scheduler_config or SchedulerConfig()
        self._cluster_config = cluster_config
        self._sleep = sleep
        self._retry_delay_s = retry_delay_s
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._lock = threading.Lock()
        self._repos_seen: dict[str, float] = {}
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._stopped = threading.Event()
        self._started_at = 0.0
        self._last_sweep = 0.0
        self._last_purge = 0.0
        self.coordinator_id = ""
        self.public_key = ""
        self.signing_public = ""
        self.cluster: CoordinatorCluster | None = None
        self.jobs: SqliteJobStore | None = None
        self.logs: LogStore | None = None
        self.registry: Registry | None = None
        self.scheduler: Scheduler | None = None
        self.collector: LogCollector | None = None
        self.server: WebhookServer | None = None
        self.jit: JitRunners | None = None
        self.picker: Picker | None = None

    # lifecycle

    def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        key = load_or_create_private_key(self._dir / KEY_FILE)
        self.coordinator_id = node_id(key.public_key())
        self.public_key = encode_public_key(key.public_key())
        signing_key = load_or_create_signing_key(self._dir / SIGNING_KEY_FILE)
        self.signing_public = encode_signing_public(signing_key.public_key())
        self.jobs = SqliteJobStore(str(self._dir / JOBS_DB))
        self.logs = LogStore(str(self._dir / LOGS_DB))
        try:
            self._transport.start()
            self.registry = Registry(self._transport)
            self.registry.start()
            self.collector = LogCollector(self._transport, key, self.logs)
            self.collector.start()
            policy = TrustPolicy(
                allowed_repos=frozenset(self._config.allowed_repos),
                allowed_orgs=frozenset(self._config.allowed_orgs),
            )
            if self._github is not None:
                self._start_scheduling(key, signing_key, policy)
            pick = None
            if self._config.pick_enabled:
                self.picker = Picker(self.registry, allowed_labels=self._config.pick_labels)
                pick = PickService(
                    OidcVerifier(self._config.oidc_audience),
                    self.picker,
                    policy,
                ).handle
            receiver = WebhookReceiver(
                [s.get_secret_value().encode() for s in self._config.webhook_secrets],
                self._on_webhook,
                label_prefix=PICK_LABEL_PREFIX if self._config.pick_enabled else None,
            )
            self.server = WebhookServer(
                receiver,
                self._config.listen_host,
                self._config.listen_port,
                self._config.webhook_path,
                pick,
            )
            self.server.start()
        except BaseException:
            self.stop()
            raise
        self._started_at = time.time()
        self._spawn(self._maintenance, "octopus-maintenance")
        self._write_state()

    def _start_scheduling(self, key, signing_key, policy: TrustPolicy) -> None:
        """The JIT runner flow: needs a GitHub credential."""
        offers = OfferSender(self._transport, key, on_status=self._on_status)
        self.jit = JitRunners(self._github)
        if self._config.peers:
            self.cluster = CoordinatorCluster(
                self.coordinator_id,
                signing_key,
                {cid: decode_signing_public(pub) for cid, pub in self._config.peers.items()},
                self._transport,
                self._adopt,
                config=self._cluster_config,
                cancel=self._cancel,
            )
        self.scheduler = Scheduler(
            self.registry,
            self.jobs,
            offers,
            make_jit_provider(self.jit, self.jobs.record_runner),
            config=self._scheduler_config,
            policy=policy,
            logs=self.collector,
            events=self.cluster,
        )
        self.scheduler.start()
        if self.cluster is not None:
            self.cluster.start()
            self._spawn(self._cluster_ticker, "octopus-cluster")
        self._spawn(self._trust_worker, "octopus-trust")

    def wait(self) -> None:
        self._stopped.wait()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self.server is not None:
            self.server.stop()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(timeout=10)
        for part in (self.cluster, self.scheduler, self.collector, self.registry):
            if part is not None:
                try:
                    part.stop()
                except Exception:
                    log.exception("could not stop %s", type(part).__name__)
        try:
            self._transport.stop()
        except Exception:
            log.exception("could not stop the transport")
        for store in (self.jobs, self.logs):
            if store is not None:
                store.close()
        state_path(self._dir).unlink(missing_ok=True)
        self._stopped.set()

    def install_signal_handlers(self) -> None:
        """Stop on SIGINT/SIGTERM. Main thread only."""

        def _handler(signum, frame):
            threading.Thread(target=self.stop, name="octopus-stop", daemon=True).start()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    @property
    def webhook_address(self) -> tuple[str, int] | None:
        return self.server.address if self.server is not None else None

    # GitHub events

    def _on_webhook(self, event: WorkflowJobEvent) -> None:
        """Runs on the webhook request: must not block. Raising answers 500."""
        with self._lock:
            self._repos_seen[event.repo] = time.time()
        if self.picker is not None:
            self.picker.on_job_event(event)
        if self._github is None or not event.has_label(OCTOPUS_LABEL):
            return  # a pick-mode job: Octopus does not schedule it
        if event.action == "queued":
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                raise RuntimeError("trust queue full") from None
        elif event.action == "in_progress":
            self._note_runner(event)
        elif event.action == "completed":
            detail = f"GitHub completed the job before it ran: {event.conclusion}"
            if self.cluster is not None:
                # The job may be queued on another coordinator: the cluster tells its owner.
                self.cluster.cancel(str(event.job_id), detail)
            else:
                self._cancel(str(event.job_id), detail)

    def _cancel(self, job_id: str, detail: str) -> None:
        if self.scheduler is not None and self.scheduler.cancel(job_id, detail):
            log.info("job %s cancelled on GitHub while queued", job_id)

    def _note_runner(self, event: WorkflowJobEvent) -> None:
        """GitHub gives a job to any idle runner with its labels, not to the one Octopus made
        for it. When the runner that took `event.job_id` was made for another job, keep this
        job's records under that other job (see `JobStore.set_alias`)."""
        if self.jobs is None or not event.runner_name:
            return
        made_for = self.jobs.job_for_runner(event.runner_name)
        job_id = str(event.job_id)
        if made_for is None or made_for == job_id:
            return
        self.jobs.set_alias(job_id, made_for)
        log.info(
            "GitHub ran job %s on the runner made for job %s: records are kept under %s",
            job_id,
            made_for,
            made_for,
        )

    def _trust_worker(self) -> None:
        while True:
            event = self._queue.get()
            if event is _STOP:
                return
            try:
                self._submit(event)
            except Exception:
                log.exception("could not handle queued job %s", event.job_id)

    def _submit(self, event: WorkflowJobEvent) -> None:
        job_id = str(event.job_id)
        error = ""
        for attempt in range(TRUST_ATTEMPTS):
            try:
                context = resolve_trust_inputs(event, self._github).job_context()
                break
            except (GitHubApiError, httpx.HTTPError) as e:
                error = str(e)
                log.warning("trust check of job %s failed: %s", job_id, error)
                if attempt + 1 < TRUST_ATTEMPTS and not self._stopping.is_set():
                    self._sleep(self._retry_delay_s * 2**attempt)
        else:
            # Fail closed, and say why in the timeline.
            assert self.jobs is not None
            try:
                self.jobs.create(job_id, event.repo, actor=ACTOR)
            except ValueError:
                return  # a repeat of a job already known
            self.jobs.transition(
                job_id,
                State.FAILED,
                actor=ACTOR,
                reason=FailureReason.POLICY_REJECTED,
                detail=f"could not check trust: {error}",
            )
            return
        if self.cluster is not None:
            # The owner among the live coordinators adopts it (maybe this one).
            self.cluster.submit(to_coord_job(job_id, event.repo, list(event.labels), context))
            return
        assert self.scheduler is not None
        self.scheduler.submit(job_id, event.repo, list(event.labels), context)

    def _adopt(self, job) -> None:
        """The cluster says this coordinator owns the job: queue it in the scheduler."""
        assert self.scheduler is not None
        self.scheduler.submit(job.job_id, job.repo, list(job.labels), to_job_context(job))

    def _cluster_ticker(self) -> None:
        while not self._stopping.wait(CLUSTER_TICK_S):
            try:
                assert self.cluster is not None
                self.cluster.tick()
            except Exception:
                log.exception("cluster tick failed")

    def _on_status(self, status) -> None:
        if self.scheduler is not None:
            self.scheduler.on_status(status)

    # upkeep

    def _maintenance(self) -> None:
        while not self._stopping.wait(MAINTENANCE_S):
            try:
                self.maintain()
            except Exception:
                log.exception("coordinator maintenance failed")

    def maintain(self, now: float | None = None) -> None:
        """Refresh the state file; sweep orphaned runners and purge old data when due."""
        now = time.time() if now is None else now
        self._write_state()
        if now - self._last_sweep >= SWEEP_EVERY_S:
            self._last_sweep = now
            with self._lock:
                repos = [r for r, t in self._repos_seen.items() if now - t < SWEEP_WINDOW_S]
            for repo in repos:
                try:
                    removed = self.jit.sweep_orphans(repo) if self.jit else []
                except (JitRunnerError, GitHubApiError, httpx.HTTPError) as e:
                    log.warning("could not sweep runners of %s: %s", repo, e)
                    continue
                if removed:
                    log.info("removed %d orphaned runner(s) from %s", len(removed), repo)
        if now - self._last_purge >= PURGE_EVERY_S:
            self._last_purge = now
            from .jobs.sqlite import DEFAULT_RETENTION_S
            from .logs import DEFAULT_RETENTION_S as LOG_RETENTION_S

            if self.jobs is not None:
                self.jobs.purge(DEFAULT_RETENTION_S)
            if self.logs is not None:
                self.logs.purge(LOG_RETENTION_S)

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def state(self) -> dict:
        address = self.webhook_address
        multiaddrs: list[str] = []
        try:
            multiaddrs = list(getattr(self._transport, "multiaddrs", list)())
        except RuntimeError as e:  # the node may not be up yet
            log.debug("no multiaddrs yet: %s", e)
        return {
            "pid": os.getpid(),
            "coordinator_id": self.coordinator_id,
            "public_key": self.public_key,
            "cluster_key": self.signing_public,
            "peers": sorted(self._config.peers),
            "webhook": f"http://{address[0]}:{address[1]}{self._config.webhook_path}"
            if address
            else "",
            "webhook_url": self._config.webhook_url,
            "multiaddrs": multiaddrs,
            "auth": self._config.auth_mode,
            "allowed_repos": self._config.allowed_repos,
            "allowed_orgs": self._config.allowed_orgs,
            "runners_online": len(self.registry.online()) if self.registry else 0,
            "started_at": self._started_at,
            "updated_at": time.time(),
        }

    def _write_state(self) -> None:
        path = state_path(self._dir)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state(), f)
        os.replace(tmp, path)
