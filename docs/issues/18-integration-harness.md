# Integration test harness and NAT test
**Labels:** testing

## Tasks
- In-process harness: N runners + 2 coordinators on `MemoryTransport` and a GitHub API/webhook stub.
- Scenarios: single assignment, ack timeout then reassign, runner vanishes mid-job, coordinator failover, duplicate webhook, forged offer, fork PR, log completeness after reconnect; assert the failure reason shown by `explain`.
- Real-transport variant (`needs_lib`) with local logos-delivery nodes.
- docker-compose with separate networks to force the relay path (see 04).

## Acceptance
- Harness runs in CI; each scenario documented in the test name/docstring.

## Depends on
08, 09, 10, 14 (incrementally).
