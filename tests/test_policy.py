from octopus.policy import JobContext, TrustPolicy

POLICY = TrustPolicy(allowed_repos=frozenset({"acme/app"}), allowed_orgs=frozenset({"acme-labs"}))


def job(repo="acme/app", head="acme/app", member=True, event="pull_request"):
    return JobContext(repo=repo, head_repo=head, sender_is_org_member=member, event=event)


def test_same_repo_member_allowed():
    assert POLICY.check(job())[0]


def test_org_allowlist():
    assert POLICY.check(job(repo="acme-labs/x", head="acme-labs/x"))[0]


def test_unknown_repo_rejected():
    assert not POLICY.check(job(repo="evil/app", head="evil/app"))[0]


def test_fork_rejected():
    ok, reason = POLICY.check(job(head="stranger/app"))
    assert not ok and "fork" in reason


def test_non_member_rejected():
    assert not POLICY.check(job(member=False))[0]


def test_names_are_case_insensitive():
    assert POLICY.check(job(repo="Acme/App", head="acme/APP"))[0]
    assert POLICY.check(job(repo="ACME-Labs/X", head="acme-labs/x"))[0]


def test_empty_allowlist_rejects_everything():
    ok, reason = TrustPolicy().check(job())
    assert not ok and "not allowed" in reason


def test_dangerous_events_rejected_even_for_same_repo_members():
    for event in ("pull_request_target", "workflow_run"):
        ok, reason = POLICY.check(job(event=event))
        assert not ok and event in reason


def test_denied_events_are_configurable():
    relaxed = TrustPolicy(allowed_repos=frozenset({"acme/app"}), denied_events=frozenset())
    assert relaxed.check(job(event="pull_request_target"))[0]
