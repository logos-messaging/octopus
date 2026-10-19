from octopus.policy import JobContext, TrustPolicy

POLICY = TrustPolicy(allowed_repos=frozenset({"acme/app"}), allowed_orgs=frozenset({"acme-labs"}))


def job(repo="acme/app", head="acme/app", member=True):
    return JobContext(repo=repo, head_repo=head, sender_is_org_member=member)


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
