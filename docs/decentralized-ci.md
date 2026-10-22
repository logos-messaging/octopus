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
   to the fleet and waits up to `wait` seconds for a lease on each leg.
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
   `.octopus/ci.sh`, streams the log, and posts the commit status `octopus/ci (<os>)` with your
   `gh` login. The job environment has no `GITHUB_TOKEN`/`GH_TOKEN`.
5. Legs nobody leased go to a hosted runner (dynamic matrix); `windows` always does.

## What uses logos-delivery

| What | Mechanism |
| --- | --- |
| Presence, status, claims, leases, done, the signed member list, requests | topic `/octopus/1/ci/proto` |
| Logs and the result to the submitter | reliable encrypted channel (keys from the reply key and the node key); the node replays its buffer when the submitter says hello |

Only the supported messaging and channel APIs are used, not the kernel API. History/store is not
used: lists are re-published every 5 minutes and on request.

## Security notes

- Trust boundary is the same as GitHub's: whoever can push to an allowed repo can run code on a
  team machine, as the machine's owner. A job is not sandboxed: it runs in a fresh directory as
  the owner. Do not put release or deploy secrets there.
- Membership: team key -> signed member list (or a delegation, 30 days, scoped to one org/repo) ->
  GitHub login -> SSH signature of the node's keys (checked against `github.com/<login>.keys`) ->
  node. Removing someone takes effect at the next sync (a list lives 3 days at most).
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

## Not verified

- Real GitHub OIDC tokens through the full chain, and the reusable workflow itself (the spike
  used a plain workflow). Found on the first real run: `github.job_workflow_sha` is empty in a
  reusable workflow, so the job reads `job_workflow_sha` from its own OIDC token instead.
- Several team machines and a real fleet under load; a takeover with real laptops sleeping.
- Speed and cost of a real pipeline: nothing is compared with a hosted run yet.
- Windows nodes, RSA SSH keys, a sandbox for jobs, membership removal before the next sync.
