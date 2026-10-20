from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_serializer

TOPIC_PREFIX = "/octopus/1"
PRESENCE_TOPIC = f"{TOPIC_PREFIX}/presence/proto"
COORD_TOPIC = f"{TOPIC_PREFIX}/coord/proto"


def jobs_topic(runner_id: str) -> str:
    return f"{TOPIC_PREFIX}/jobs-{runner_id}/proto"


def logs_topic(job_id: str) -> str:
    return f"{TOPIC_PREFIX}/logs-{job_id}/proto"


class FailureReason(str, Enum):
    NO_IDLE_RUNNER = "no-idle-runner"
    OFFER_TIMEOUT = "offer-timeout"
    JITCONFIG_ERROR = "jitconfig-error"
    RUNNER_DOWNLOAD_FAILED = "runner-download-failed"
    RUNNER_START_FAILED = "runner-start-failed"
    POLICY_REJECTED = "policy-rejected"
    RUNNER_LOST = "runner-lost"
    JOB_FAILED = "job-failed"
    # GitHub cancelled the job (e.g. the workflow run) before a runner took it.
    CANCELLED = "cancelled"


class Presence(BaseModel):
    kind: Literal["presence"] = "presence"
    runner_id: str
    os: str
    arch: str
    labels: list[str] = Field(default_factory=list)
    capacity: int = 1
    busy: int = 0
    sent_at: float
    # Sent once on graceful shutdown so coordinators drop the runner immediately.
    offline: bool = False
    # Runner's X25519 public key; `runner_id` is derived from it.
    pubkey: str | None = None


class JobOffer(BaseModel):
    kind: Literal["job-offer"] = "job-offer"
    job_id: str
    repo: str
    runner_id: str = ""
    coordinator_id: str = ""
    # Travels only inside the encrypted channel to this runner; never logged.
    jit_config: SecretStr
    labels: list[str] = Field(default_factory=list)
    offer_ttl_s: float = 30
    expires_at: float = 0.0
    nonce: str = ""
    # Trust-policy facts, supplied by the coordinator over the authenticated channel.
    # The defaults fail closed: the runner rejects an offer that does not state them.
    head_repo: str = ""
    event: str = ""
    sender_is_org_member: bool = False

    @field_serializer("jit_config", when_used="json")
    def _reveal_jit_config(self, value: SecretStr) -> str:
        return value.get_secret_value()


class JobAck(BaseModel):
    kind: Literal["job-ack"] = "job-ack"
    job_id: str
    runner_id: str
    accepted: bool
    reason: FailureReason | None = None
    # Human-readable why, shown by `octopus explain`; never contains secrets.
    detail: str | None = None


class JobStatus(BaseModel):
    kind: Literal["job-status"] = "job-status"
    job_id: str
    runner_id: str
    state: Literal["runner-started", "step", "finished"]
    step: str | None = None
    exit_code: int | None = None
    reason: FailureReason | None = None


# A chunk's text is at most this many UTF-8 bytes, so even fully JSON-escaped it stays well
# under the transport's message size limit.
MAX_LOG_CHUNK_BYTES = 16 * 1024


class LogChunk(BaseModel):
    """Runner to coordinator, on the job's log channel. `seq` counts from 0 without gaps;
    `final` marks the last chunk. Secrets are masked before a chunk is made."""

    kind: Literal["log-chunk"] = "log-chunk"
    job_id: str
    seq: int
    data: str
    final: bool = False


class LogAck(BaseModel):
    """Coordinator to runner: every chunk up to `seq` is stored (-1: none yet). The runner
    resends from `seq + 1`. `complete` once the final chunk and everything before it is in."""

    kind: Literal["log-ack"] = "log-ack"
    job_id: str
    seq: int
    complete: bool = False
