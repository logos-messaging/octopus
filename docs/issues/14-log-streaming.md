# Job log capture and streaming
**Labels:** runner, coordinator, transport

## Tasks
- Runner: capture runner stdout/stderr and step markers, publish ordered `LogChunk`s (size-bounded, `seq`, `final`) on `logs-<jobId>`; backpressure and buffering when offline; resume from last acked `seq`.
- Coordinator: persist chunks (bounded retention), de-duplicate, detect gaps, serve ranges.
- Mask secrets (the JIT config and GitHub-masked values) before sending.

## Acceptance
- Logs arrive complete and in order after a dropped connection; a 50 MB log does not exhaust memory.

## Tests
Unit and fault-injection tests over `MemoryTransport`.

## Depends on
01, 09.
