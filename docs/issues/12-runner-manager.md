# Runner manager: fetch, verify and run `actions/runner`
**Labels:** runner

## Tasks
- Download the pinned `actions/runner` release for the OS/arch, verify checksum, cache under the Octopus data dir, update on version bump.
- Start with `./run.sh --jitconfig <cfg>` (`run.cmd` on Windows), isolated work directory per job, cleaned afterward (also on crash).
- Capture exit code, enforce a max job duration, kill the process tree on stop.
- Map failures: download failure, start failure, runner exit non-zero with step info into `FailureReason`.

## Acceptance
- On Linux, macOS and Windows a runner process starts with a fake/real JIT config and is cleaned up afterwards.

## Tests
Unit tests with a fake runner executable; manual E2E against a test repo.

## Depends on
None (consumes a JIT config string).
