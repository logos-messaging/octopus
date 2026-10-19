# Scheduler: assign queued jobs to runners
**Labels:** coordinator

## Tasks
- Match a queued job to an idle runner by labels, OS and arch; fair choice (least recently used) among candidates.
- Offer flow: reserve runner in registry, send offer, wait for ack up to `offer_ttl_s`, on timeout/reject release and try the next; give up with `no-idle-runner` after a configurable queue timeout.
- No double assignment: a job id is assigned at most once; a runner holds at most `capacity` jobs.
- Re-queue when a runner disappears mid-job (`runner-lost`), subject to a retry cap.

## Acceptance
- Property/stress tests: N jobs, M runners, random drops/timeouts; every job ends in exactly one terminal state, no double assignment.

## Tests
Pure unit tests with fake registry, `MemoryTransport` and fake clock.

## Depends on
02, 03, 09.
