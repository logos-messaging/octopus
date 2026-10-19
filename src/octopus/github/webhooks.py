"""GitHub webhook receiver: signature verification, idempotency and `workflow_job` parsing.

Transport-agnostic: `WebhookReceiver.handle(headers, body)` takes the raw request and returns
the response to send. `octopus.coordinator.webhook_server` serves it over HTTP.

Nothing here calls the GitHub API, so a delivery is answered well within GitHub's 10 s limit.
Trust-policy inputs that need the API are resolved later by `octopus.github.trust`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-hub-signature-256"
EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"

OCTOPUS_LABEL = "octopus"
# workflow_job payloads are a few KB; GitHub's own cap is 25 MB.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
# A delivery can be redelivered by hand for up to 3 days, with the same delivery id.
DEFAULT_DELIVERY_TTL_S = 3 * 24 * 3600

JobAction = Literal["queued", "in_progress", "completed"]
HANDLED_ACTIONS: frozenset[str] = frozenset({"queued", "in_progress", "completed"})


class PayloadError(ValueError):
    """The payload is not a well-formed `workflow_job` event."""


def sign(secret: bytes, body: bytes) -> str:
    """The `X-Hub-Signature-256` value GitHub sends for `body`."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secrets: Iterable[bytes], body: bytes, header: str | None) -> bool:
    """Constant-time check of `X-Hub-Signature-256` against any of `secrets`.

    Several secrets are accepted so the webhook secret can be rotated without downtime.
    """
    if not header or not header.startswith("sha256="):
        return False
    given = header.encode("ascii", "replace")
    ok = False
    for secret in secrets:
        # No early exit: the time taken does not depend on which secret matched.
        ok |= hmac.compare_digest(sign(secret, body).encode(), given)
    return ok


class DeliveryLog:
    """Remembers `X-GitHub-Delivery` ids so each delivery is processed once.

    In memory, bounded by `max_entries` and `ttl_s`. A restart forgets it, so consumers
    must still be idempotent per job id (the job lifecycle store rejects illegal transitions).
    """

    def __init__(
        self,
        ttl_s: float = DEFAULT_DELIVERY_TTL_S,
        max_entries: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max = max_entries
        self._clock = clock
        self._lock = Lock()
        # delivery id -> time claimed; insertion order is age order
        self._seen: OrderedDict[str, float] = OrderedDict()

    def claim(self, delivery_id: str) -> bool:
        """True if this is the first time `delivery_id` is seen (and it is now taken)."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = now
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def release(self, delivery_id: str) -> None:
        """Forget a claim whose processing failed, so a redelivery is processed again."""
        with self._lock:
            self._seen.pop(delivery_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def _expire(self, now: float) -> None:
        while self._seen:
            oldest = next(iter(self._seen.values()))
            if now - oldest < self._ttl_s:
                break
            self._seen.popitem(last=False)


@dataclass(frozen=True)
class WorkflowJobEvent:
    """A `workflow_job` webhook, reduced to what Octopus uses."""

    delivery_id: str
    action: JobAction
    job_id: int
    run_id: int
    run_attempt: int
    job_name: str
    workflow_name: str | None
    labels: tuple[str, ...]
    repo: str  # "owner/name"
    owner: str
    org: str | None  # None for repos owned by a user account
    installation_id: int | None
    sender: str
    sender_type: str  # "User", "Bot", ...
    head_sha: str
    head_branch: str | None
    html_url: str | None
    runner_name: str | None  # set from in_progress on
    conclusion: str | None  # set on completed
    failed_step: str | None  # first failed step, on completed

    def has_label(self, label: str) -> bool:
        # Runner labels match case-insensitively on GitHub.
        want = label.casefold()
        return any(lbl.casefold() == want for lbl in self.labels)


def parse_workflow_job(payload: Mapping[str, Any], delivery_id: str) -> WorkflowJobEvent:
    """Parse a `workflow_job` payload. Raises `PayloadError` if a required field is missing."""
    try:
        action = payload["action"]
        job = payload["workflow_job"]
        repo = payload["repository"]
        sender = payload["sender"]
        if action not in HANDLED_ACTIONS:
            raise PayloadError(f"unhandled workflow_job action: {action!r}")
        steps = job.get("steps") or []
        failed_step = next((s.get("name") for s in steps if s.get("conclusion") == "failure"), None)
        full_name = str(repo["full_name"])
        owner, sep, name = full_name.partition("/")
        if not (owner and sep and name):
            raise PayloadError(f"bad repository full_name: {full_name!r}")
        org = payload.get("organization")
        installation = payload.get("installation")
        return WorkflowJobEvent(
            delivery_id=delivery_id,
            action=action,
            job_id=int(job["id"]),
            run_id=int(job["run_id"]),
            run_attempt=int(job.get("run_attempt") or 1),
            job_name=str(job["name"]),
            workflow_name=job.get("workflow_name"),
            labels=tuple(str(lbl) for lbl in job.get("labels") or ()),
            repo=full_name,
            owner=owner,
            org=str(org["login"]) if org else None,
            installation_id=int(installation["id"]) if installation else None,
            sender=str(sender["login"]),
            sender_type=str(sender.get("type") or "User"),
            head_sha=str(job["head_sha"]),
            head_branch=job.get("head_branch"),
            html_url=job.get("html_url"),
            runner_name=job.get("runner_name") if action != "queued" else None,
            conclusion=job.get("conclusion"),
            failed_step=failed_step,
        )
    except PayloadError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise PayloadError(f"malformed workflow_job payload: {e!r}") from e


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    message: str


EventSink = Callable[[WorkflowJobEvent], None]


class WebhookReceiver:
    """Verifies, de-duplicates and parses GitHub deliveries, then hands Octopus jobs to `sink`.

    `sink` runs on the request thread and must not block (enqueue and return). If it raises,
    the delivery answers 500 and is released, so GitHub's redelivery is processed again.
    """

    def __init__(
        self,
        secrets: Iterable[bytes],
        sink: EventSink,
        *,
        label: str = OCTOPUS_LABEL,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        deliveries: DeliveryLog | None = None,
    ) -> None:
        self._secrets = tuple(secrets)
        if not self._secrets or not all(self._secrets):
            raise ValueError("a non-empty webhook secret is required")
        self._sink = sink
        self._label = label
        self.max_body_bytes = max_body_bytes
        self._deliveries = deliveries if deliveries is not None else DeliveryLog()

    def handle(self, headers: Mapping[str, str], body: bytes) -> WebhookResponse:
        h = {k.lower(): v for k, v in headers.items()}
        if len(body) > self.max_body_bytes:
            return WebhookResponse(413, "payload too large")
        # Authenticate before looking at anything else in the request.
        if not verify_signature(self._secrets, body, h.get(SIGNATURE_HEADER)):
            log.warning("rejected delivery %s: bad signature", h.get(DELIVERY_HEADER, "?"))
            return WebhookResponse(401, "invalid signature")
        delivery_id = h.get(DELIVERY_HEADER, "").strip()
        if not delivery_id:
            return WebhookResponse(400, "missing X-GitHub-Delivery")
        event = h.get(EVENT_HEADER, "")
        if event == "ping":
            return WebhookResponse(200, "pong")
        if event != "workflow_job":
            return WebhookResponse(202, f"ignored event: {event or '?'}")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookResponse(400, "invalid JSON")
        if not isinstance(payload, dict):
            return WebhookResponse(400, "invalid JSON")
        if payload.get("action") not in HANDLED_ACTIONS:
            return WebhookResponse(202, f"ignored action: {payload.get('action')}")
        try:
            job = parse_workflow_job(payload, delivery_id)
        except PayloadError as e:
            log.warning("rejected delivery %s: %s", delivery_id, e)
            return WebhookResponse(400, "malformed workflow_job payload")
        if not job.has_label(self._label):
            return WebhookResponse(202, f"ignored: job has no {self._label!r} label")
        if not self._deliveries.claim(delivery_id):
            log.info("duplicate delivery %s ignored", delivery_id)
            return WebhookResponse(200, "duplicate delivery")
        try:
            self._sink(job)
        except Exception:
            self._deliveries.release(delivery_id)
            log.exception("processing delivery %s failed", delivery_id)
            return WebhookResponse(500, "processing failed")
        log.info("delivery %s: job %d %s (%s)", delivery_id, job.job_id, job.action, job.repo)
        return WebhookResponse(202, "accepted")
