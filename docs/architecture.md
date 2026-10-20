# Octopus architecture

## Roles
- **Runner** (laptop): `octopus runner start`. Announces presence, accepts job offers that pass the trust policy, runs the official `actions/runner` with a JIT config, streams logs and status.
- **Coordinator** (VPS, public IP): `octopus coordinator start`. Runs a logos-delivery Core node (entry node and relay for NAT'd runners), receives GitHub webhooks, schedules jobs, keeps the job lifecycle and serves logs. Several coordinators run for failover.

## GitHub integration
A GitHub App receives `workflow_job` webhooks (`queued`). The coordinator picks an idle runner, requests an ephemeral JIT runner config (`POST /repos/{o}/{r}/actions/runners/generate-jitconfig`, label `octopus`) and sends it over the encrypted job channel to that runner only. The runner starts `actions/runner --jitconfig`; it runs one job and exits. Workflows opt in with `runs-on: [self-hosted, octopus]`.

## Trust policy
Only trusted internal PRs: the repo or org is on the allowlist, the PR branch is in the same repo (no forks) and the sender is an org member. Enforced twice: by the coordinator before offering, and by the runner before accepting.

## Messaging (content topics under `/octopus/1/`)
| Topic | Purpose |
|---|---|
| `presence` | runner announces (os, arch, labels, capacity), heartbeat |
| `jobs-<runnerId>` | coordinator to runner: encrypted job offer; runner to coordinator: ack and status. Reliable channel |
| `logs-<jobId>` | chunked, sequence-numbered log stream. Reliable channel |
| `coord` | coordinator-to-coordinator gossip (assignment locks, ownership) |

All code talks to a `Transport` interface; `LogosTransport` wraps `logosdelivery.NodeWrapper`, `MemoryTransport` is the test fake.

## Job lifecycle and diagnosis
`queued -> assigned -> offered -> acked -> runner-started -> step N -> finished`, each with a machine-readable `FailureReason` (`no-idle-runner`, `offer-timeout`, `jitconfig-error`, `runner-download-failed`, `policy-rejected`, `runner-lost`, `job-failed`). `octopus explain <job>` prints the timeline, the reason and the last log lines; `octopus logs <job> -f` follows the log stream.

## Job channel and keys
Offers travel over a logos-delivery reliable channel (SDS) with a per-channel cipher, not plain publish. One channel per (coordinator, runner) pair: `octopus-jobs-<coordinatorId>-<runnerId>` on `jobs-<runnerId>`.

- Each node has an X25519 key (`octopus.crypto`). Its id is derived from the public key (`node_id`), so a forged presence cannot claim another runner's id; the registry drops presences whose id does not match their `pubkey`.
- The channel key is `HKDF(X25519(own, peer), info = channel_id)`; the cipher is ChaCha20-Poly1305 with a random nonce per segment (`nonce || ct || tag`), because the library decrypts each segment on its own. Only the two key holders can encrypt or decrypt, so it also authenticates the coordinator to the runner. A message that fails to decrypt is dropped.
- A runner accepts offers only from coordinators it was told to trust (`id -> public key`, id must match the key). Distribution of those keys is #38.
- The channel confirms delivery; `JobAck` carries accept/reject and the `FailureReason`. `expires_at` and a per-offer `nonce` give expiry and replay protection (the cipher gives no freshness).
- The cipher callbacks are cffi function pointers called inline on the library event loop: they only transform bytes and never log.
