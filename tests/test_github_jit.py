import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from octopus.coordinator.scheduler import JitConfigError, PendingJob
from octopus.github import (
    GitHubApp,
    JitRunnerError,
    JitRunners,
    make_jit_provider,
    runner_name,
)
from octopus.proto import Presence

T0 = 1_700_000_000.0
SECRET = "SUPER-SECRET-JIT-CONFIG"
REPO = "acme/app"
BASE = f"/repos/{REPO}/actions/runners"


class FakeRunnersApi:
    """GitHub stub: installation tokens, generate-jitconfig, list and delete runners."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.generate_responses: list[httpx.Response] = []
        self.runners: list[dict] = []
        self.deleted: list[int] = []
        self.next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77})
        if request.method == "POST" and path == f"{BASE}/generate-jitconfig":
            if self.generate_responses:
                return self.generate_responses.pop(0)
            self.next_id += 1
            return httpx.Response(
                201,
                json={"runner": {"id": self.next_id, "name": "n"}, "encoded_jit_config": SECRET},
            )
        if request.method == "GET" and path == BASE:
            page = int(request.url.params.get("page", 1))
            per = int(request.url.params.get("per_page", 30))
            chunk = self.runners[(page - 1) * per : page * per]
            return httpx.Response(200, json={"total_count": len(self.runners), "runners": chunk})
        if request.method == "DELETE" and path.startswith(f"{BASE}/"):
            rid = int(path.rsplit("/", 1)[1])
            if not any(r["id"] == rid for r in self.runners) and rid not in self._issued_ids():
                return httpx.Response(404, json={"message": "Not Found"})
            self.deleted.append(rid)
            self.runners = [r for r in self.runners if r["id"] != rid]
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Not Found"})

    def _issued_ids(self):
        return range(101, self.next_id + 1)

    def calls(self, method: str, suffix: str = "") -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == method and r.url.path.startswith(BASE) and r.url.path.endswith(suffix)
        ]


class Env:
    def __init__(self, rsa_key_pem, **kw):
        self.now = T0
        self.sleeps: list[float] = []
        self.api = FakeRunnersApi()
        client = httpx.Client(transport=httpx.MockTransport(self.api.handler))
        app = GitHubApp(1, rsa_key_pem, client=client, clock=lambda: self.now)
        self.jit = JitRunners(app, sleep=self.sleeps.append, clock=lambda: self.now, **kw)


@pytest.fixture
def env(rsa_key_pem):
    return Env(rsa_key_pem)


def test_generate_posts_name_group_and_labels(env):
    runner = env.jit.generate(REPO, "octopus-aaaa-bbbb-0", ["linux", "x64", "octopus"])
    (req,) = env.api.calls("POST", "generate-jitconfig")
    body = json.loads(req.content)
    assert body["name"] == "octopus-aaaa-bbbb-0"
    assert body["runner_group_id"] == 1
    assert body["labels"] == ["self-hosted", "octopus", "linux", "x64"]
    assert runner.jit_config.get_secret_value() == SECRET
    assert runner.github_runner_id == 101
    assert SECRET not in repr(runner)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_client_errors_are_not_retried(env, status):
    env.api.generate_responses = [httpx.Response(status, json={"message": "nope"})]
    with pytest.raises(JitRunnerError, match=str(status)):
        env.jit.generate(REPO, "octopus-a", [])
    assert len(env.api.calls("POST", "generate-jitconfig")) == 1
    assert env.sleeps == []


def test_server_errors_are_retried_with_backoff_then_succeed(env):
    env.api.generate_responses = [httpx.Response(502), httpx.Response(503)]
    runner = env.jit.generate(REPO, "octopus-a", [])
    assert runner.github_runner_id == 101
    assert env.sleeps == [1.0, 2.0]
    assert len(env.api.calls("POST", "generate-jitconfig")) == 3


def test_persistent_server_errors_give_up(env):
    env.api.generate_responses = [httpx.Response(500)] * 10
    with pytest.raises(JitRunnerError, match="500"):
        env.jit.generate(REPO, "octopus-a", [])
    assert len(env.api.calls("POST", "generate-jitconfig")) == 4
    assert env.sleeps == [1.0, 2.0, 4.0]


def test_retry_after_is_honored(env):
    env.api.generate_responses = [httpx.Response(429, headers={"Retry-After": "7"})]
    env.jit.generate(REPO, "octopus-a", [])
    assert env.sleeps == [7.0]


def test_primary_rate_limit_waits_until_reset(env):
    reset = str(int(T0 + 12))
    env.api.generate_responses = [
        httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset},
            json={"message": "API rate limit exceeded"},
        )
    ]
    env.jit.generate(REPO, "octopus-a", [])
    assert env.sleeps == [12.0]


def test_plain_403_is_not_a_rate_limit(env):
    env.api.generate_responses = [httpx.Response(403, json={"message": "forbidden"})]
    with pytest.raises(JitRunnerError, match="403"):
        env.jit.generate(REPO, "octopus-a", [])
    assert env.sleeps == []


def test_delay_is_capped(env):
    env.api.generate_responses = [httpx.Response(429, headers={"Retry-After": "9999"})]
    env.jit.generate(REPO, "octopus-a", [])
    assert env.sleeps == [60.0]


def test_malformed_response_is_an_error(env):
    env.api.generate_responses = [httpx.Response(201, json={"unexpected": True})]
    with pytest.raises(JitRunnerError, match="malformed"):
        env.jit.generate(REPO, "octopus-a", [])


def test_secret_never_in_logs_or_errors(env, caplog):
    caplog.set_level(logging.DEBUG)
    runner = env.jit.generate(REPO, "octopus-a", [])
    env.jit.release(runner.name)
    env.api.generate_responses = [httpx.Response(500, text=SECRET)] * 4
    with pytest.raises(JitRunnerError) as exc:
        env.jit.generate(REPO, "octopus-b", [])
    assert SECRET not in caplog.text
    assert SECRET not in str(exc.value)


def test_release_deletes_runner_it_issued(env):
    runner = env.jit.generate(REPO, "octopus-a", [])
    assert env.jit.release("octopus-a") is True
    assert env.api.deleted == [runner.github_runner_id]
    assert env.jit.release("octopus-a") is False  # forgotten after deletion
    assert env.jit.release("never-issued") is False


def test_delete_treats_missing_runner_as_removed(env):
    assert env.jit.delete(REPO, 999) is True


def test_list_pages_and_filters_by_prefix(env):
    env.api.runners = [
        {"id": i, "name": f"octopus-{i}", "status": "offline", "busy": False} for i in range(1, 151)
    ] + [{"id": 900, "name": "someone-elses", "status": "online", "busy": False}]
    names = [r.name for r in env.jit.list_octopus_runners(REPO)]
    assert len(names) == 150
    assert "someone-elses" not in names
    assert len(env.api.calls("GET")) == 2


def reg(id, name, status="offline", busy=False):
    return {"id": id, "name": name, "status": status, "busy": busy}


def test_sweep_deletes_only_offline_idle_unknown_octopus_runners(env):
    env.api.runners = [
        reg(1, "octopus-orphan"),
        reg(2, "octopus-online", status="online"),
        reg(3, "octopus-busy", busy=True),
        reg(4, "octopus-inflight"),
        reg(5, "other-runner"),
    ]
    deleted = env.jit.sweep_orphans(REPO, keep={"octopus-inflight"})
    assert deleted == ["octopus-orphan"]
    assert env.api.deleted == [1]


def test_sweep_spares_runners_issued_within_grace(env):
    runner = env.jit.generate(REPO, "octopus-fresh", [])
    env.api.runners = [reg(runner.github_runner_id, "octopus-fresh")]
    assert env.jit.sweep_orphans(REPO, grace_s=600) == []
    env.now += 601
    assert env.jit.sweep_orphans(REPO, grace_s=600) == ["octopus-fresh"]


def test_runner_name_is_unique_per_runner_job_attempt():
    names = {
        runner_name("r" * 16, "job-12345678", 0),
        runner_name("r" * 16, "job-12345678", 1),
        runner_name("s" * 16, "job-12345678", 0),
    }
    assert len(names) == 3
    assert all(n.startswith("octopus-") for n in names)


def test_runner_name_differs_for_jobs_of_one_push():
    # Real ids of two jobs from one push: the first 8 digits are the same.
    assert runner_name("r" * 16, "118982330267", 1) != runner_name("r" * 16, "118982330333", 1)


def presence(runner_id="abcdef0123456789"):
    return Presence(runner_id=runner_id, os="linux", arch="x64", sent_at=T0)


def pending(attempt=0):
    return PendingJob(
        job_id="job-12345678",
        repo=REPO,
        labels=("self-hosted", "octopus", "linux", "x64"),
        enqueued_at=T0,
        attempt=attempt,
    )


def test_provider_returns_secret_and_names_runner(env):
    provide = make_jit_provider(env.jit)
    jit = provide(pending(), presence())
    assert isinstance(jit, SecretStr) and jit.get_secret_value() == SECRET
    (req,) = env.api.calls("POST", "generate-jitconfig")
    body = json.loads(req.content)
    assert body["name"] == "octopus-abcdef01-job-12345678-0"
    assert body["labels"] == ["self-hosted", "octopus", "linux", "x64"]


def test_provider_maps_failures_to_jitconfig_error(env):
    env.api.generate_responses = [httpx.Response(422, json={"message": "Validation Failed"})]
    provide = make_jit_provider(env.jit)
    with pytest.raises(JitConfigError, match="422"):
        provide(pending(), presence())
