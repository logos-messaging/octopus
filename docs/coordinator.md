# Running a coordinator

A coordinator is one process on a host with a public IP: the logos-delivery node runners
connect to, the GitHub webhook endpoint, the scheduler, and the store of jobs and their
logs.

```bash
octopus coordinator start --env-file coordinator.env
```

It prints what runners need, then runs in the foreground until Ctrl-C (or SIGTERM):

```
coordinator     k3v7...
public key      Zt4f...
                runners: octopus runner setup --coordinator Zt4f...
entry node      /ip4/203.0.113.7/tcp/60000/p2p/16Uiu2...
webhook         http://127.0.0.1:8080/github/webhook (public: https://octopus.example.org/github/webhook)
allowed         acme/app  (auth: token)
runners online  0
```

## Settings

`OCTOPUS_*` variables, from the environment over `--env-file`. `octopus github setup`
writes the token-mode ones ([github-setup.md](github-setup.md)).

| Variable | |
|---|---|
| `OCTOPUS_WEBHOOK_SECRET` | required; `OCTOPUS_WEBHOOK_SECRET_PREVIOUS` is accepted too during a rotation |
| `OCTOPUS_GITHUB_TOKEN` | token mode: a repo-scoped fine-grained token (Administration: write, Actions: read, Metadata: read) |
| `OCTOPUS_GITHUB_APP_ID`, `OCTOPUS_GITHUB_APP_KEY_FILE` | GitHub App mode instead ([github-app.md](github-app.md)); the key is checked at start |
| `OCTOPUS_GITHUB_REPO`, `OCTOPUS_ALLOWED_REPOS`, `OCTOPUS_ALLOWED_ORGS` | what may run (comma-separated); at least one, or the coordinator refuses to start |
| `OCTOPUS_PUBLIC_IP` | required: the address runners reach the node at |
| `OCTOPUS_TCP_PORT` | node port (default 60000); open it in the firewall |
| `OCTOPUS_ENTRY_NODES` | other coordinators' multiaddrs |
| `OCTOPUS_WEBHOOK_LISTEN` | webhook HTTP address, default `127.0.0.1:8080` (or `--listen`); put a TLS proxy in front |
| `OCTOPUS_WEBHOOK_URL` | the public webhook URL, shown in the output |
| `OCTOPUS_GITHUB_API_URL` | GitHub Enterprise Server: `https://HOST/api/v3` |

Data lives in `$OCTOPUS_DATA_DIR/coordinator/` (`--data-dir` for another root): the node
key `coordinator.key` (its public half is what runners trust, so keep the file), `jobs.db`,
`logs.db` (SQLite, 30 days) and `coordinator.state.json` while running.

Exit codes: 1 already running, 2 usage, 3 bad settings, 4 network, library or port failure.

## What it does with a job

1. GitHub's `workflow_job` `queued` webhook is verified and answered at once.
2. A worker checks trust through the GitHub API (fork, trigger, sender: org member with
   the App, write access with a token), retrying 3 times. If GitHub cannot answer, the job
   fails closed as `policy-rejected` with the error in its timeline.
3. The scheduler offers the job to an idle matching runner, with a just-in-time runner
   config, over the encrypted job channel, and listens for its log.
4. The runner's statuses and log come back; the job ends `finished` or `failed` with a
   reason.

A `completed` webhook for a job still waiting for a runner (e.g. the run was cancelled)
fails it as `cancelled`, so no runner is started for a job GitHub will not hand out.
Orphaned `octopus-*` runners of recently seen repos are swept every 5 minutes.

## Inspecting jobs

On the coordinator host (they read the databases read-only, so they work whether or not
the coordinator runs):

```bash
octopus coordinator status          # running?, key, entry nodes, runners online, job counts
octopus jobs [--failed|--running] [--repo acme/app] [--limit 50]
octopus explain <job>               # timeline, why it failed, end of the log
octopus logs <job> [-f]             # the whole log; -f follows it until the job is over
```

All take `--json`. A job id is GitHub's workflow job id. `explain` names the failure
reason, the detail recorded with it and a one-line hint, for example:

```
job      28978245  acme/app
state    failed (job-failed), updated 2m ago
runner   k2b9xq1m  step: build
why      job-failed: exit code: 1
         the job ran and failed: see the log

timeline
  2026-10-20 10:00:00  +   0.0s  queued          scheduler
  ...
log      runner k2b9xq1m, complete
  | assertion failed: 1 != 2
```

## Limits

- Jobs queued on GitHub while the coordinator was down are not picked up when it comes
  back: redeliver them from the App's or webhook's Recent Deliveries. Jobs the coordinator
  was working on when it stopped stay in their last state in the store.
- Failover needs `OCTOPUS_COORDINATOR_PEERS` on every coordinator (see coordinator-ops.md); without it a coordinator runs alone. A partition can make two coordinators offer the same job (no fencing yet).
- Inspection is local to the coordinator host.
