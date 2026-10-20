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
    POLICY_REJECTED = "policy-rejected"
    RUNNER_LOST = "runner-lost"
    JOB_FAILED = "job-failed"


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

    @field_serializer("jit_config", when_used="json")
    def _reveal_jit_config(self, value: SecretStr) -> str:
        return value.get_secret_value()


class JobAck(BaseModel):
    kind: Literal["job-ack"] = "job-ack"
    job_id: str
    runner_id: str
    accepted: bool
    reason: FailureReason | None = None


class JobStatus(BaseModel):
    kind: Literal["job-status"] = "job-status"
    job_id: str
    runner_id: str
    state: Literal["runner-started", "step", "finished"]
    step: str | None = None
    exit_code: int | None = None
    reason: FailureReason | None = None


class LogChunk(BaseModel):
    kind: Literal["log-chunk"] = "log-chunk"
    job_id: str
    seq: int
    data: str
    final: bool = False
