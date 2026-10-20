"""In-process Octopus cluster: coordinators and runners wired on one MemoryBus.

Real code throughout (scheduler, registry, job channel crypto, offers, trust policy, job
store, log streaming, webhook receiver, RunnerAgent); only GitHub and the `actions/runner`
process are faked. The coordinator is driven by hand (`cluster.tick()`), so tests are
deterministic; runners run their jobs on the threads RunnerAgent already uses.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from conftest import SECRET, octopus_job
from pydantic import SecretStr

from octopus.agent import Allowlist
from octopus.agent.config import RunnerConfig
from octopus.agent.logs import LogStreamer
from octopus.agent.runner import RunResult
from octopus.agent.runner_agent import KEY_FILE, RunnerAgent
from octopus.coordinator import (
    JitConfigError,
    LogCollector,
    LogStore,
    OfferSender,
    Registry,
    Scheduler,
    SchedulerConfig,
)
from octopus.coordinator.jobs import Job, SqliteJobStore, State
from octopus.crypto import encode_public_key, generate_private_key, load_or_create_private_key
from octopus.github.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    DeliveryLog,
    WebhookReceiver,
    WebhookResponse,
    WorkflowJobEvent,
    sign,
)
from octopus.policy import JobContext, TrustPolicy
from octopus.proto import FailureReason
from octopus.transport import MemoryBus, MemoryTransport

REPO = "acme/app"


class FakeClock:
    """Real time plus a manual offset, because runners check offer expiry on the real clock."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def now(self) -> float:
        return time.time() + self.offset

    def advance(self, s: float) -> None:
        self.offset += s


def wait_until(predicate, timeout_s: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for: {what}")


class LossyTransport(MemoryTransport):
    """Drops, duplicates and delays this node's outgoing channel sends (seeded)."""

    def __init__(self, bus, *, seed=1, drop=0.0, dup=0.0, delay=0.0):
        super().__init__(bus)
        self.rng = random.Random(seed)
        self.drop, self.dup, self.delay = drop, dup, delay
        self.mute = False  # lose everything, as when the link is down
        self.held: list[tuple[str, bytes]] = []

    def send_on_channel(self, channel_id, payload, on_result=None):
        if self.mute:
            return
        r = self.rng.random()
        if r < self.drop:
            return
        if r < self.drop + self.delay:
            self.held.append((channel_id, payload))
            return
        super().send_on_channel(channel_id, payload, on_result)
        if self.rng.random() < self.dup:
            super().send_on_channel(channel_id, payload, on_result)

    def flush_held(self) -> None:
        held, self.held = self.held, []
        for channel_id, payload in held:
            super().send_on_channel(channel_id, payload)


@dataclass
class Behavior:
    """What a runner's fake `actions/runner` does with a job."""

    outcome: str = "ok"  # "ok" | "fail" | "hang" (until `release()`)
    log_lines: tuple[str, ...] = ()
    stream_logs: bool = False
    log_transport_drop: float = 0.0
    gate: threading.Event = field(default_factory=threading.Event)


class FakeManager:
    """Stands in for RunnerManager: same `run_job`/`stop` shape, no process."""

    def __init__(self, node: RunnerNode) -> None:
        self._node = node
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._node.behavior.gate.set()

    def run_job(self, job_id, jit_config, on_output=None) -> RunResult:
        node, b = self._node, self._node.behavior
        node.runs.append(job_id)
        streamer = self._streamer(job_id) if b.stream_logs else None
        try:
            for line in b.log_lines:
                if streamer is not None:
                    streamer.write(line)
            if b.outcome == "hang":
                b.gate.wait(30)
            if self._stop.is_set():
                return RunResult(None, FailureReason.RUNNER_LOST, "stopped", 0.0)
            if b.outcome == "fail":
                return RunResult(1, FailureReason.JOB_FAILED, "exit code 1", 0.0)
            return RunResult(0, None, "", 0.0)
        finally:
            if streamer is not None:
                streamer.close(timeout_s=10)

    def _streamer(self, job_id: str) -> LogStreamer:
        node = self._node
        coordinator = node.cluster.coordinators[0]
        key = load_or_create_private_key(node.data_dir / KEY_FILE)
        transport = LossyTransport(
            node.cluster.bus, seed=7, drop=node.behavior.log_transport_drop
        )
        transport.start()
        streamer = LogStreamer(
            transport,
            key,
            coordinator.sender.coordinator_id,
            coordinator.sender.public_key,
            job_id,
            node.data_dir / "spool",
            resend_after_s=0.02,
            flush_interval_s=0.01,
        )
        streamer.start()
        return streamer


class RunnerNode:
    def __init__(self, cluster: Cluster, name: str, behavior: Behavior, allow, transport) -> None:
        self.cluster = cluster
        self.name = name
        self.behavior = behavior
        self.runs: list[str] = []
        self.data_dir = cluster.root / name
        allowlist = Allowlist()
        for target in allow:
            allowlist.add(target)
        config = RunnerConfig(
            coordinators=[c.sender.public_key for c in cluster.coordinators], max_jobs=1
        )
        self.transport = transport
        self.agent = RunnerAgent(
            config,
            allowlist,
            transport,
            data_dir=self.data_dir,
            manager_factory=lambda: FakeManager(self),
        )
        self.agent.start()

    @property
    def runner_id(self) -> str:
        return self.agent.runner_id

    def release(self) -> None:
        self.behavior.gate.set()


class FakeGitHub:
    """JIT config source; records what the coordinator asked GitHub for."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.fail_with: str | None = None

    def provide(self, job, runner) -> SecretStr:
        self.requests.append(job.job_id)
        if self.fail_with:
            raise JitConfigError(self.fail_with)
        return SecretStr(f"jit-{job.job_id}-attempt-{job.attempt}")


class CoordinatorNode:
    def __init__(self, cluster: Cluster, policy, config: SchedulerConfig, store_path: str) -> None:
        bus, clock = cluster.bus, cluster.clock
        self.key = generate_private_key()
        self.transport = MemoryTransport(bus)
        self.store = SqliteJobStore(store_path)
        self.log_store = LogStore(":memory:")
        self.github = FakeGitHub()
        self.registry = Registry(self.transport, clock)
        self.registry.start()
        self.sender = OfferSender(
            self.transport, self.key, clock, on_status=lambda s: self.scheduler.on_status(s)
        )
        self.logs = LogCollector(self.transport, self.key, self.log_store, clock=clock)
        self.scheduler = Scheduler(
            self.registry,
            self.store,
            self.sender,
            self.github.provide,
            clock,
            config,
            policy=policy,
            logs=self.logs,
        )
        self.contexts: dict[int, JobContext] = {}
        self.webhooks = WebhookReceiver([SECRET], self._on_event, deliveries=DeliveryLog())
        self.public_key = encode_public_key(self.key.public_key())

    def _on_event(self, ev: WorkflowJobEvent) -> None:
        if ev.action != "queued":
            return
        context = self.contexts.get(ev.job_id) or JobContext(
            repo=ev.repo, head_repo=ev.repo, sender_is_org_member=True, event="push"
        )
        self.scheduler.submit(str(ev.job_id), ev.repo, ev.labels, context)

    def deliver(self, job_id: int, *, delivery_id: str | None = None, **ctx) -> WebhookResponse:
        """POST a signed `workflow_job.queued` for `job_id`; `ctx` overrides its trust context."""
        if ctx:
            self.contexts[job_id] = JobContext(
                **{
                    "repo": REPO,
                    "head_repo": REPO,
                    "sender_is_org_member": True,
                    "event": "push",
                    **ctx,
                }
            )
        payload = octopus_job(id=job_id)
        payload["repository"]["full_name"] = REPO
        payload["repository"]["name"] = "app"
        payload["repository"]["owner"]["login"] = "acme"
        body = json.dumps(payload).encode()
        headers = {
            SIGNATURE_HEADER: sign(SECRET, body),
            DELIVERY_HEADER: delivery_id or f"delivery-{job_id}",
            EVENT_HEADER: "workflow_job",
        }
        return self.webhooks.handle(headers, body)

    def job(self, job_id: int | str) -> Job:
        job = self.store.get(str(job_id))
        assert job is not None, f"no such job: {job_id}"
        return job

    def states(self, job_id: int | str) -> list[State]:
        return [e.state for e in self.store.timeline(str(job_id))]


class Cluster:
    def __init__(
        self,
        root: Path,
        *,
        coordinators: int = 1,
        policy: TrustPolicy | None = None,
        scheduler: SchedulerConfig | None = None,
    ) -> None:
        self.root = root
        self.bus = MemoryBus()
        self.clock = FakeClock()
        policy = policy if policy is not None else TrustPolicy(allowed_orgs=frozenset({"acme"}))
        config = scheduler or SchedulerConfig(offer_ttl_s=2.0)
        self.coordinators = [
            CoordinatorNode(self, policy, config, ":memory:") for _ in range(coordinators)
        ]
        self.runners: list[RunnerNode] = []

    @property
    def coordinator(self) -> CoordinatorNode:
        return self.coordinators[0]

    def add_runner(
        self, name: str, behavior: Behavior | None = None, *, allow=("acme",), transport=None
    ) -> RunnerNode:
        node = RunnerNode(
            self,
            name,
            behavior or Behavior(),
            allow,
            transport or MemoryTransport(self.bus),
        )
        self.runners.append(node)
        return node

    def tick(self, coordinator: int = 0) -> None:
        self.coordinators[coordinator].scheduler.tick()

    def close(self) -> None:
        for r in self.runners:
            r.release()
        for r in self.runners:
            r.agent.stop()
        for c in self.coordinators:
            c.logs.stop()
