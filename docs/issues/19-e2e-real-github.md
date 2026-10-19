# Nightly end-to-end test against a real GitHub test repo
**Labels:** testing

## Tasks
- A dedicated test repo with a sample workflow `runs-on: [self-hosted, octopus]`, a test GitHub App, and secrets stored in CI.
- Nightly job: start a coordinator and a runner, trigger the workflow, assert success, runner deregistration, and log availability through `octopus logs`.
- Also a failing workflow: assert `explain` reports `job-failed` with the failing step.

## Acceptance
- Nightly green; failures open an issue/notify.

## Depends on
06, 07, 08, 12, 14, 16.
