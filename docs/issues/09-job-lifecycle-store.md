# Job lifecycle store with failure reasons
**Labels:** coordinator

## Tasks
- State machine: `queued -> assigned -> offered -> acked -> runner-started -> running(step) -> finished`, plus terminal failures with a `FailureReason` and detail text. Reject illegal transitions.
- Append-only event timeline per job (timestamps, actor, detail) so `explain` can print exactly what happened.
- Storage behind an interface; SQLite implementation with retention (default 30 days).
- Query API: get job, list jobs (filter failed/running/repo), timeline.

## Acceptance
- Every terminal path in the scheduler records a reason; the timeline can be reconstructed after a restart.

## Tests
State-machine unit tests; SQLite persistence tests.

## Depends on
None.
