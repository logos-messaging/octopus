# Trust policy enforcement and `octopus runner allow`
**Labels:** runner, coordinator, security

## Context
Octopus serves only trusted internal-team PRs. `policy.py` has the pure check; it must be wired in.

## Tasks
- Persistent allowlist (repos, orgs) in the runner config; `octopus runner allow|deny|list`.
- Enforce in the coordinator (before offering) and again in the runner (before accepting; never trust the offer).
- Fork PRs, non-members and unknown repos yield `policy-rejected` with a clear reason in `explain`.
- Consider `pull_request_target`/untrusted-input pitfalls; document the threat model and what the runner does not protect against.

## Acceptance
- A forged offer for a non-allowed repo is rejected by the runner.

## Tests
Extend `tests/test_policy.py`; offer-rejection integration test.

## Depends on
03, 06.
