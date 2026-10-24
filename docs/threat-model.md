# Threat model

Octopus runs GitHub Actions jobs on team members' laptops, coordinated over logos-delivery. Scope is **trusted internal-team PRs only**; it is not a public runner pool. This page says what is protected, what is not, and what to do about the rest. "Today" means what is on `main`; "planned" links the issue.

## Assets
- **The laptop and the lender's data**: jobs run arbitrary code as the runner user.
- **The JIT runner config**: a bearer credential that registers a runner on a repo.
- **Coordinator secrets**: webhook secret, GitHub App private key or fine-grained token (`coordinator.env`).
- **Repo secrets and the repo itself**: reachable from the job's `GITHUB_TOKEN` and any workflow secrets.
- **Job logs**: may contain anything the job printed.

## Actors
| Actor | Trusted for |
|---|---|
| Org member / repo writer | Running code on any allowed runner (by design) |
| Lender (runner owner) | Choosing which repos to serve |
| Coordinator operator | Scheduling and handing out JIT configs |
| Network peers | Nothing |
| GitHub | Webhooks and runner registration |

## Threats

### 1. Running team PR code on the laptop
- **Today**: the trust policy runs only same-repo `push`/`pull_request` jobs from org members (see "Trust policy"). Each job runs in its own work dir, which is deleted afterwards, and the process tree is killed on timeout or stop.
- **Residual**: without isolation the job runs as the lender's user, so it can read the lender's files, use their network and persist things outside the work dir: a compromised or malicious member's PR owns the laptop. Decentralized-CI nodes can isolate jobs: a container on Linux, or the **light level** on Linux and macOS: a dedicated non-admin `octopus` account in a sandbox (a bubblewrap mount namespace on Linux, a Seatbelt profile on macOS; see `docs/decentralized-ci.md`). The light level keeps the lender's files, keys, keychain and GitHub login out of reach and wipes the job's home every time, but it shares the host kernel (a kernel or sandbox bug could escape it), cannot block the LAN selectively (and on Linux leaves `localhost` services reachable unless the network is off), and a node that declines still runs jobs as the lender. Jobs that need signing secrets are not supported on nodes.
- **Harden**: run `octopus runner start` as a dedicated user, in a VM or in a container; keep no credentials, SSH keys or browser sessions on that machine; restrict network egress to GitHub, package registries and the coordinator.

### 2. Secret exposure
- **JIT config**: travels only inside the encrypted job channel to one runner; held as a `SecretStr`, passed to `actions/runner` through the `ACTIONS_RUNNER_INPUT_JITCONFIG` environment variable (not argv, so not in `ps`) and scrubbed from output, errors and logs.
- **Logs**: the log streamer masks the JIT config and values a workflow registers with `::add-mask::`. Chunks travel over an encrypted channel and are spooled to a mode 0600 file on the runner.
- **Files at rest**: node key, allowlist, runner settings and `coordinator.env` are written with mode 0600. A corrupt allowlist is an error, never an open door.
- **Residual**: a secret that a job prints without registering a mask can appear in the coordinator's log store (kept 30 days, capped at 100 MiB per stream), and anyone with read access to the coordinator host can read stored logs.
- **Harden**: use a fine-grained token limited to the served repos, or a GitHub App with only the permissions listed in [github-app.md](github-app.md); rotate webhook secrets (several can be configured at once); keep `coordinator.env` and the App `.pem` readable only by the coordinator user.

### 3. Malicious org member
Trusted by definition: any org member (or, for a repo set up with a token, anyone who can push to it) can run code on every runner that allows the repo.
- **Harden**: keep the allowlist narrow (prefer `org/name` over a whole org), use branch protection and required review for workflow files, and treat each laptop as disposable (item 1).

### 4. Fork PRs and unsafe triggers
Rejected: the head repo must equal the repo, and only `push` and `pull_request` are served. `pull_request_target` and `workflow_run` run in the base repo's context with its secrets and are refused even for same-repo senders. Bots (dependabot, ...) are never org members. Names are compared case-folded.

### 5. Compromised coordinator
- **Can**: lie about fork, sender or trigger (the runner cannot verify them), so it can get a job accepted for any repo on a lender's allowlist; read the job logs it receives; drop or delay jobs; learn presence and timing.
- **Cannot**: reach repos the lender did not allow (the runner re-checks its allowlist) or impersonate a coordinator the runner was not told to trust.
- **Harden**: narrow allowlists; run coordinators on dedicated hosts with minimal access; restrict the App or token to the served repos.

### 6. Impersonation and key distribution
- **Today**: node ids are derived from the X25519 public key, so a forged presence cannot claim another runner's id (the registry drops presences whose id does not match their key). A runner accepts offers only from coordinators it was told to trust (`id -> public key`, id must match the key) and drops offers addressed to another party. Each pair of nodes derives its own channel key (`HKDF(X25519(own, peer))`), so a node that can decrypt is also authenticated.
- **Residual**: coordinator keys are passed to runners by hand (`octopus runner setup --coordinator <key>`); verifying them out of band is on the operator.
- **Planned**: signed coordinator adverts and a pinned root key (#38).

### 7. Replay and tampering of offers
Offers carry `expires_at` and a random per-offer `nonce`. A runner remembers recent `(job_id, nonce)` pairs and ignores repeats, and rejects expired offers. The cipher is authenticated (ChaCha20-Poly1305): tampered messages fail to decrypt and are dropped. The cipher gives no freshness, which is why expiry and nonce exist.
- **Residual**: the replay cache is in memory and bounded, so it resets when the runner restarts. A replayed offer within its TTL (default 30 s) must still pass the policy, and JIT configs are single use at GitHub.

### 8. Webhook forgery
Deliveries need a valid `X-Hub-Signature-256` (constant-time comparison); invalid ones are rejected. Put the endpoint behind TLS.

### 9. Metadata leaked to peers
logos-delivery gossip is visible to its participants. Peers and coordinators see presence (runner id, OS, arch, labels, capacity, busy count), message timing and sizes, and the runner's IP address for the peers it connects to. Content (offers, logs) is encrypted. GitHub itself sees the runner's IP and every job, because `actions/runner` talks to GitHub directly. Octopus does not hide the lender from GitHub or from the coordinator.

### 10. Availability and abuse
Malformed or undecryptable messages are dropped. A member can queue many jobs to keep lenders' machines busy; `--max-jobs` bounds concurrency per runner (`unlimited` removes that bound, so a member could then run as many jobs at once as the laptop can start) and the maximum job duration bounds time. Rate limiting of peers and members is not implemented.

### 11. OIDC pick mode
Pick mode ([oidc-pick.md](oidc-pick.md)) stores no GitHub credential on the coordinator: a stolen coordinator host yields no way to act on GitHub. Its costs: runners are persistent and run jobs as the owner's user, so one job can affect the next (no one-use isolation); GitHub assigns a job by label, so a writer of an allowed repo can target a runner label directly and skip the pick (the boundary is who can push to the allowed repos); `/pick` is a public endpoint, guarded by the OIDC signature, audience, expiry and one-time `jti`, and by claims-based trust (repo, event, workflow in the same repo); fork PRs get no OIDC token and cannot ask. A pick can reserve a runner for up to 10 minutes without using it, so an allowed repo's writer can hold runners; there is no per-repo rate limit.

### 12. Decentralized CI
Decentralized CI ([decentralized-ci.md](decentralized-ci.md)) stores no GitHub credential anywhere and registers no runners. The trust boundary is that of GitHub: whoever can push to an allowed repo can run code on a team machine as its owner, in a fresh directory but without a sandbox, and a job can read the owner's GitHub login (the `gh` token and keychain) and use their SSH agent: only `GITHUB_TOKEN`, `GH_TOKEN` and `SSH_AUTH_SOCK` are removed from its environment. `octopus ci init` says so and asks the owner to confirm. Membership is a chain: team key -> signed member list (or a 30-day delegation for one scope; a list only counts for the repos inside its scope) -> GitHub login -> SSH signature of the node's keys (checked against the public `github.com/<login>.keys`). The team key is encrypted at rest and used only by admin commands. Weak points: a member node can post a false commit status (it shows its login); a removed member keeps access until the next sync (lists live at most 3 days); the signed OIDC token travels on an open topic, but its audience binds the OS and the reply key and it is accepted once per node; partitions can run a leg twice (idempotent by commit and OS). Fork PRs get no OIDC token and are never run.

## Trust policy
Only jobs that pass every check run: repo or org on the runner's allowlist, PR branch in the same repo, sender is an org member (with token auth: can push to the repo), trigger is `push` or `pull_request`. Checked by the coordinator before offering and again by the runner before accepting. Everything unknown fails closed (no context, no allowlist, unreadable config). Widen the allowed events only knowingly, via `TrustPolicy(allowed_events=...)`.

## Operating guidance
- Start with `octopus runner allow org/name` for the specific repos you want to serve; `octopus runner list` shows what is allowed. A runner with an empty allowlist rejects every job.
- Treat a lent laptop as a CI machine, not your daily driver (item 1).
- `coordinator.env` (written by `octopus github setup`) holds the webhook secret and the coordinator's token: mode 0600, coordinator only. The `gh` login is never written to it.
- Report security issues privately to the maintainers rather than in a public issue.
