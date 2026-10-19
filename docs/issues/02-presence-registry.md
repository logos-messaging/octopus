# Presence announcements and runner registry
**Labels:** transport, coordinator, runner

## Context
Coordinators must know which runners are online, idle and suitable for a job.

## Tasks
- Runner side: publish `Presence` (os, arch, labels, capacity, busy) on `PRESENCE_TOPIC` at startup and every N seconds (configurable, default 30s) and on state change.
- Coordinator side: `Registry` consuming presence, tracking last-seen, marking a runner offline after 3 missed heartbeats, exposing `idle_runners(labels, os, arch)`.
- Handle clock skew (do not trust `sent_at` for liveness; use local receive time) and duplicate/replayed presence.
- Thread-safe, in-memory, with an interface that allows persistence later.

## Acceptance
- Registry reflects join, leave (timeout) and busy/idle changes within 2 heartbeat intervals.

## Tests
Unit tests with `MemoryTransport` and a fake clock; property: no runner reported idle after timeout.

## Depends on
01 (for real transport; unit tests only need `MemoryTransport`).
