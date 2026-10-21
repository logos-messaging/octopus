"""The e2e delivery relay, against a fake deliveries API and a real WebhookReceiver."""

from __future__ import annotations

import httpx
from conftest import SECRET, octopus_job
from e2e.relay import DeliveryRelay, compact_body

from octopus.github.token import TokenClient
from octopus.github.webhooks import WebhookReceiver, sign

HOOK = 7
TARGET = "http://127.0.0.1:9/github/webhook"
BASE = f"/repos/acme/app/hooks/{HOOK}/deliveries"


class FakeHook:
    """The deliveries API of one hook. GitHub lists the newest first."""

    def __init__(self) -> None:
        self.deliveries: dict[int, dict] = {}
        self.list_status = 200

    def add(
        self, delivery_id: int, payload: dict, event: str = "workflow_job", sig: str = ""
    ) -> None:
        body = compact_body(payload)
        self.deliveries[delivery_id] = {
            "id": delivery_id,
            "event": event,
            "request": {
                "headers": {
                    "Content-Type": "application/json",
                    "X-Github-Event": event,
                    "X-Github-Delivery": f"guid-{delivery_id}",
                    "X-Hub-Signature-256": sig or sign(SECRET, body),
                    "X-Github-Hook-Id": str(HOOK),
                },
                "payload": payload,
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == BASE:
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"message": "nope"})
            summaries = [
                {"id": d["id"], "event": d["event"]}
                for d in sorted(self.deliveries.values(), key=lambda d: -d["id"])
            ]
            return httpx.Response(200, json=summaries)
        delivery_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=self.deliveries[delivery_id])


class Coordinator:
    def __init__(self, fail: int = 0) -> None:
        self.jobs: list = []
        self.headers: list[dict] = []
        self.fail = fail  # answer this many requests with 503 first
        self.receiver = WebhookReceiver([SECRET], self.jobs.append)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        if self.fail:
            self.fail -= 1
            return httpx.Response(503)
        resp = self.receiver.handle(dict(request.headers), request.content)
        return httpx.Response(resp.status, text=resp.message)


def _relay(hook: FakeHook, coordinator: Coordinator) -> DeliveryRelay:
    github = TokenClient("tok", client=httpx.Client(transport=httpx.MockTransport(hook.handler)))
    http = httpx.Client(transport=httpx.MockTransport(coordinator.handler))
    return DeliveryRelay(github, "acme/app", HOOK, TARGET, SECRET, http=http)


def test_forwards_new_deliveries_oldest_first_with_githubs_signature():
    hook, coord = FakeHook(), Coordinator()
    hook.add(2, octopus_job(id=202))
    hook.add(1, octopus_job(id=101))
    relay = _relay(hook, coord)

    assert relay.poll_once() == 2
    assert [j.job_id for j in coord.jobs] == [101, 202]
    assert [r.status for r in relay.relayed] == [202, 202]
    assert relay.resigned == 0
    assert "x-github-hook-id" not in coord.headers[0]


def test_skips_deliveries_from_before_the_start_and_other_events():
    hook, coord = FakeHook(), Coordinator()
    hook.add(1, octopus_job(id=101))
    relay = _relay(hook, coord)
    relay.skip_existing()
    hook.add(2, {"zen": "hi"}, event="ping")
    hook.add(3, octopus_job(id=303))

    assert relay.poll_once() == 1
    assert [j.job_id for j in coord.jobs] == [303]
    assert relay.poll_once() == 0


def test_resigns_a_body_whose_signature_does_not_match():
    hook, coord = FakeHook(), Coordinator()
    hook.add(1, octopus_job(id=101), sig="sha256=" + "0" * 64)
    relay = _relay(hook, coord)

    assert relay.poll_once() == 1
    assert relay.resigned == 1
    assert [j.job_id for j in coord.jobs] == [101]


def test_retries_a_delivery_the_coordinator_could_not_process():
    hook, coord = FakeHook(), Coordinator(fail=1)
    hook.add(1, octopus_job(id=101))
    relay = _relay(hook, coord)

    assert relay.poll_once() == 0
    assert relay.poll_once() == 1
    assert [j.job_id for j in coord.jobs] == [101]


def test_retries_when_the_coordinator_is_unreachable():
    hook, coord = FakeHook(), Coordinator()
    hook.add(1, octopus_job(id=101))
    relay = _relay(hook, coord)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    relay._http = httpx.Client(transport=httpx.MockTransport(refuse))
    assert relay.poll_once() == 0
    relay._http = httpx.Client(transport=httpx.MockTransport(coord.handler))
    assert relay.poll_once() == 1
