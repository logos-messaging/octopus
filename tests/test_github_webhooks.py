import json
import threading

import pytest
from conftest import SECRET, load_fixture, octopus_job

from octopus.github.webhooks import (
    DeliveryLog,
    PayloadError,
    WebhookReceiver,
    parse_workflow_job,
    sign,
    verify_signature,
)

# Test vector from GitHub's "Validating webhook deliveries" docs.
DOCS_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def test_signature_matches_github_docs_vector():
    assert sign(SECRET, b"Hello, World!") == DOCS_SIGNATURE
    assert verify_signature([SECRET], b"Hello, World!", DOCS_SIGNATURE)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha1=" + DOCS_SIGNATURE[7:],  # legacy X-Hub-Signature format
        DOCS_SIGNATURE.upper(),
        DOCS_SIGNATURE[:-1] + "0",
        DOCS_SIGNATURE + "00",
        "sha256=éé",
    ],
)
def test_bad_signatures_rejected(header):
    assert not verify_signature([SECRET], b"Hello, World!", header)


def test_signature_covers_body_and_secret():
    assert not verify_signature([SECRET], b"Hello, World?", DOCS_SIGNATURE)
    assert not verify_signature([b"other"], b"Hello, World!", DOCS_SIGNATURE)


def test_any_configured_secret_verifies_for_rotation():
    assert verify_signature([b"new", SECRET], b"Hello, World!", DOCS_SIGNATURE)


# --- parsing recorded GitHub payloads -------------------------------------------------


def test_parse_queued():
    ev = parse_workflow_job(load_fixture("workflow_job.queued.json"), "d1")
    assert ev.action == "queued"
    assert ev.job_id == 289782451 and ev.run_id == 2202229078 and ev.run_attempt == 1
    assert ev.repo == "Codertocat/Hello-World" and ev.owner == "Codertocat" and ev.org is None
    assert ev.installation_id == 3456996
    assert ev.sender == "Codertocat" and ev.sender_type == "User"
    assert ev.labels == ("ubuntu-latest",)
    assert ev.runner_name is None and ev.conclusion is None and ev.failed_step is None


def test_parse_in_progress():
    ev = parse_workflow_job(load_fixture("workflow_job.in_progress.json"), "d2")
    assert ev.action == "in_progress" and ev.runner_name == "GitHub Actions 5"


def test_parse_completed_failure_with_org():
    ev = parse_workflow_job(
        load_fixture("workflow_job.completed.failure.with-organization.json"), "d3"
    )
    assert ev.action == "completed" and ev.conclusion == "failure"
    assert ev.org == "Octocoders" and ev.installation_id is None
    payload = load_fixture("workflow_job.completed.failure.with-organization.json")
    first_failed = next(
        s["name"] for s in payload["workflow_job"]["steps"] if s["conclusion"] == "failure"
    )
    assert ev.failed_step == first_failed


def test_parse_completed_success():
    ev = parse_workflow_job(
        load_fixture("workflow_job.completed.success.with-organization.json"), "d4"
    )
    assert ev.conclusion == "success" and ev.failed_step is None


def test_parse_rejects_unhandled_action():
    with pytest.raises(PayloadError):
        parse_workflow_job(load_fixture("workflow_job.waiting.json"), "d5")


@pytest.mark.parametrize("section", ["workflow_job", "repository", "sender"])
def test_parse_rejects_wrongly_typed_sections(section):
    payload = load_fixture("workflow_job.queued.json")
    payload[section] = "oops"
    with pytest.raises(PayloadError):
        parse_workflow_job(payload, "d")


@pytest.mark.parametrize("drop", ["workflow_job", "repository", "sender", "action"])
def test_parse_rejects_missing_sections(drop):
    payload = load_fixture("workflow_job.queued.json")
    del payload[drop]
    with pytest.raises(PayloadError):
        parse_workflow_job(payload, "d")


def test_label_match_is_case_insensitive():
    ev = parse_workflow_job(octopus_job(labels=["self-hosted", "Octopus"]), "d")
    assert ev.has_label("octopus") and not ev.has_label("gpu")


# --- receiver ---------------------------------------------------------------------------


class Sink:
    def __init__(self, fail_times: int = 0):
        self.events = []
        self.fail_times = fail_times
        self.lock = threading.Lock()

    def __call__(self, event):
        with self.lock:
            if self.fail_times:
                self.fail_times -= 1
                raise RuntimeError("store unavailable")
            self.events.append(event)


def delivery(
    payload, event="workflow_job", delivery_id="72d3162e-cc78-11e3-81ab-4c9367dc0958", secret=SECRET
):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sign(secret, body),
        "Content-Type": "application/json",
    }
    return headers, body


@pytest.fixture
def sink():
    return Sink()


@pytest.fixture
def receiver(sink):
    return WebhookReceiver([SECRET], sink)


def test_octopus_job_accepted(receiver, sink):
    resp = receiver.handle(*delivery(octopus_job()))
    assert resp.status == 202
    assert [e.job_id for e in sink.events] == [289782451]


def test_header_names_are_case_insensitive(receiver, sink):
    headers, body = delivery(octopus_job())
    resp = receiver.handle({k.lower(): v for k, v in headers.items()}, body)
    assert resp.status == 202 and len(sink.events) == 1


def test_invalid_signature_rejected(receiver, sink):
    headers, body = delivery(octopus_job(), secret=b"wrong")
    assert receiver.handle(headers, body).status == 401
    assert sink.events == []


def test_missing_signature_rejected(receiver, sink):
    headers, body = delivery(octopus_job())
    del headers["X-Hub-Signature-256"]
    assert receiver.handle(headers, body).status == 401
    assert sink.events == []


def test_body_tampered_after_signing_rejected(receiver, sink):
    headers, body = delivery(octopus_job())
    tampered = body.replace(b"Codertocat/Hello-World", b"Codertocat/Hello-Evil!")
    assert receiver.handle(headers, tampered).status == 401
    assert sink.events == []


def test_signature_checked_before_parsing(receiver):
    # An unauthenticated request learns nothing about payload validation.
    headers, _ = delivery(octopus_job(), secret=b"wrong")
    assert receiver.handle(headers, b"not json").status == 401


def test_duplicate_delivery_processed_once(receiver, sink):
    headers, body = delivery(octopus_job())
    assert receiver.handle(headers, body).status == 202
    resp = receiver.handle(headers, body)
    assert resp.status == 200 and resp.message == "duplicate delivery"
    assert len(sink.events) == 1


def test_concurrent_duplicate_deliveries_processed_once(sink):
    receiver = WebhookReceiver([SECRET], sink)
    headers, body = delivery(octopus_job())
    barrier = threading.Barrier(16)
    statuses = []

    def post():
        barrier.wait()
        statuses.append(receiver.handle(headers, body).status)

    threads = [threading.Thread(target=post) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sink.events) == 1
    assert sorted(statuses) == [200] * 15 + [202]


def test_distinct_deliveries_of_same_job_all_processed(receiver, sink):
    for i, name in enumerate(["workflow_job.queued.json", "workflow_job.in_progress.json"]):
        assert receiver.handle(*delivery(octopus_job(name), delivery_id=f"d{i}")).status == 202
    assert [e.action for e in sink.events] == ["queued", "in_progress"]


def test_failed_processing_is_retried_on_redelivery():
    sink = Sink(fail_times=1)
    receiver = WebhookReceiver([SECRET], sink)
    headers, body = delivery(octopus_job())
    assert receiver.handle(headers, body).status == 500
    assert receiver.handle(headers, body).status == 202
    assert len(sink.events) == 1


def test_job_without_octopus_label_ignored(receiver, sink):
    resp = receiver.handle(*delivery(load_fixture("workflow_job.queued.json")))
    assert resp.status == 202 and "label" in resp.message
    assert sink.events == []


def test_custom_label(sink):
    receiver = WebhookReceiver([SECRET], sink, label="ubuntu-latest")
    assert receiver.handle(*delivery(load_fixture("workflow_job.queued.json"))).status == 202
    assert len(sink.events) == 1


def test_ping_answered(receiver, sink):
    resp = receiver.handle(*delivery(load_fixture("ping.json"), event="ping"))
    assert (resp.status, resp.message) == (200, "pong")
    assert sink.events == []


def test_other_events_ignored(receiver, sink):
    resp = receiver.handle(*delivery({"action": "opened"}, event="pull_request"))
    assert resp.status == 202 and "ignored" in resp.message
    assert sink.events == []


def test_waiting_action_ignored(receiver, sink):
    payload = octopus_job("workflow_job.waiting.json")
    resp = receiver.handle(*delivery(payload))
    assert resp.status == 202 and "ignored action" in resp.message
    assert sink.events == []


def test_oversized_body_rejected_before_verification(sink):
    receiver = WebhookReceiver([SECRET], sink, max_body_bytes=1000)
    resp = receiver.handle(*delivery(octopus_job()))
    assert resp.status == 413 and sink.events == []


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe"])
def test_invalid_json_rejected(receiver, body):
    assert receiver.handle(*delivery(body)).status == 400


def test_malformed_payload_rejected(receiver, sink):
    payload = octopus_job()
    del payload["workflow_job"]["run_id"]
    assert receiver.handle(*delivery(payload)).status == 400
    assert sink.events == []


def test_missing_delivery_id_rejected(receiver):
    headers, body = delivery(octopus_job())
    del headers["X-GitHub-Delivery"]
    assert receiver.handle(headers, body).status == 400


@pytest.mark.parametrize("secrets", [[], [b""]])
def test_empty_secret_refused(secrets):
    with pytest.raises(ValueError):
        WebhookReceiver(secrets, lambda e: None)


# --- delivery log ----------------------------------------------------------------------


def test_delivery_log_expires_after_ttl():
    now = [0.0]
    deliveries = DeliveryLog(ttl_s=60, clock=lambda: now[0])
    assert deliveries.claim("a")
    now[0] = 59
    assert not deliveries.claim("a")
    now[0] = 60
    assert deliveries.claim("a")


def test_delivery_log_is_bounded():
    deliveries = DeliveryLog(max_entries=3)
    for d in "abcd":
        assert deliveries.claim(d)
    assert len(deliveries) == 3
    assert deliveries.claim("a")  # oldest was evicted
    assert not deliveries.claim("d")


def test_delivery_log_release():
    deliveries = DeliveryLog()
    assert deliveries.claim("a")
    deliveries.release("a")
    assert deliveries.claim("a")


@pytest.mark.parametrize("full_name", ["no-slash", "/name", "owner/"])
def test_parse_rejects_bad_repo_name(full_name):
    payload = octopus_job()
    payload["repository"]["full_name"] = full_name
    with pytest.raises(PayloadError):
        parse_workflow_job(payload, "d")
