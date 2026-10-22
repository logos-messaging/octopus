import httpx
import pytest
from _oidc_keys import AUDIENCE, FakeIssuer
from conftest import SECRET

from octopus.agent import PresencePublisher, RunnerInfo
from octopus.coordinator import Registry
from octopus.coordinator.config import ConfigError, CoordinatorConfig
from octopus.coordinator.pick import PickService
from octopus.coordinator.picker import Picker
from octopus.coordinator.webhook_server import WebhookServer
from octopus.github.webhooks import WebhookReceiver
from octopus.oidc import OidcVerifier
from octopus.policy import TrustPolicy
from octopus.transport import MemoryBus, MemoryTransport


@pytest.fixture
def issuer():
    return FakeIssuer()


def build(issuer, runners=("a",)):
    bus = MemoryBus()
    registry = Registry(MemoryTransport(bus))
    registry.start()
    for n in runners:
        PresencePublisher(
            MemoryTransport(bus), RunnerInfo(f"id-{n}", "linux", "x64", (f"octopus-{n}",))
        ).tick()
    service = PickService(
        OidcVerifier(AUDIENCE, fetch=issuer.jwks),
        Picker(registry, allowed_labels=[f"octopus-{n}" for n in runners]),
        TrustPolicy(allowed_repos=frozenset({"acme/app"})),
    )
    server = WebhookServer(
        WebhookReceiver([SECRET], lambda e: None), port=0, pick=service.handle
    )
    server.start()
    host, port = server.address
    return server, f"http://{host}:{port}/pick"


def call(url, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(url, headers=headers, **kw)


def test_pick_returns_a_label(issuer):
    server, url = build(issuer)
    try:
        resp = call(url, issuer.token())
        assert resp.status_code == 200
        assert resp.json() == {"label": "octopus-a", "runner": "id-a"}
    finally:
        server.stop()


def test_no_runner_free_is_503_with_retry_after(issuer):
    server, url = build(issuer, runners=())
    try:
        resp = call(url, issuer.token())
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "30"
        assert resp.json()["retry_after_s"] == 30
    finally:
        server.stop()


def test_second_run_gets_another_runner_or_503(issuer):
    server, url = build(issuer)
    try:
        assert call(url, issuer.token(run_id="1")).status_code == 200
        assert call(url, issuer.token(run_id="2")).status_code == 503
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"repository": "evil/app", "job_workflow_ref": "evil/app/.github/workflows/x.yml@r"}, 403),
        ({"event_name": "pull_request_target"}, 403),
        ({"event_name": "workflow_run"}, 403),
        ({"job_workflow_ref": "acme/other/.github/workflows/x.yml@r"}, 403),
        ({"aud": "https://other"}, 401),
    ],
)
def test_untrusted_tokens(issuer, overrides, status):
    server, url = build(issuer)
    try:
        resp = call(url, issuer.token(**overrides))
        assert resp.status_code == status
        assert "label" not in resp.json()
    finally:
        server.stop()


def test_pull_request_event_allowed(issuer):
    server, url = build(issuer)
    try:
        assert call(url, issuer.token(event_name="pull_request")).status_code == 200
    finally:
        server.stop()


def test_missing_or_malformed_authorization(issuer):
    server, url = build(issuer)
    try:
        assert call(url).status_code == 401
        assert httpx.post(url, headers={"Authorization": "Basic abc"}).status_code == 401
        assert call(url, "garbage").status_code == 401
    finally:
        server.stop()


def test_replayed_token_is_refused(issuer):
    server, url = build(issuer, runners=("a", "b"))
    try:
        token = issuer.token()
        assert call(url, token).status_code == 200
        assert call(url, token).status_code == 401
    finally:
        server.stop()


def test_get_and_webhook_path_still_work(issuer):
    server, url = build(issuer)
    try:
        assert httpx.get(url).status_code == 405
        host, port = server.address
        assert httpx.get(f"http://{host}:{port}/github/webhook").status_code == 405
    finally:
        server.stop()


def test_pick_route_absent_without_pick_mode():
    receiver = WebhookReceiver([SECRET], lambda e: None)
    server = WebhookServer(receiver, port=0)
    server.start()
    try:
        host, port = server.address
        assert httpx.post(f"http://{host}:{port}/pick").status_code == 404
    finally:
        server.stop()


# config

BASE = {
    "OCTOPUS_WEBHOOK_SECRET": SECRET.decode(),
    "OCTOPUS_ALLOWED_REPOS": "acme/app",
    "OCTOPUS_PUBLIC_IP": "203.0.113.5",
}


def test_audience_turns_on_pick_mode_without_a_github_credential():
    cfg = CoordinatorConfig.from_env(
        {**BASE, "OCTOPUS_OIDC_AUDIENCE": AUDIENCE, "OCTOPUS_PICK_LABELS": "octopus-a"}
    )
    assert cfg.auth_mode == "oidc"
    assert cfg.pick_enabled and cfg.oidc_audience == AUDIENCE


def test_pick_mode_needs_the_runner_labels():
    with pytest.raises(ConfigError, match="OCTOPUS_PICK_LABELS"):
        CoordinatorConfig.from_env({**BASE, "OCTOPUS_OIDC_AUDIENCE": AUDIENCE})


def test_a_pick_label_must_have_the_octopus_prefix():
    with pytest.raises(ConfigError, match="invalid pick label"):
        CoordinatorConfig.from_env(
            {**BASE, "OCTOPUS_OIDC_AUDIENCE": AUDIENCE, "OCTOPUS_PICK_LABELS": "laptop"}
        )


def test_credentials_still_required_without_pick_mode():
    with pytest.raises(ConfigError, match="no GitHub credentials"):
        CoordinatorConfig.from_env(BASE)


def test_coordinator_in_pick_mode_end_to_end(issuer, tmp_path, monkeypatch):
    import json as _json

    from conftest import octopus_job

    from octopus.coordinator import service
    from octopus.github.webhooks import sign

    monkeypatch.setattr(service, "OidcVerifier", lambda aud: OidcVerifier(aud, fetch=issuer.jwks))
    config = CoordinatorConfig.from_env(
        {
            **BASE,
            "OCTOPUS_OIDC_AUDIENCE": AUDIENCE,
            "OCTOPUS_PICK_LABELS": "octopus-a",
            "OCTOPUS_WEBHOOK_LISTEN": "127.0.0.1:0",
        }
    )
    bus = MemoryBus()
    coordinator = service.Coordinator(config, MemoryTransport(bus), None, tmp_path)
    coordinator.start()
    try:
        PresencePublisher(
            MemoryTransport(bus), RunnerInfo("id-a", "linux", "x64", ("octopus", "octopus-a"))
        ).tick()
        host, port = coordinator.webhook_address
        pick = f"http://{host}:{port}/pick"
        assert call(pick, issuer.token(run_id="1001")).json()["label"] == "octopus-a"
        assert call(pick, issuer.token(run_id="1002")).status_code == 503

        payload = octopus_job("workflow_job.completed.success.with-organization.json")
        payload["workflow_job"]["labels"] = ["self-hosted", "octopus-a"]
        payload["workflow_job"]["run_id"] = 1001
        payload["repository"]["full_name"] = "acme/app"
        body = _json.dumps(payload).encode()
        resp = httpx.post(
            f"http://{host}:{port}/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": "d-1",
                "X-Hub-Signature-256": sign(SECRET, body),
            },
        )
        assert resp.status_code in (200, 202)
        assert call(pick, issuer.token(run_id="1002")).json()["label"] == "octopus-a"
    finally:
        coordinator.stop()
