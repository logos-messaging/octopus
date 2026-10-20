# Threat model: trust policy

Octopus runs CI jobs on team members' laptops. This covers what the trust policy protects against and what it does not. It is the starting point for the wider threat model (#27).

## What the policy enforces
Only jobs that pass every check run: repo or org on the runner's allowlist, PR branch in the same repo, sender is an org member, trigger not in the denied events. Checked by the coordinator before offering and again by the runner before accepting. Everything unknown fails closed (no context, no allowlist, unreadable config).

## Pitfalls handled
- **Fork PRs** run attacker code. Rejected: the head repo must equal the repo (an unknown head repo, e.g. a deleted fork, never matches).
- **`pull_request_target` / `workflow_run`** run in the base repo's context with its secrets, and workflows often check out the PR head. Denied by default (`DEFAULT_DENIED_EVENTS`), even for same-repo senders. Relax only knowingly via `TrustPolicy(denied_events=...)`.
- **Bots** (dependabot, ...) are never org members.
- **Case**: GitHub names are case-insensitive; the policy compares case-folded.
- **Forged offers**: the runner re-checks, so an offer for a non-allowed repo is rejected (`policy-rejected`) even if a coordinator skipped its own check.

## What it does not protect against
- **A trusted member's malicious or compromised PR**: the code runs on the laptop. Use a dedicated user, VM or container, keep no secrets on the machine and limit network egress.
- **A compromised coordinator** can lie about fork, sender or trigger (the runner cannot verify them), so it can get a job accepted for any repo on the lender's allowlist. It cannot reach repos the lender did not allow. Keep the allowlist narrow (prefer `org/name` over a whole org).
- **A malicious org member** is trusted by definition.
- **Metadata**: peers and coordinators see presence and timing.

## Operating guidance
- Start with `octopus runner allow org/name` for the specific repos you want to serve; `octopus runner list` shows what is allowed.
- A runner with an empty allowlist rejects every job.
- The allowlist file is mode 0600; a corrupt file is an error, never an open door.
