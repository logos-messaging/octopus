# JIT runner config generation and orphan cleanup
**Labels:** coordinator, github

## Tasks
- `generate-jitconfig` call per assigned job (repo or org scope), labels `self-hosted, octopus, <os>, <arch>`, runner group, unique runner name.
- Handle API errors/rate limits with backoff; map failures to `FailureReason.JITCONFIG_ERROR`.
- Orphan cleanup: runners registered but never started (offer timeout, runner lost) are removed via the runners API; periodic sweep for stale `octopus-*` runners.
- Never log the encoded JIT config.

## Acceptance
- Against a GitHub API stub: success, 4xx, 5xx and rate-limit paths behave as specified; orphans are deleted.

## Depends on
06.
