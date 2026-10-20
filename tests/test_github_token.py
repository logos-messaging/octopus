import subprocess

import httpx
import pytest
from _fake_github_repo import REPO, TOKEN, FakeRepoGitHub
from conftest import octopus_job

from octopus.github.app import GitHubApiError
from octopus.github.runners import JitRunners
from octopus.github.token import TokenClient, TokenError, api_hostname, resolve_token
from octopus.github.trust import has_write_access, resolve_trust_inputs
from octopus.github.webhooks import parse_workflow_job
from octopus.policy import TrustPolicy


def gh_cli(stdout="", returncode=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return run


def test_explicit_token_wins():
    tok = resolve_token(" abc ", {"GITHUB_TOKEN": "env"}, run=gh_cli("gh"))
    assert (tok.value.get_secret_value(), tok.source) == ("abc", "--token")


def test_env_token_before_gh():
    tok = resolve_token(None, {"GITHUB_TOKEN": "env"}, run=gh_cli("gh"))
    assert (tok.value.get_secret_value(), tok.source) == ("env", "GITHUB_TOKEN")


def test_gh_login_fallback_uses_the_api_host():
    calls = []
    tok = resolve_token(
        None, {}, api_url="https://ghe.acme.io/api/v3", run=gh_cli("gho_x\n", calls=calls)
    )
    assert (tok.value.get_secret_value(), tok.source) == ("gho_x", "gh")
    assert calls == [["gh", "auth", "token", "--hostname", "ghe.acme.io"]]


@pytest.mark.parametrize("run", [gh_cli("", 1), gh_cli("  ")])
def test_no_token_anywhere(run):
    with pytest.raises(TokenError, match="gh auth login"):
        resolve_token(None, {}, run=run)


def test_gh_not_installed():
    def run(cmd, **kw):
        raise FileNotFoundError("gh")

    with pytest.raises(TokenError):
        resolve_token(None, {}, run=run)


def test_resolved_token_repr_hides_it():
    tok = resolve_token("supersecret", {})
    assert "supersecret" not in repr(tok)


@pytest.mark.parametrize(
    ("url", "host"),
    [("https://api.github.com", "github.com"), ("https://ghe.acme.io/api/v3", "ghe.acme.io")],
)
def test_api_hostname(url, host):
    assert api_hostname(url) == host


def test_token_client_auth_and_errors():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(404, json={"message": "Not Found"})

    client = TokenClient(TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubApiError) as exc:
        client.get_json("/repos/acme/app")
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert TOKEN not in str(exc.value) and TOKEN not in repr(client)
    assert client.repo_installation_id("acme/app") == 0


def test_jit_runners_work_with_a_token():
    gh = FakeRepoGitHub(
        runners=[{"id": 1, "name": "octopus-a-b-1", "busy": False, "status": "offline"}]
    )
    jit = JitRunners(gh.client(), sleep=lambda s: None)
    assert [r.name for r in jit.list_octopus_runners(REPO)] == ["octopus-a-b-1"]


# --- trust by repo permission -----------------------------------------------------------


def job(sender="alice", sender_type="User", repo=REPO):
    payload = octopus_job()
    payload["repository"]["full_name"] = repo
    payload["sender"] = {"login": sender, "type": sender_type}
    return parse_workflow_job(payload, "d1")


@pytest.mark.parametrize(
    ("permission", "trusted"),
    [("admin", True), ("write", True), ("read", False), ("none", False)],
)
def test_write_access(permission, trusted):
    gh = FakeRepoGitHub(permissions={"alice": permission})
    assert has_write_access(gh.client(), job(), 0) is trusted


def test_unknown_user_is_not_trusted():
    assert not has_write_access(FakeRepoGitHub().client(), job(), 0)


def test_bot_is_not_trusted_without_asking():
    gh = FakeRepoGitHub(permissions={"dependabot[bot]": "write"})
    assert not has_write_access(gh.client(), job("dependabot[bot]", "Bot"), 0)
    assert gh.requests == []


def test_permission_api_error_raises():
    gh = FakeRepoGitHub()
    gh.faults[("GET", r".*/permission")] = [500, 500, 500]
    with pytest.raises(GitHubApiError):
        has_write_access(gh.client(), job(), 0)


def run_response(head):
    return {"event": "pull_request", "head_repository": {"full_name": head}}


class RunsAndPermissions(FakeRepoGitHub):
    """Adds the workflow-run endpoint the trust check reads."""

    head = REPO

    def _routes(self):
        return [
            (
                r"GET /actions/runs/(\d+)",
                lambda b, q, i: httpx.Response(200, json=run_response(self.head)),
            ),
            *super()._routes(),
        ]


def test_token_client_trusts_by_repo_permission():
    gh = RunsAndPermissions(permissions={"alice": "write"})
    inputs = resolve_trust_inputs(job(), gh.client())
    assert inputs.sender_check == "repo-permission" and inputs.sender_is_org_member
    policy = TrustPolicy(allowed_repos=frozenset({REPO}))
    assert policy.check(inputs.job_context()) == (True, "ok")
    assert not any("/orgs/" in p for _, p, _ in gh.requests)


def test_read_only_collaborator_is_rejected():
    gh = RunsAndPermissions(permissions={"alice": "read"})
    inputs = resolve_trust_inputs(job(), gh.client())
    ok, why = TrustPolicy(allowed_repos=frozenset({REPO})).check(inputs.job_context())
    assert not ok and "not trusted" in why


def test_fork_pr_is_rejected_even_from_a_writer():
    gh = RunsAndPermissions(permissions={"alice": "write"})
    gh.head = "mallory/app"
    inputs = resolve_trust_inputs(job(), gh.client())
    ok, why = TrustPolicy(allowed_repos=frozenset({REPO})).check(inputs.job_context())
    assert not ok and "fork" in why


def test_sender_check_can_be_forced():
    gh = RunsAndPermissions(permissions={"alice": "write"})
    inputs = resolve_trust_inputs(job(), gh.client(), sender_check="org-membership")
    # The recorded payload's repo is user-owned: only the owner counts.
    assert inputs.sender_check == "org-membership" and not inputs.sender_is_org_member
