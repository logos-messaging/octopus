"""`octopus coordinator start` as a cluster: two real coordinators over one MemoryBus.

The cluster logic itself is in test_cluster.py; this checks that the coordinator process
wires it in (peers from config, webhook -> cluster -> owner's scheduler, failover).
"""

import sqlite3

import httpx
import pytest
from test_coordinator_service import (
    REPO,
    SECRET,
    FakeGitHub,
    FakeRunner,
    wait_for,
)

from octopus.agent.config import RunnerConfig
from octopus.agent.runner_agent import RunnerAgent
from octopus.agent.trust import Allowlist
from octopus.coordinator import ClusterConfig, SchedulerConfig
from octopus.coordinator.config import (
    PEERS_ENV,
    ConfigError,
    CoordinatorConfig,
    parse_peers,
)
from octopus.coordinator.jobs import State
from octopus.coordinator.service import Coordinator
from octopus.crypto.signing import encode_signing_public, generate_signing_key
from octopus.github.token import TokenClient
from octopus.transport import MemoryBus, MemoryTransport

FAST = ClusterConfig(heartbeat_s=0.05, dead_after_s=0.4, max_skew_s=60.0)


def env(peers=""):
    values = {
        "OCTOPUS_WEBHOOK_SECRET": SECRET,
        "OCTOPUS_GITHUB_TOKEN": "github_pat_FAKE",
        "OCTOPUS_GITHUB_REPO": REPO,
        "OCTOPUS_PUBLIC_IP": "203.0.113.7",
        "OCTOPUS_WEBHOOK_LISTEN": "127.0.0.1:0",
    }
    if peers:
        values[PEERS_ENV] = peers
    return values


def test_parse_peers():
    key = encode_signing_public(generate_signing_key().public_key())
    assert parse_peers(f"abc={key}, def={key}") == {"abc": key, "def": key}
    assert parse_peers("") == {}
    assert parse_peers(None) == {}


@pytest.mark.parametrize("bad", ["nokey", "abc=", "=key", "abc=not-a-key"])
def test_parse_peers_rejects_malformed(bad):
    with pytest.raises(ConfigError, match=PEERS_ENV):
        parse_peers(bad)


def test_config_reads_peers():
    key = encode_signing_public(generate_signing_key().public_key())
    assert CoordinatorConfig.from_env(env(f"abc={key}")).peers == {"abc": key}
    assert CoordinatorConfig.from_env(env()).peers == {}


class Cluster:
    """Two coordinators that trust each other, one runner that trusts both."""

    def __init__(self, tmp_path):
        self.bus = MemoryBus()
        self.github = FakeGitHub()
        self.nodes = {}
        # Each coordinator creates its own keys on start, so start them once to learn the
        # public keys, then restart with the peers configured.
        probes = [self._build(tmp_path, name, "") for name in "ab"]
        keys = {}
        for node in probes:
            node.start()
            keys[node.coordinator_id] = node.signing_public
            node.stop()
        self.coordinators = []
        for node, name in zip(probes, "ab", strict=True):
            peers = ",".join(f"{c}={k}" for c, k in keys.items() if c != node.coordinator_id)
            fresh = self._build(tmp_path, name, peers)
            fresh.start()
            self.coordinators.append(fresh)
        self.a, self.b = self.coordinators
        allowlist = Allowlist()
        allowlist.add(REPO)
        self.agent = RunnerAgent(
            RunnerConfig(coordinators=[c.public_key for c in self.coordinators]),
            allowlist,
            MemoryTransport(self.bus),
            data_dir=tmp_path / "runner",
            manager_factory=lambda: FakeRunner(),
            log_drain_s=5,
        )
        self.agent.start()
        assert wait_for(lambda: len(self.a.registry.online()) == 1)
        assert wait_for(lambda: self.a.cluster.live() == self.b.cluster.live())
        assert wait_for(lambda: len(self.a.cluster.live()) == 2)

    def _build(self, tmp_path, name, peers):
        client = TokenClient(
            "github_pat_FAKE",
            client=httpx.Client(transport=httpx.MockTransport(self.github.handler)),
        )
        return Coordinator(
            CoordinatorConfig.from_env(env(peers)),
            MemoryTransport(self.bus),
            client,
            tmp_path / "data" / name,
            scheduler_config=SchedulerConfig(tick_s=0.02, offer_ttl_s=2.0),
            cluster_config=FAST,
            retry_delay_s=0.0,
        )

    def deliver(self, coordinator, job_id):
        import json

        from conftest import octopus_job
        from test_coordinator_service import RUN_ID

        from octopus.github.webhooks import sign

        payload = octopus_job()
        payload["action"] = "queued"
        payload["workflow_job"].update(id=job_id, run_id=RUN_ID)
        payload["repository"]["full_name"] = REPO
        payload["sender"] = {"login": "alice", "type": "User"}
        body = json.dumps(payload).encode()
        host, port = coordinator.webhook_address
        return httpx.post(
            f"http://{host}:{port}/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": f"d-{job_id}",
                "X-Hub-Signature-256": sign(SECRET.encode(), body),
            },
        )

    def finished_on(self, job_id):
        ids = []
        for c in self.coordinators:
            try:
                job = c.jobs.get(str(job_id)) if c.jobs is not None else None
            except sqlite3.ProgrammingError:  # a stopped coordinator closed its database
                continue
            if job is not None and job.state is State.FINISHED:
                ids.append(c.coordinator_id)
        return ids

    def stop(self):
        self.agent.stop()
        for c in self.coordinators:
            c.stop()


@pytest.fixture
def cluster(tmp_path):
    c = Cluster(tmp_path)
    yield c
    c.stop()


def test_job_received_by_either_coordinator_runs_once(cluster):
    for job_id, receiver in ((2001, cluster.a), (2002, cluster.b)):
        assert cluster.deliver(receiver, job_id).status_code == 202
        assert wait_for(lambda j=job_id: len(cluster.finished_on(j)) >= 1)
        # Exactly one coordinator ran it, whichever received the webhook.
        assert len(cluster.finished_on(job_id)) == 1


def test_job_survives_the_other_coordinator_dying(cluster):
    cluster.b.stop()
    assert wait_for(lambda: cluster.a.cluster.live() == [cluster.a.coordinator_id])
    assert cluster.deliver(cluster.a, 2003).status_code == 202
    assert wait_for(lambda: cluster.finished_on(2003) == [cluster.a.coordinator_id])


def test_status_shows_cluster_key_and_peers(cluster):
    state = cluster.a.state()
    assert state["cluster_key"] == cluster.a.signing_public
    assert state["peers"] == [cluster.b.coordinator_id]
