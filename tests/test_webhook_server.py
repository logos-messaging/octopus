import json
import socket

import httpx
import pytest
from conftest import SECRET, octopus_job

from octopus.coordinator.webhook_server import DEFAULT_PATH, WebhookServer
from octopus.github.webhooks import WebhookReceiver, sign


@pytest.fixture
def events():
    return []


@pytest.fixture
def server(events):
    receiver = WebhookReceiver([SECRET], events.append, max_body_bytes=64 * 1024)
    srv = WebhookServer(receiver, port=0)
    srv.start()
    yield srv
    srv.stop()


def url(server, path=DEFAULT_PATH):
    host, port = server.address
    return f"http://{host}:{port}{path}"


def post(server, payload, secret=SECRET, delivery="d-1", path=DEFAULT_PATH):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "workflow_job",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(secret, body),
        "Content-Type": "application/json",
    }
    return httpx.post(url(server, path), content=body, headers=headers)


def test_signed_delivery_accepted(server, events):
    resp = post(server, octopus_job())
    assert resp.status_code == 202
    assert [e.job_id for e in events] == [289782451]


def test_bad_signature_rejected(server, events):
    assert post(server, octopus_job(), secret=b"nope").status_code == 401
    assert events == []


def test_duplicate_delivery_processed_once(server, events):
    assert post(server, octopus_job()).status_code == 202
    resp = post(server, octopus_job())
    assert resp.status_code == 200 and resp.text.strip() == "duplicate delivery"
    assert len(events) == 1


def test_oversized_body_refused_before_reading(server, events):
    # Announce a huge body but send none: the server must answer from the header alone.
    host, port = server.address
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(
            f"POST {DEFAULT_PATH} HTTP/1.1\r\nHost: x\r\nContent-Length: {10**9}\r\n\r\n".encode()
        )
        status_line = s.recv(1024).split(b"\r\n", 1)[0]
    assert status_line.split()[1] == b"413"
    assert events == []


def test_chunked_body_refused(server):
    def gen():
        yield b"{}"

    resp = httpx.post(url(server), content=gen())  # chunked: no Content-Length
    assert resp.status_code == 411


def test_wrong_path_and_method(server):
    assert post(server, octopus_job(), path="/other").status_code == 404
    assert httpx.get(url(server)).status_code == 405
    assert httpx.get(url(server, "/")).status_code == 404


def test_binds_localhost_by_default(events):
    srv = WebhookServer(WebhookReceiver([SECRET], events.append), port=0)
    try:
        assert srv.address[0] == "127.0.0.1"
    finally:
        srv.stop()
