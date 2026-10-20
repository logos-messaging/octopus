# Repo setup with a token

`octopus github setup` configures **one repo** for Octopus in one command, without a GitHub
App and without touching the org. It is the quick path for a repo admin; for many repos or a
whole org, use the GitHub App ([github-app.md](github-app.md)).

```bash
octopus github setup --repo acme/app --webhook-url https://octopus.example.org/github/webhook
```

Missing flags are asked for, then a summary is shown for confirmation. In CI pass every flag
and `--yes`.

## Prerequisites

- You are an **admin** of the repo.
- The coordinator has a **public HTTPS URL** for its webhook endpoint (see "The webhook
  endpoint" in [github-app.md](github-app.md)); `http://` URLs are refused.
- A token (below). By default the `gh` login is used (`gh auth login`); `--token` or
  `GITHUB_TOKEN` take precedence. Prefer `GITHUB_TOKEN` over `--token`, which other users
  can see in the process list.

## Tokens

Two uses, which may be the same token:

| Token | Used by | Fine-grained permissions (this repo only) | Classic / `gh` scopes |
|---|---|---|---|
| setup | `octopus github setup` and `teardown`, once | Webhooks: read and write; Administration: read and write; Metadata: read. For the example PR also Contents, Pull requests and Workflows: write | `repo`, plus `workflow` for the example PR |
| coordinator | the coordinator, at run time | Administration: read and write (JIT runners); Actions: read (workflow runs); Metadata: read (sender's permission) | `repo` |

The coordinator token is written to `coordinator.env`. It is `--coordinator-token` if given,
else the setup token from `--token` or `GITHUB_TOKEN`. **The `gh` login is never written**:
it reaches every repo you can, so it should not sit on a server. Create a fine-grained token
limited to this repo (Settings → Developer settings → Fine-grained tokens) and pass it as
`--coordinator-token`, or add it to the env file as `OCTOPUS_GITHUB_TOKEN` yourself.

## What it does

Each step is idempotent; a rerun changes nothing and the checklist shows `ok` throughout.

| Step | Does | On rerun |
|---|---|---|
| admin access | `GET /repos/{o}/{r}`: the token works, the user is an admin, a classic token has the `repo` scope | |
| `coordinator.env` | writes `OCTOPUS_GITHUB_REPO`, `OCTOPUS_WEBHOOK_URL`, `OCTOPUS_WEBHOOK_SECRET` and the coordinator token, mode 0600, keeping any other lines | rewritten only if something changed |
| webhook | creates a repo webhook for `--webhook-url`: `workflow_job` events only, JSON, TLS verified, a new random secret | the hook for that URL is reused; wrong events or config are fixed |
| example workflow PR | opens a PR (branch `octopus/example-workflow`) adding `.github/workflows/octopus-example.yml` with `runs-on: [self-hosted, octopus]`. Never pushes to the default branch. `--no-example-pr` skips it | skipped if the file is on the default branch or the PR is open |
| ping | asks GitHub to ping the hook and waits (`--ping-timeout`, 30 s) for the coordinator's answer in the hook's deliveries. `--no-ping` skips it | pings again |

**The secret.** GitHub never shows a hook's secret again, so the env file is where it is
remembered. If the hook exists but the env file has no secret for that repo and URL (another
machine set it up, or the file was lost), a new secret is set on the hook. `--rotate-secret`
does that on purpose. The env file is written before the hook is changed, so a failed hook
update leaves a secret that the next run reuses.

**The ping** usually fails on the very first run, because the coordinator needs the env file
to start. Start it with `coordinator.env`, then rerun the command. The failure says what
GitHub saw: `401` means the coordinator has a different secret, `0` means GitHub could not
connect.

Exit codes: 0 all good (warnings allowed), 1 a step failed, 2 usage, 3 no token, 4 GitHub
unreachable.

GitHub calls are retried on rate limits; reads are also retried on 5xx, writes are not
(a write answered with a 5xx may still have happened; the next run finds it).

The `OCTOPUS_*` variables in `coordinator.env` are what the coordinator reads; wiring them
into `octopus coordinator start` is #23.

## Who is trusted

With a token the coordinator cannot see org membership (that needs an org permission), so
the sender is trusted when they can push to the repo:
`GET /repos/{o}/{r}/collaborators/{sender}/permission` must be `write`, `maintain` or
`admin`. Read and triage collaborators, outsiders and bots are rejected. The rest of the
policy is unchanged: the repo must be on the allowlist, the PR branch must be in the same
repo (no forks), and the trigger must be `push` or `pull_request`.
`resolve_trust_inputs` picks this check for a `TokenClient` and org membership for a
`GitHubApp`.

## Teardown

```bash
octopus github teardown --repo acme/app
```

Removes the webhook for the URL in `coordinator.env` (or `--webhook-url`), idle `octopus-*`
runners, and the open example PR with its branch. Busy runners are left and reported: rerun
when their jobs end. An example workflow already merged into the default branch is left in
place, as is `coordinator.env`.
