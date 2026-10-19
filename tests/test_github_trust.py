import httpx
import pytest
from conftest import load_fixture, octopus_job

from octopus.github.app import GitHubApiError, GitHubApp
from octopus.github.trust import UNKNOWN_HEAD_REPO, resolve_trust_inputs
from octopus.github.webhooks import parse_workflow_job
from octopus.policy import TrustPolicy

# The recorded org payloads are for repo Codertocat/Hello-World in org Octocoders.
REPO = "Codertocat/Hello-World"
POLICY = TrustPolicy(allowed_repos=frozenset({REPO}))


def run_response(head_repo: str | None = REPO, event="pull_request") -> dict:
    """The recorded workflow run, pointed at `head_repo`."""
    run = load_fixture("workflow_run.json")
    run["event"] = event
    run["repository"]["full_name"] = REPO
    if head_repo is None:
        run["head_repository"] = None
    else:
        run["head_repository"]["full_name"] = head_repo
    return run


class FakeGitHub:
    def __init__(self, run=None, member_status=204):
        self.run = run if run is not None else run_response()
        self.member_status = member_status
        self.paths = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.paths.append(path)
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 99})
        if path.endswith("/actions/runs/2202229078"):
            return httpx.Response(200, json=self.run)
        if path.startswith("/orgs/Octocoders/members/"):
            return httpx.Response(self.member_status)
        return httpx.Response(404, json={"message": "Not Found"})


def make_app(rsa_key_pem, gh):
    return GitHubApp(1, rsa_key_pem, client=httpx.Client(transport=httpx.MockTransport(gh.handler)))


def org_job(**overrides):
    """A queued octopus job in an org repo, built from recorded payloads."""
    payload = octopus_job()
    org_payload = load_fixture("workflow_job.completed.failure.with-organization.json")
    payload["organization"] = org_payload["organization"]
    payload["repository"] = org_payload["repository"]
    payload.update(overrides)
    return parse_workflow_job(payload, "d1")


def test_same_repo_member_is_allowed(rsa_key_pem):
    gh = FakeGitHub()
    inputs = resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh))
    assert inputs.repo == REPO and inputs.head_repo == REPO and not inputs.is_fork
    assert inputs.event == "pull_request"
    assert inputs.sender == "Codertocat" and inputs.sender_is_org_member
    assert POLICY.check(inputs.job_context()) == (True, "ok")
    assert "/orgs/Octocoders/members/Codertocat" in gh.paths
    assert "/app/installations/3456996/access_tokens" in gh.paths  # from the payload


def test_fork_pr_is_rejected(rsa_key_pem):
    gh = FakeGitHub(run=run_response(head_repo="stranger/Hello-World"))
    inputs = resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh))
    assert inputs.is_fork and inputs.head_repo == "stranger/Hello-World"
    ok, reason = POLICY.check(inputs.job_context())
    assert not ok and "fork" in reason


def test_missing_head_repo_counts_as_fork(rsa_key_pem):
    gh = FakeGitHub(run=run_response(head_repo=None))
    inputs = resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh))
    assert inputs.head_repo == UNKNOWN_HEAD_REPO and inputs.is_fork
    assert not POLICY.check(inputs.job_context())[0]


def test_non_member_is_rejected(rsa_key_pem):
    gh = FakeGitHub(member_status=404)
    inputs = resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh))
    assert not inputs.sender_is_org_member
    assert POLICY.check(inputs.job_context()) == (False, "sender is not an org member")


def test_membership_not_visible_counts_as_non_member(rsa_key_pem):
    gh = FakeGitHub(member_status=302)
    assert not resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh)).sender_is_org_member


def test_membership_api_error_raises(rsa_key_pem):
    gh = FakeGitHub(member_status=500)
    with pytest.raises(GitHubApiError):
        resolve_trust_inputs(org_job(), make_app(rsa_key_pem, gh))


def test_bot_sender_is_never_a_member(rsa_key_pem):
    gh = FakeGitHub()
    job = org_job(sender={"login": "dependabot[bot]", "type": "Bot"})
    inputs = resolve_trust_inputs(job, make_app(rsa_key_pem, gh))
    assert inputs.sender_type == "Bot" and not inputs.sender_is_org_member
    assert not any("/members/" in p for p in gh.paths)


def test_user_owned_repo_only_trusts_the_owner(rsa_key_pem):
    gh = FakeGitHub()
    app = make_app(rsa_key_pem, gh)
    owner_job = parse_workflow_job(octopus_job(), "d1")  # sender Codertocat owns the repo
    assert owner_job.org is None and owner_job.owner == "Codertocat"
    assert resolve_trust_inputs(owner_job, app).sender_is_org_member
    other = parse_workflow_job(octopus_job() | {"sender": {"login": "mallory"}}, "d2")
    assert not resolve_trust_inputs(other, app).sender_is_org_member
    assert not any("/orgs/" in p for p in gh.paths)


def test_installation_looked_up_when_payload_has_none(rsa_key_pem):
    gh = FakeGitHub()
    payload = octopus_job()
    del payload["installation"]
    org_payload = load_fixture("workflow_job.completed.failure.with-organization.json")
    payload["organization"] = org_payload["organization"]
    payload["repository"] = org_payload["repository"]
    resolve_trust_inputs(parse_workflow_job(payload, "d1"), make_app(rsa_key_pem, gh))
    assert f"/repos/{REPO}/installation" in gh.paths
    assert "/app/installations/99/access_tokens" in gh.paths
