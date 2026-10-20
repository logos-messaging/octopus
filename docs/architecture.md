# Octopus architecture

## Roles
- **Runner** (laptop): `octopus runner start`. Announces presence, accepts job offers that pass the trust policy, runs the official `actions/runner` with a JIT config, streams logs and status.
- **Coordinator** (VPS, public IP): `octopus coordinator start`. Runs a logos-delivery Core node (entry node and relay for NAT'd runners), receives GitHub webhooks, schedules jobs, keeps the job lifecycle and serves logs. Several coordinators run for failover.

## GitHub integration
A GitHub App receives `workflow_job` webhooks (`queued`). The coordinator picks an idle runner, requests an ephemeral JIT runner config (`POST /repos/{o}/{r}/actions/runners/generate-jitconfig`, label `octopus`) and sends it over the encrypted job channel to that runner only. The runner starts `actions/runner --jitconfig`; it runs one job and exits. Workflows opt in with `runs-on: [self-hosted, octopus]`. Setup and webhook handling: [github-app.md](github-app.md).

## Trust policy
Only trusted internal PRs: the repo or org is on the allowlist, the PR branch is in the same repo (no forks), the sender is an org member and the trigger is `push` or `pull_request` (`pull_request_target`, `workflow_run` and the like run with base-repo secrets and can check out attacker-controlled code). An empty allowlist rejects everything; names compare case-insensitively.

Enforced twice (`octopus.policy.TrustPolicy`):
- **Coordinator**, before offering: `Scheduler.submit(..., context)` checks the job; a denied job (or one without a context) fails as `policy-rejected` with the reason in its timeline and is never offered.
- **Runner**, before accepting: `make_offer_handler` re-checks every offer against the lender's own allowlist (`octopus runner allow|deny|list`, stored in `runner.json` in the platform config dir). The runner cannot verify fork, sender or trigger itself, so it takes them from the offer; only a pinned coordinator can deliver one over the encrypted channel. The allowlist stays the lender's local decision, so a compromised coordinator can only reach what the lender allowed. The rejection detail travels back in `JobAck.detail` and shows up in `octopus explain`.

See `docs/threat-model.md`.

## Messaging (content topics under `/octopus/1/`)
| Topic | Purpose |
|---|---|
| `presence` | runner announces (os, arch, labels, capacity), heartbeat |
| `jobs-<runnerId>` | coordinator to runner: encrypted job offer; runner to coordinator: ack and status. Reliable channel |
| `logs-<jobId>` | runner to coordinator: chunked, sequence-numbered, encrypted log stream; coordinator to runner: acks. Reliable channel |
| `coord` | coordinator-to-coordinator gossip (assignment locks, ownership) |

All code talks to a `Transport` interface; `LogosTransport` wraps `logosdelivery.NodeWrapper`, `MemoryTransport` is the test fake.

## Job lifecycle and diagnosis
`queued -> assigned -> offered -> acked -> runner-started -> step N -> finished`, each with a machine-readable `FailureReason` (`no-idle-runner`, `offer-timeout`, `jitconfig-error`, `runner-download-failed`, `runner-start-failed`, `policy-rejected`, `runner-lost`, `job-failed`). `octopus explain <job>` prints the timeline, the reason and the last log lines; `octopus logs <job> -f` follows the log stream.

## Job channel and keys
Offers travel over a logos-delivery reliable channel (SDS) with a per-channel cipher, not plain publish. One channel per (coordinator, runner) pair: `octopus-jobs-<coordinatorId>-<runnerId>` on `jobs-<runnerId>`.

- Each node has an X25519 key (`octopus.crypto`). Its id is derived from the public key (`node_id`), so a forged presence cannot claim another runner's id; the registry drops presences whose id does not match their `pubkey`.
- The channel key is `HKDF(X25519(own, peer), info = channel_id)`; the cipher is ChaCha20-Poly1305 with a random nonce per segment (`nonce || ct || tag`), because the library decrypts each segment on its own. Only the two key holders can encrypt or decrypt, so it also authenticates the coordinator to the runner. A message that fails to decrypt is dropped.
- A runner accepts offers only from coordinators it was told to trust (`id -> public key`, id must match the key). Distribution of those keys is #38.
- The channel confirms delivery; `JobAck` carries accept/reject and the `FailureReason`. `expires_at` and a per-offer `nonce` give expiry and replay protection (the cipher gives no freshness).
- The cipher callbacks are cffi function pointers called inline on the library event loop: they only transform bytes and never log.

## Job logs
A job's output streams from the runner to the coordinator that offered it, on its own encrypted channel (`log_channel_id(coordinator, runner, job)` on `logs-<jobId>`, keyed like the job channel).

- **Runner** (`octopus.agent.LogStreamer`): takes the runner's output line by line (`RunnerManager.run_job(on_output=streamer.write)`), masks secrets (the JIT config, and values a workflow registers with `::add-mask::`), and packs lines into `LogChunk`s of at most 16 KiB (`MAX_LOG_CHUNK_BYTES`, well under the transport's message limit even when JSON-escaped), sealed when full or after 1 s. Chunks are appended to a spool file (mode 0600, removed on close) and only their offsets stay in memory, so a long log or a long outage costs disk, not memory; the spool is capped (256 MiB, then a truncation note).
- **Delivery**: at most `window` (16) chunks are unacknowledged. The coordinator answers each chunk with `LogAck(seq)`, the highest seq up to which it has everything; if no ack advances for `resend_after_s` (10 s) the runner resends from there (go-back-N). Lost chunks, lost acks, duplicates, reordering, a dropped connection and a coordinator restart all recover this way. `close()` sends a `final` chunk and waits (default 5 min) until the coordinator has the whole log.
- **Coordinator** (`octopus.coordinator.LogCollector`, `LogStore`): the scheduler calls `watch(job, runner)` just before the offer goes out (so the first chunk finds an open channel) and `unwatch` when the offer is refused or the runner is lost. Chunks go to SQLite keyed by (job, runner, seq): repeats are ignored, gaps are tracked, and a job retried on another runner gets a separate stream (readers get the newest unless they ask). After a restart, `watch` re-sends the stored position so the runner resumes there. `read(job, start, end)` serves ranges in batches and `tail(job, n)` the last lines for `octopus explain`. Streams are capped (100 MiB) and kept 30 days.
- `actions/runner` prints job boundaries on its console (`Running job: <name>`, `Job <name> completed with result: <result>`); `LogStreamer(on_marker=...)` reports them. Step output is uploaded to GitHub by the runner's worker process; streaming it too (from the worker's `_diag` pages) is a possible follow-up.

## Runner manager
`octopus.agent.runner.RunnerManager` (runner hosts) downloads the pinned `actions/runner` release (`RUNNER_VERSION`, checksums in `release.py`, refreshed with `scripts/update_runner_version.py`), verifies SHA-256 and extracts it safely into `$OCTOPUS_DATA_DIR/runner/<version>/`. Each job runs in a fresh copy of that tree (`bin/` and `externals/` hard-linked), in its own process group, with a maximum duration; the whole process tree is killed on timeout or `stop()` and the job directory is always removed. The JIT config goes to the runner through `ACTIONS_RUNNER_INPUT_JITCONFIG` (not argv, so it is not visible in `ps`) and is scrubbed from output, errors and logs. `run_job` returns a `RunResult` with a `FailureReason`: `runner-download-failed`, `runner-start-failed`, `job-failed` (non-zero exit or timeout), `runner-lost` (stopped).

## Runner agent and CLI
`octopus runner start` runs `RunnerAgent` in the foreground: it loads (or creates) the node key at `$OCTOPUS_DATA_DIR/node.key`, starts the transport, announces presence (`capacity = --max-jobs`) and opens an `OfferReceiver` for the coordinators in the saved settings. Every offer goes through `make_offer_handler(allowlist, ...)`; an accepted offer only reserves a slot (the ack is sent right after the handler returns) and the job runs on a worker thread with its own `RunnerManager`. Over the limit the offer is rejected with `no-idle-runner`. `JobStatus` (`runner-started`, `finished` with exit code and `FailureReason`) goes back on the job channel. The agent writes `runner.state.json` (pid, runner id, busy) in the data dir; `status` and `stop` read it and signal the pid (`stop` uses SIGTERM, so on Windows it is best-effort). Settings (`<config>.settings.json`) hold entry nodes, trusted coordinator public keys, labels, max jobs, port and NAT. Detaching and OS service units are #24.
