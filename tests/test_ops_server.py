import httpx
import pytest

from octopus.coordinator.ops import MetricsRegistry, OpsServer, parse_metrics_listen


def make_server(checks=None, render=None):
    registry = MetricsRegistry()
    registry.counter("octopus_t_total", "T.").inc()
    return OpsServer(render or registry.render, checks, port=0)


@pytest.fixture
def started():
    servers = []

    def _start(**kw):
        srv = make_server(**kw)
        srv.start()
        servers.append(srv)
        host, port = srv.address
        return f"http://{host}:{port}"

    yield _start
    for s in servers:
        s.stop()


def test_healthz_and_metrics(started):
    base = started()
    assert httpx.get(f"{base}/healthz").text == "ok\n"
    r = httpx.get(f"{base}/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "octopus_t_total 1" in r.text


def test_readyz_follows_checks(started):
    state = {"node": True, "github": False}
    base = started(checks=lambda: state)
    r = httpx.get(f"{base}/readyz")
    assert r.status_code == 503
    assert r.json() == {"ready": False, "checks": {"github": False, "node": True}}
    state["github"] = True
    assert httpx.get(f"{base}/readyz").status_code == 200


def test_readyz_without_checks_is_ready(started):
    assert httpx.get(f"{started()}/readyz").status_code == 200


def test_readyz_fails_closed_when_checks_raise(started):
    def boom():
        raise RuntimeError("x")

    assert httpx.get(f"{started(checks=boom)}/readyz").status_code == 503


def test_metrics_render_error_is_500_and_server_survives(started):
    def boom():
        raise RuntimeError("secret detail")

    base = started(render=boom)
    r = httpx.get(f"{base}/metrics")
    assert r.status_code == 500
    assert "secret detail" not in r.text
    assert httpx.get(f"{base}/healthz").status_code == 200


def test_unknown_path_and_methods(started):
    base = started()
    assert httpx.get(f"{base}/nope").status_code == 404
    assert httpx.post(f"{base}/metrics").status_code == 405
    assert httpx.get(f"{base}/metrics?x=1").status_code == 200


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ("127.0.0.1", 9100)),
        ("0.0.0.0:9200", ("0.0.0.0", 9200)),
        ("[::1]:9100", ("::1", 9100)),
        ("off", None),
        ("OFF", None),
    ],
)
def test_parse_metrics_listen(value, expected):
    assert parse_metrics_listen(value) == expected


@pytest.mark.parametrize("value", ["9100", ":9100", "host:", "host:x", "host:70000"])
def test_parse_metrics_listen_rejects(value):
    with pytest.raises(ValueError, match="OCTOPUS_METRICS_LISTEN"):
        parse_metrics_listen(value)


def test_scrape_of_a_real_collector(started):
    from octopus.coordinator.jobs import SqliteJobStore
    from octopus.coordinator.ops import CoordinatorMetrics

    class NoRunners:
        def online(self):
            return []

    store = SqliteJobStore()
    metrics = CoordinatorMetrics(NoRunners(), store)
    store.create("j1", "acme/app")
    metrics.webhook("workflow_job", "accepted")
    base = started(render=metrics.render)
    text = httpx.get(f"{base}/metrics").text
    assert "octopus_runners_online 0" in text
    assert 'octopus_jobs{state="queued"} 1' in text
    assert 'octopus_webhook_events_total{event="workflow_job",outcome="accepted"} 1' in text
    store.close()
