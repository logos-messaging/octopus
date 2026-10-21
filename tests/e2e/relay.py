"""Relays the deliveries of a repo webhook from the GitHub API to a local coordinator.

A CI machine has no public address, so GitHub cannot send webhooks to a coordinator on it.
The e2e webhook points at a URL that does not resolve. GitHub still records each delivery
with its headers and payload, and the deliveries API returns them. The relay sends each new
`workflow_job` delivery to the local webhook endpoint.

GitHub signs the compact JSON form of the payload. The relay makes that form again, so the
coordinator gets the signature that GitHub made. If the signature does not agree with the
body, the relay signs the body with the webhook secret and counts it in `resigned`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from octopus.github.app import GitHubApiError, repo_path
from octopus.github.token import TokenClient
from octopus.github.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    sign,
    verify_signature,
)

log = logging.getLogger(__name__)

# The headers the coordinator reads. The relay does not send the other headers.
FORWARDED_HEADERS = ("content-type", "user-agent", EVENT_HEADER, DELIVERY_HEADER, SIGNATURE_HEADER)


@dataclass(frozen=True)
class Relayed:
    delivery_id: str  # the X-GitHub-Delivery GUID
    action: str
    job_id: int
    status: int  # what the coordinator answered


def compact_body(payload: Any) -> bytes:
    """The bytes that GitHub sends for a payload."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


class DeliveryRelay:
    """Polls the deliveries of one hook and posts the new `workflow_job` ones to `target`."""

    def __init__(
        self,
        github: TokenClient,
        repo: str,
        hook_id: int,
        target: str,
        secret: bytes,
        *,
        poll_s: float = 2.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._github = github
        self._path = f"{repo_path(repo)}/hooks/{hook_id}/deliveries"
        self._target = target
        self._secret = secret
        self._poll_s = poll_s
        self._http = http or httpx.Client(timeout=10.0)
        self._seen: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.relayed: list[Relayed] = []
        self.resigned = 0

    def skip_existing(self) -> None:
        """Mark the deliveries that GitHub has now as done. The relay sends only later ones."""
        self._seen.update(d["id"] for d in self._list())

    def poll_once(self) -> int:
        """Send the new deliveries, the oldest first. Returns how many the relay sent."""
        sent = 0
        for summary in sorted(self._list(), key=lambda d: d["id"]):
            if summary["id"] in self._seen:
                continue
            if summary.get("event") != "workflow_job":
                self._seen.add(summary["id"])
                continue
            detail = self._github.get_json(f"{self._path}/{summary['id']}")
            status = self._forward(detail)
            if status >= 500:
                # The coordinator could not process it. The next poll tries again.
                log.warning("relay: coordinator answered %d: delivery: %s", status, summary["id"])
                continue
            self._seen.add(summary["id"])
            sent += 1
        return sent

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="e2e-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._http.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except (httpx.HTTPError, GitHubApiError) as e:
                log.warning("relay: poll failed: %s", e)
            self._stop.wait(self._poll_s)

    def _list(self) -> list[dict[str, Any]]:
        resp = self._github.request("GET", self._path, params={"per_page": 100})
        if resp.status_code != 200:
            raise GitHubApiError("GET", self._path, resp.status_code, resp.text[:200])
        return list(resp.json())

    def _forward(self, detail: dict[str, Any]) -> int:
        request = detail["request"]
        payload = request["payload"]
        given = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
        headers = {k: given[k] for k in FORWARDED_HEADERS if k in given}
        body = compact_body(payload)
        if not verify_signature([self._secret], body, headers.get(SIGNATURE_HEADER)):
            self.resigned += 1
            headers[SIGNATURE_HEADER] = sign(self._secret, body)
        try:
            status = self._http.post(self._target, content=body, headers=headers).status_code
        except httpx.HTTPError as e:
            log.warning("relay: could not reach the coordinator: %s", e)
            return 599
        job = payload.get("workflow_job") or {}
        self.relayed.append(
            Relayed(
                headers.get(DELIVERY_HEADER, ""),
                str(payload.get("action")),
                int(job.get("id") or 0),
                status,
            )
        )
        return status
