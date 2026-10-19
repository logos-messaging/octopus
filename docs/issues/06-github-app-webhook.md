# GitHub App and `workflow_job` webhook receiver
**Labels:** coordinator, github

## Tasks
- Document GitHub App creation: permissions (Actions: read/write, Administration: read/write on repo or org for self-hosted runners, Metadata: read), webhook secret, events (`workflow_job`).
- HTTP endpoint in the coordinator with `X-Hub-Signature-256` verification (constant-time), body size limits, idempotency by delivery id.
- App authentication: JWT then installation token, with caching and refresh.
- Parse `workflow_job` `queued/in_progress/completed` into internal events; ignore jobs without the `octopus` label.
- Extract trust-policy inputs: repo, PR head repo (fork or not), sender and whether they are an org member.

## Acceptance
- Invalid signature is rejected; duplicate deliveries are processed once; fixtures from real GitHub payloads parse.

## Tests
Unit tests with recorded payload fixtures; signature tests.

## Depends on
None.
