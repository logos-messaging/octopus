# Decentralized CI: team machines run the pipeline over logos-delivery

Status: implemented and tested with in-memory transport and a fake GitHub. The logos part was
measured once from a GitHub-hosted runner (phase 0 below); **the whole chain has not run against
real GitHub OIDC yet**. Design
and discussion: issue #78.

No GitHub credential on a server, no coordinator, no runner registration (so no admin rights per
machine). Team machines run the **Linux and macOS** legs of a workflow; **Windows always runs on
GitHub-hosted runners**; any leg no machine takes **falls back** to a hosted runner.

## The pipeline in your repo

```yaml
# .github/workflows/ci.yml
on: [push, pull_request]
jobs:
  ci:
    uses: logos-messaging/octopus/.github/workflows/octopus-ci.yml@<commit sha>   # pin a commit
    permissions: { id-token: write, contents: read }
```

```bash
# .octopus/ci.sh: the pipeline. Team machines and the hosted fallback both run it.
make test
```

Fork PRs get no OIDC token, so they never reach the team machines.

## Join the team

```bash
octopus ci init owner/repo
```

One command; it works out your role. It asks `gh` whether you are admin of the target.

- **Admin:** creates the team key if there is none (passphrase asked), tells you to commit its
  public half as `.octopus/team.pub`, and offers `Add the members of the "owner" organization to
  the team?` (for a user-owned repo: the collaborators with write access). Say yes and the member
  list is signed, a short-lived delegation is stored, and your node repeats the sync daily.
  `octopus team sync --org <org>`, `team show` and `ci status` are there for scripts and checks.
- **Member:** pins the team key from `.octopus/team.pub` of the repo.
- **Both:** the node proves it belongs to your GitHub login by signing its keys with your
  Ed25519 SSH key (`ssh-keygen -Y sign`, the agent is used), allows the repo, and installs a
  background service that runs `octopus ci node`. `--no-service` skips the service.

You need an Ed25519 SSH key added to your GitHub account, `gh` logged in (it needs no admin rights
for a member), and `gh auth setup-git` so the machine can clone with your credentials.

## What happens on a push

1. The hosted `submit` job asks GitHub for one OIDC token per OS. The token's audience carries the
   OS and the key replies are sealed to, so GitHub's signature binds them. It sends the tokens
   to the fleet and waits for a lease on each leg (see "Busy machines" below for how long). It
   then **stays until every leg a team machine took has ended** (see "The submit job is the
   coordinator").
2. Every node of that OS verifies the token (GitHub's public keys, issuer, expiry, one-time `jti`),
   then the repo, event and workflow: only `push`/`pull_request` of an allowed repo, from the
   pinned Octopus workflow. The commit, run and OS come from the token, never from the request.
3. Nodes agree who runs a leg without a vote: members that are online and idle are ranked by
   `hash(leg id, node id)`; rank 0 claims at once, rank k after k x 2 s. A claim is held 1 s
   (a better claim makes it yield), then leases are published every 10 s. A silent owner is
   replaced after 30 s by the next node with epoch + 1, and a node that lost never posts a final
   status. Under a partition a leg can run twice; the status is keyed by (commit, OS), so the later
   one replaces the earlier.
4. The owner checks out the commit in a fresh directory with your git credentials, runs
   `.octopus/ci.sh`, streams the log to the submit job, and sends it the result. It also tries to
   post the commit status `octopus/ci (<os>)` with your `gh` login, **best effort**: a login
   without write access to the repo cannot, and the leg runs and reports anyway. The job
   environment has no `GITHUB_TOKEN`/`GH_TOKEN`.
5. While a leg runs, the node asks GitHub (with the owner's `gh`, every lease interval, 10 s)
   whether it is still wanted, and stops it if not, so a cancelled run does not keep the machine
   busy and push the next run onto a paid hosted runner. A leg is stale when the attempt of its
   run concluded `cancelled` (typically `cancel-in-progress` after a new push), or, for a
   `pull_request` leg, when the PR was closed or its head is no longer the head the leg started
   for. The submit job now stays up for the legs it handed off, so the run is `in_progress` while
   a leg runs and `completed` alone still means nothing for a leg of an older job; a completed
   run can no longer be cancelled by a newer push, hence the PR check. A newer commit never stops a `push` leg. Any error, rate limit or
   unknown answer keeps the leg running. A stale leg frees the machine at once, publishes `done`
   with state `cancelled` (so no other node takes it over) and **posts no commit status**: it
   could be a re-run of the same commit as a newer run, and a status posted here would replace
   that run's. The `pending` posted at the start stays until the newer run replaces it; a run
   cancelled with nothing newer behind it leaves `pending` on its commit.
6. Legs nobody leased go to a hosted runner (dynamic matrix); `windows` always does.

## The submit job is the coordinator

`octopus ci submit --follow` (what the reusable workflow runs) does not exit after the lease. It
prints each leg's log with an `[os]` prefix, receives its result over the encrypted reply
channel, writes a summary table to the job summary and decides:

| A leg taken by a team machine ends as | The submit job | The leg |
| --- | --- | --- |
| `success` | passes | done |
| `failure` / `error` | **fails** (the check that gates), with the exit code in the summary | is not run again |
| `timeout`: still running after `max-run` (default 5400 s) | **fails** | is not run again |
| `cancelled`: its run was superseded | not affected | done |
| `lost`: the machine said nothing (no log line, no lease) for `silent-timeout` (default 90 s) | not affected | **runs on a hosted runner** (`fallback`) |

So team machines need no write access to the repo: the result reaches GitHub through the submit
job. The job is a hosted Linux job that stays up for as long as the slowest leg (at most 6 h). It
is free on a public repo and bills minutes on a private one: lower `max-run` or `busy-wait` if
that matters. A leg that was lost may have been taken over by another team machine in the
meantime (nodes take over a silent owner after 30 s), so it can run twice; the fallback is the
price of never leaving a run without a result.

Not done: the node still posts its own commit status when it can, so there can be two signals
(this job and `octopus/ci (<os>)`). Removing it is a follow-up.

## Busy machines: wait or fall back

Nodes announce every 10 s whether they are busy (`busy`, `capacity`). Per OS, the submitter
tells three cases apart, counting only machines whose login is a member for the leg's repo:

| Heard in the announces | The submitter | If the limit passes, the reason says |
| --- | --- | --- |
| No machine of the OS online | gives up after about 25 s | `no team machine of this OS was online` |
| Machines online, all busy | waits up to `busy-wait` (240 s), repeating the request | `all team machines of this OS were busy for 240 s` |
| An idle machine | waits up to `wait` (45 s) for its lease | `no machine took it within 45 s` |

`wait` counts from the moment a busy machine became free, so the time spent waiting does not eat
the wait for its answer. Both are inputs of the reusable workflow (`wait`, `busy-wait`) and
options of `octopus ci submit` (`--wait`, `--busy-wait`).

A busy node that gets a request does not forget it: the leg is kept in the state `busy`, and
the node plans it again as soon as it has a free slot, as long as the submitter keeps repeating
the request (it does every 3 s; the node stops caring after 30 s of silence, so a leg whose
submitter already went to a hosted runner is not run twice for nothing). The token is not
verified again (its one-time `jti` allows one verification): the node remembers the digest of each
token and the verified leg. Waiting legs are planned oldest first, one per free slot, and join the
ranking of the nodes idle at that moment, so a machine that was busy never jumps ahead of idle
ones; if another node already claimed the leg, the usual lease rules apply.

Cost trade-off: the hosted `submit` job bills while it waits. A Linux minute costs a fraction of
a hosted macOS minute, and a machine that is online will usually be free within a few minutes,
so up to 4 minutes of waiting is cheaper than a hosted macOS leg. Lower `busy-wait` if your legs
are long, set it to `0` and a busy fleet falls back at once. The job
has `timeout-minutes: 15`.

## What uses logos-delivery

| What | Mechanism |
| --- | --- |
| Presence, status, claims, leases, done, the signed member list, requests | topic `/octopus/1/ci/proto` |
| Logs and the result to the submitter | reliable encrypted channel (keys from the reply key and the node key); the node replays its buffer when the submitter says hello |

Only the supported messaging and channel APIs are used, not the kernel API. History/store is not
used: lists are re-published every 5 minutes and on request.

## Security notes

- **A job can read your GitHub login and use your SSH agent.** A job runs `.octopus/ci.sh` on the
  machine as its owner, in a fresh directory but with no sandbox. Whoever can push a branch to an
  allowed repo can make it run `gh auth token` or `git credential fill` (the token is in the `gh`
  config or the keychain, not only in the environment), read `~/.ssh`, and reach anything else
  the owner's user can. Octopus removes `GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK` and the team
  passphrase from the job environment, which hides them from a careless script and no more.
  `octopus ci init` prints this and asks the owner to confirm (`--yes` skips the question, not the
  notice). Only serve repos whose writers you would trust with your account. Running jobs as a
  separate OS user or in a container, with the commit statuses posted by the node outside of it,
  is the real fix and is not built yet.
- Membership: team key -> signed member list (or a delegation, 30 days, scoped to one org/repo) ->
  GitHub login -> SSH signature of the node's keys (checked against `github.com/<login>.keys`) ->
  node. Removing someone takes effect at the next sync (a list lives 3 days at most).
- **A list only speaks for its scope.** A login on the list of `org:a` is a member for the legs of
  repos of `a`, not for other orgs: nodes and the submitter check the login against the lists
  that cover the repo of the leg. So a leaked delegated key (one scope, 30 days) can add logins
  only to its own scope, and cannot claim or fake leases for legs elsewhere.
- Lists hold logins, not numeric user ids: a login that is renamed or deleted can be taken by
  another person, who could then prove it with their own SSH key. Known limitation.
- If `github.com/<login>.keys` fails (429, 5xx), the keys already held keep being used and a retry
  follows in a minute; an outage is never taken for "no keys". A missing user (404) has none.
- The team key is encrypted at rest and loaded only by `team init`/`team sync`/`ci init`; the
  service holds only the delegated key (it can sign member lists of one scope for 30 days).
- A rogue member node could post a false success. Anyone with write access can already set a
  commit status; the status shows which login posted it.
- A sync that would shrink a list sharply is refused without `--force`; a failed sync keeps the
  previous list.

## Phase 0 spike (measured once, 2026-10-22)

A throwaway workflow on a GitHub-hosted `ubuntu-latest` runner, against a node on a laptop behind
NAT, both on the shared `logos.test` fleet:

| | Edge | Core |
| --- | --- | --- |
| `pip install "octopus[logos]"` from git | 51 s | 45 s |
| `octopus lib install` (the library) | 8 s | 3 s |
| node start | 10.1 s | 6.4 s |
| first topic round trip (first ping to first pong) | 1.8 s | 0.5 s |
| first channel round trip (first send to first echo) | 0.8 s | 1.3 s |

Both modes joined the fleet and exchanged messages on a topic and on a reliable channel. So a
hosted `submit` job costs roughly 70 to 80 s of setup (about 2 billed minutes on Linux), plus the
time to get a lease. One run, one network, two jobs: treat the numbers as an order of magnitude.

Findings that changed the code:

- `transport.stop()` took about 23 s, and in Edge mode the process crashed at exit (segmentation
  fault, exit code 139). `octopus ci submit` writes its outputs and leaves with `os._exit(0)`
  without stopping the node.
- The library download is fast (3 to 8 s), not the 270 MB wait that was feared.

## First end-to-end run (2026-10-22, `octopus-scratch`, real GitHub OIDC)

A push ran the reusable workflow from the pushed branch commit. The hosted `submit` job read its
workflow commit from its OIDC token, installed Octopus and the library, and asked the fleet for
`linux` and `macos`. The laptop (a macOS node installed by `octopus ci init`, running as a
launchd service) verified the real token against GitHub's keys, claimed the macOS leg, cloned the
commit, ran `.octopus/ci.sh`, and posted `octopus/ci (macos)` pending, then success, as its
owner. (`github.job_workflow_sha` is empty in a reusable workflow, so the job reads
`job_workflow_sha` from its own OIDC token.) There is no Linux node, so that leg fell back to a hosted runner, and Windows ran hosted.
The submit job took about 2 minutes, most of it waiting for the Linux leg: the submitter now gives
up on an OS with no node online after about 25 s (two announce intervals and a margin, so
one lost announce does not make a leg run twice).

## Not verified

- Several team machines and a real fleet under load; a takeover with real laptops sleeping.
- Stopping a stale leg against real GitHub: the probe is tested with a fake `gh` only, so the
  exact answers of `actions/runs/{id}/attempts/{n}` and `pulls/{n}` (and the rate cost of about
  one to two calls per 10 s per running leg) are assumed from the API docs. A leg that is still
  waiting or claiming is not checked, only a running one.
- Speed and cost of a real pipeline: nothing is compared with a hosted run yet.
- Windows nodes, RSA SSH keys, a sandbox for jobs, membership removal before the next sync.
