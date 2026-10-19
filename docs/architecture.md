# Octopus architecture

## Roles
- **Runner** (laptop): `octopus runner start`. Announces presence, accepts job offers that pass the trust policy, runs the official `actions/runner` with a JIT config, streams logs and status.
- **Coordinator** (VPS, public IP): `octopus coordinator start`. Runs a logos-delivery Core node (entry node and relay for NAT'd runners), receives GitHub webhooks, schedules jobs, keeps the job lifecycle and serves logs. Several coordinators run for failover.

## GitHub integration
A GitHub App receives `workflow_job` webhooks (`queued`). The coordinator picks an idle runner, requests an ephemeral JIT runner config (`POST /repos/{o}/{r}/actions/runners/generate-jitconfig`, label `octopus`) and sends it, encrypted for that runner only. The runner starts `actions/runner --jitconfig`; it runs one job and exits. Workflows opt in with `runs-on: [self-hosted, octopus]`.

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
