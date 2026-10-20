import os
import stat

import pytest
from _fake_github_repo import RATE_LIMITED, REPO, TOKEN, FakeRepoGitHub
from pydantic import SecretStr

from octopus.github.setup import (
    EXAMPLE_BRANCH,
    EXAMPLE_PATH,
    SetupOptions,
    Status,
    read_env_file,
    setup,
    teardown,
    write_env_file,
)

URL = "https://octopus.example.org/github/webhook"
COORD_TOKEN = "github_pat_COORDINATOR987"
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


class FakeTime:
    """sleep() advances the clocks, so polling and backoff run instantly."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def clock(self):
        return self.now


@pytest.fixture
def gh():
    return FakeRepoGitHub()


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / "coordinator.env"


def opts(env_file, **kw):
    kw.setdefault("coordinator_token", SecretStr(COORD_TOKEN))
    return SetupOptions(repo=REPO, webhook_url=URL, env_file=env_file, **kw)


def run_setup(gh, env_file, t=None, **kw):
    t = t or FakeTime()
    return setup(gh.client(), opts(env_file, **kw), sleep=t.sleep, clock=t.clock, monotonic=t.clock)


def statuses(checks):
    return {c.name: c.status for c in checks}


def the_hook(gh):
    (hook,) = gh.hooks.values()
    return hook


# --- setup ------------------------------------------------------------------------------


def test_setup_from_scratch(gh, env_file):
    checks = run_setup(gh, env_file)
    assert statuses(checks) == {
        "admin access": Status.OK,
        "coordinator.env": Status.CHANGED,
        "webhook": Status.CHANGED,
        "example workflow PR": Status.CHANGED,
        "ping": Status.OK,
    }
    hook = the_hook(gh)
    assert hook["events"] == ["workflow_job"] and hook["active"]
    assert hook["config"]["url"] == URL
    assert hook["config"]["content_type"] == "json" and hook["config"]["insecure_ssl"] == "0"
    env = read_env_file(env_file)
    assert env == {
        "OCTOPUS_GITHUB_REPO": REPO,
        "OCTOPUS_WEBHOOK_URL": URL,
        "OCTOPUS_WEBHOOK_SECRET": hook["config"]["secret"],
        "OCTOPUS_GITHUB_TOKEN": COORD_TOKEN,
    }
    assert len(env["OCTOPUS_WEBHOOK_SECRET"]) == 64


def test_example_pr_content(gh, env_file):
    run_setup(gh, env_file)
    (pr,) = gh.open_pulls()
    assert pr["head"] == EXAMPLE_BRANCH and pr["base"] == "main"
    workflow = gh.files[(EXAMPLE_BRANCH, EXAMPLE_PATH)]
    assert "runs-on: [self-hosted, octopus]" in workflow
    assert "branches: [main]" in workflow
    assert (gh.default_branch, EXAMPLE_PATH) not in gh.files  # never pushed to main


@POSIX
def test_env_file_is_private(gh, env_file):
    run_setup(gh, env_file)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_rerun_changes_nothing(gh, env_file):
    run_setup(gh, env_file)
    before = env_file.read_bytes(), env_file.stat().st_mtime_ns
    gh.requests.clear()
    checks = run_setup(gh, env_file)
    assert all(c.status is Status.OK for c in checks), checks
    assert gh.mutations() == [("POST", f"/repos/{REPO}/hooks/{the_hook(gh)['id']}/pings")]
    assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == before
    assert len(gh.hooks) == 1 and len(gh.pulls) == 1


def test_existing_hook_with_unknown_secret_gets_a_new_one(gh, env_file):
    hook_id = gh.add_hook(URL, secret="old-secret")
    checks = run_setup(gh, env_file)
    assert statuses(checks)["webhook"] is Status.CHANGED
    assert "new secret" in next(c.detail for c in checks if c.name == "webhook")
    secret = gh.hooks[hook_id]["config"]["secret"]
    assert secret != "old-secret"
    assert read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"] == secret
    assert len(gh.hooks) == 1


def test_existing_hook_events_fixed_without_touching_the_secret(gh, env_file):
    run_setup(gh, env_file)
    hook = the_hook(gh)
    secret = hook["config"]["secret"]
    hook["events"] = ["push", "workflow_job"]
    gh.requests.clear()
    run_setup(gh, env_file)
    (patch,) = [b for m, p, b in gh.requests if m == "PATCH"]
    assert patch == {"events": ["workflow_job"], "active": True}
    assert hook["config"]["secret"] == secret


def test_existing_hook_with_insecure_config_fixed(gh, env_file):
    run_setup(gh, env_file)
    hook = the_hook(gh)
    secret = hook["config"]["secret"]
    hook["config"].update(insecure_ssl="1", content_type="form")
    run_setup(gh, env_file)
    assert hook["config"] == {
        "url": URL,
        "content_type": "json",
        "insecure_ssl": "0",
        "secret": secret,
    }


def test_deleted_hook_recreated_with_the_known_secret(gh, env_file):
    run_setup(gh, env_file)
    secret = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    gh.hooks.clear()
    checks = run_setup(gh, env_file)
    assert statuses(checks)["webhook"] is Status.CHANGED
    assert the_hook(gh)["config"]["secret"] == secret
    assert statuses(checks)["coordinator.env"] is Status.OK


def test_rotate_secret(gh, env_file):
    run_setup(gh, env_file)
    old = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    run_setup(gh, env_file, rotate_secret=True)
    new = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    assert new != old and the_hook(gh)["config"]["secret"] == new


def test_secret_for_another_url_is_not_reused(gh, env_file):
    run_setup(gh, env_file)
    old = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    other = "https://other.example.org/github/webhook"
    setup(gh.client(), SetupOptions(REPO, other, env_file, ping=False), sleep=lambda s: None)
    assert read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"] != old


def test_not_admin_stops_before_any_change(gh, env_file):
    gh.admin = False
    checks = run_setup(gh, env_file)
    assert [(c.name, c.status) for c in checks] == [("admin access", Status.FAIL)]
    assert gh.mutations() == [] and not env_file.exists()


def test_repo_not_found(gh, env_file):
    gh.exists = False
    (check,) = run_setup(gh, env_file)
    assert check.failed and "not found" in check.detail


def test_classic_token_without_repo_scope(gh, env_file):
    gh.scopes = "gist, read:org"
    (check,) = run_setup(gh, env_file)
    assert check.failed and "gh auth refresh" in check.detail


def test_classic_token_with_repo_scope_passes(gh, env_file):
    gh.scopes = "gist, read:org, repo, workflow"
    assert statuses(run_setup(gh, env_file))["admin access"] is Status.OK


def test_unknown_admin_is_a_warning(gh, env_file):
    gh.admin = None
    assert statuses(run_setup(gh, env_file))["admin access"] is Status.WARN


def test_forbidden_hook_creation_fails_and_rerun_reuses_the_secret(gh, env_file):
    gh.faults[("POST", rf"/repos/{REPO}/hooks")] = [404]
    checks = run_setup(gh, env_file)
    check = checks[-1]
    assert check.name == "webhook" and check.failed
    assert "Webhooks: read and write" in check.detail
    assert not gh.hooks
    secret = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    run_setup(gh, env_file)  # permission fixed
    assert the_hook(gh)["config"]["secret"] == secret


def test_rate_limited_call_is_retried(gh, env_file):
    gh.faults[("GET", rf"/repos/{REPO}/hooks")] = [RATE_LIMITED]
    t = FakeTime()
    checks = run_setup(gh, env_file, t)
    assert statuses(checks)["webhook"] is Status.CHANGED
    assert t.sleeps and t.sleeps[0] == 0.0  # X-RateLimit-Reset already passed


def test_server_errors_are_retried_then_reported(gh, env_file):
    gh.faults[("GET", rf"/repos/{REPO}/hooks")] = [502, 502, 502]
    checks = run_setup(gh, env_file)
    assert checks[-1].name == "webhook" and checks[-1].failed and "502" in checks[-1].detail


def test_failed_writes_are_not_retried(gh, env_file):
    gh.faults[("POST", rf"/repos/{REPO}/hooks")] = [502]
    checks = run_setup(gh, env_file)
    assert checks[-1].failed and "502" in checks[-1].detail
    assert [m for m in gh.mutations() if m == ("POST", f"/repos/{REPO}/hooks")] == [
        ("POST", f"/repos/{REPO}/hooks")
    ]


def test_rate_limited_writes_are_retried(gh, env_file):
    gh.faults[("POST", rf"/repos/{REPO}/hooks")] = [RATE_LIMITED]
    assert statuses(run_setup(gh, env_file))["webhook"] is Status.CHANGED
    assert len(gh.hooks) == 1


def test_example_pr_skipped(gh, env_file):
    checks = run_setup(gh, env_file, example_pr=False)
    assert statuses(checks)["example workflow PR"] is Status.SKIPPED
    assert not gh.pulls and EXAMPLE_BRANCH not in gh.refs


def test_example_already_on_default_branch(gh, env_file):
    gh.files[("main", EXAMPLE_PATH)] = "on: push"
    checks = run_setup(gh, env_file)
    assert statuses(checks)["example workflow PR"] is Status.OK
    assert not gh.pulls


def test_example_branch_left_from_a_closed_pr_is_reused(gh, env_file):
    run_setup(gh, env_file)
    gh.pulls[0]["state"] = "closed"
    run_setup(gh, env_file)
    assert len(gh.open_pulls()) == 1 and len(gh.pulls) == 2


def test_example_pr_without_permission_is_a_warning(gh, env_file):
    gh.faults[("PUT", rf"/repos/{REPO}/contents/.+")] = [403]
    checks = run_setup(gh, env_file)
    check = next(c for c in checks if c.name == "example workflow PR")
    assert check.status is Status.WARN and "Workflows: write" in check.detail
    assert statuses(checks)["ping"] is Status.OK


def test_ping_rejected_by_coordinator(gh, env_file):
    gh.ping_status = 401
    check = run_setup(gh, env_file)[-1]
    assert check.name == "ping" and check.failed
    assert "401" in check.detail and "OCTOPUS_WEBHOOK_SECRET" in check.detail


def test_ping_unreachable(gh, env_file):
    gh.ping_status = 0
    check = run_setup(gh, env_file)[-1]
    assert check.failed and "could not reach" in check.detail


def test_ping_never_delivered_times_out(gh, env_file):
    gh.ping_status = None
    t = FakeTime()
    check = run_setup(gh, env_file, t, ping_timeout_s=10)[-1]
    assert check.failed and "within 10s" in check.detail
    assert sum(t.sleeps) >= 10


def test_old_ping_deliveries_are_not_mistaken_for_ours(gh, env_file):
    run_setup(gh, env_file, ping=False)
    hook_id = the_hook(gh)["id"]
    gh.deliveries[hook_id] = [{"id": 1, "event": "ping", "status_code": 200, "status": "OK"}]
    gh.ping_status = 401
    assert run_setup(gh, env_file)[-1].failed


def test_ping_skipped(gh, env_file):
    checks = run_setup(gh, env_file, ping=False)
    assert statuses(checks)["ping"] is Status.SKIPPED
    assert not any(p.endswith("/pings") for _, p in gh.mutations())


def test_without_coordinator_token_env_file_warns(gh, env_file):
    checks = run_setup(gh, env_file, coordinator_token=None)
    check = next(c for c in checks if c.name == "coordinator.env")
    assert check.status is Status.WARN and "OCTOPUS_GITHUB_TOKEN" in check.detail
    assert "OCTOPUS_GITHUB_TOKEN" not in read_env_file(env_file)


def test_existing_coordinator_token_is_kept(gh, env_file):
    run_setup(gh, env_file)
    checks = run_setup(gh, env_file, coordinator_token=None)
    assert statuses(checks)["coordinator.env"] is Status.OK
    assert read_env_file(env_file)["OCTOPUS_GITHUB_TOKEN"] == COORD_TOKEN


def test_secrets_never_in_checks(gh, env_file):
    gh.ping_status = 401
    gh.faults[("PUT", rf"/repos/{REPO}/contents/.+")] = [403]
    checks = run_setup(gh, env_file)
    secret = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"]
    text = repr(checks)
    assert TOKEN not in text and COORD_TOKEN not in text and secret not in text


# --- env file ---------------------------------------------------------------------------


def test_env_file_keeps_other_lines(env_file):
    env_file.write_text("# mine\nOCTOPUS_PORT=9000\nOCTOPUS_GITHUB_REPO=old/repo\n")
    assert write_env_file(env_file, {"OCTOPUS_GITHUB_REPO": REPO, "OCTOPUS_X": "1"})
    assert env_file.read_text() == (
        f"# mine\nOCTOPUS_PORT=9000\nOCTOPUS_GITHUB_REPO={REPO}\nOCTOPUS_X=1\n"
    )
    assert not write_env_file(env_file, {"OCTOPUS_GITHUB_REPO": REPO})


@POSIX
def test_env_file_permissions_are_tightened(env_file):
    write_env_file(env_file, {"A": "1"})
    env_file.chmod(0o644)
    assert write_env_file(env_file, {"A": "1"})  # same content, but rewritten private
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_env_file_rejects_newlines(env_file):
    with pytest.raises(ValueError):
        write_env_file(env_file, {"A": "1\nB=2"})


def test_unwritable_env_file_fails_before_github_changes(gh, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    checks = run_setup(gh, blocker / "coordinator.env")
    assert checks[-1].name == "coordinator.env" and checks[-1].failed
    assert gh.mutations() == []


# --- teardown ---------------------------------------------------------------------------


def runner(id, name, busy=False, status="offline"):
    return {"id": id, "name": name, "busy": busy, "status": status}


def test_teardown_leaves_the_repo_clean(gh, env_file):
    run_setup(gh, env_file)
    gh.runners = [
        runner(1, "octopus-a-b-1"),
        runner(2, "octopus-c-d-1", status="online"),
        runner(3, "build-box"),
    ]
    checks = teardown(gh.client(), REPO, URL, sleep=lambda s: None)
    assert statuses(checks) == {
        "admin access": Status.OK,
        "webhook": Status.CHANGED,
        "octopus-* runners": Status.CHANGED,
        "example workflow PR": Status.CHANGED,
    }
    assert not gh.hooks
    assert [r["name"] for r in gh.runners] == ["build-box"]
    assert not gh.open_pulls() and EXAMPLE_BRANCH not in gh.refs
    again = teardown(gh.client(), REPO, URL, sleep=lambda s: None)
    assert all(c.status is Status.OK for c in again), again


def test_teardown_leaves_busy_runners(gh):
    gh.runners = [
        runner(1, "octopus-a-b-1", busy=True, status="online"),
        runner(2, "octopus-c-d-1"),
    ]
    checks = teardown(gh.client(), REPO, URL, sleep=lambda s: None)
    check = next(c for c in checks if c.name == "octopus-* runners")
    assert check.status is Status.WARN and "octopus-a-b-1" in check.detail
    assert [r["id"] for r in gh.runners] == [1]


def test_teardown_keeps_other_hooks(gh):
    gh.add_hook("https://ci.example.org/hook", events=("push",))
    ours = gh.add_hook(URL)
    teardown(gh.client(), REPO, URL, sleep=lambda s: None)
    assert ours not in gh.hooks and len(gh.hooks) == 1


def test_teardown_without_url_still_cleans_the_rest(gh):
    gh.runners = [runner(1, "octopus-a-b-1")]
    checks = teardown(gh.client(), REPO, None, sleep=lambda s: None)
    assert statuses(checks)["webhook"] is Status.FAIL
    assert not gh.runners


def test_teardown_leaves_a_merged_example_in_place(gh, env_file):
    run_setup(gh, env_file)
    gh.files[("main", EXAMPLE_PATH)] = gh.files[(EXAMPLE_BRANCH, EXAMPLE_PATH)]
    gh.pulls[0]["state"] = "closed"
    check = teardown(gh.client(), REPO, URL, sleep=lambda s: None)[-1]
    assert "left in place" in check.detail
    assert ("main", EXAMPLE_PATH) in gh.files


def test_coordinator_accepts_the_written_secret(gh, env_file):
    """What GitHub signs with the hook's secret, the coordinator verifies with the env file."""
    import json

    from octopus.github.webhooks import WebhookReceiver, sign

    run_setup(gh, env_file)
    env = read_env_file(env_file)
    receiver = WebhookReceiver([env["OCTOPUS_WEBHOOK_SECRET"].encode()], lambda event: None)
    body = json.dumps({"zen": "Keep it logically awesome.", "hook_id": 1}).encode()
    hook_secret = the_hook(gh)["config"]["secret"].encode()
    headers = {
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "d-ping",
        "X-Hub-Signature-256": sign(hook_secret, body),
    }
    assert receiver.handle(headers, body).status == 200
