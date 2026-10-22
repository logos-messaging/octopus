# OIDC pick mode: no GitHub credential on the coordinator

The default Octopus flow needs the coordinator to hold a GitHub token or App key, to mint
one-use JIT runners. Pick mode needs **no GitHub credential anywhere**:

1. Each machine is a plain GitHub runner registered by hand ([manual-runner.md](manual-runner.md)),
   with a **unique label** `octopus-<name>`.
2. A workflow's first job (`pick`) runs on a GitHub-hosted runner, gets a short-lived OIDC token
   from GitHub (`permissions: id-token: write`) and POSTs it to the coordinator.
3. The coordinator checks the token against GitHub's public keys, checks trust from its
   claims, reserves a free runner and answers `{"label": "octopus-lap", "runner": "<id>"}`.
4. The real job runs on `runs-on: [self-hosted, <label>]`.

The coordinator still needs a public HTTPS endpoint (Caddy in front, see
[coordinator-ops.md](coordinator-ops.md)) and the logos-delivery node (runners announce their
presence over it). It stores no GitHub secret, except the webhook secret (a shared secret
that only GitHub and the coordinator know; it lets the coordinator hear that a job ended).

## Coordinator

```
OCTOPUS_OIDC_AUDIENCE=https://octopus.example.org   # turns pick mode on; the pick job asks for this audience
OCTOPUS_ALLOWED_REPOS=acme/app                      # or OCTOPUS_ALLOWED_ORGS
OCTOPUS_WEBHOOK_SECRET=...                          # workflow_job webhooks release reservations
OCTOPUS_PUBLIC_IP=203.0.113.5
```

With the audience set, the GitHub token / App variables are optional. Set them too and the JIT flow
runs beside pick mode (jobs labelled `octopus` go to the scheduler, `octopus-<name>` jobs are picked).
Forward `POST /pick` in the proxy next to `/github/webhook` (`deploy/Caddyfile` does). In the
repo's webhook settings, send `workflow_job` events to the webhook URL.

## Runner

```bash
octopus init --role manual
```

It asks, step by step: the repo, a runner name, the registration token (hidden), and "Let a
coordinator in OIDC pick mode offer this runner?" (default yes). On yes it can take the coordinator's multiaddr
(the `multiaddr` line of `octopus coordinator status`; empty means the shared logos fleet; a wrong format is refused at
once) and moves this runner's node to a free port if the default is taken. It then prints the
label to use in `runs-on` and runs the runner, **announcing its unique `octopus-<name>` label in
presence** while it runs. Run `octopus init --role manual` again to start it later: no token is asked.
Use a separate `--data-dir` if the machine also runs the coordinator-flow runner (same node key
and cache). Answer no and the runner still works, but is never picked.

## Workflow

```yaml
on: [push, pull_request]

jobs:
  pick:
    runs-on: ubuntu-latest
    permissions:
      id-token: write          # lets this job ask GitHub for an OIDC token
    outputs:
      label: ${{ steps.pick.outputs.label }}
    steps:
      - id: pick
        env:
          OCTOPUS_URL: https://octopus.example.org     # the OIDC audience (its origin)
        run: |
          for i in $(seq 1 20); do
            # a fresh token each try: a token is accepted once
            TOKEN=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
              "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$OCTOPUS_URL" | jq -r .value)
            code=$(curl -sS -o pick.json -w '%{http_code}' -X POST \
              -H "Authorization: Bearer $TOKEN" "$OCTOPUS_URL/pick")
            if [ "$code" = 200 ]; then
              echo "label=$(jq -r .label pick.json)" >> "$GITHUB_OUTPUT"; exit 0
            fi
            [ "$code" = 503 ] || { cat pick.json; exit 1; }
            sleep "$(jq -r '.retry_after_s // 30' pick.json)"
          done
          echo "no runner became free"; exit 1

  build:
    needs: pick
    runs-on: [self-hosted, "${{ needs.pick.outputs.label }}"]
    steps:
      - uses: actions/checkout@v4
      - run: make test
```

Every job that needs a runner needs its own `pick` job (a pick reserves one runner for the run).
The reservation ends when GitHub reports the job on that label completed (webhook), or after
10 minutes if the job never starts; once it runs, it is held up to 6 hours.

## What is checked

- **Token**: RS256 signature against `https://token.actions.githubusercontent.com/.well-known/jwks`
  (cached, refreshed when a `kid` is unknown, at most every 30 s), issuer, audience, `exp`/`nbf`
  (30 s leeway) and a one-time `jti`. Any other algorithm is refused.
- **Trust, from the claims**: `repository` is on the allowlist, `event_name` is `push` or
  `pull_request` (the existing `TrustPolicy`), the workflow file is in that same repo
  (`job_workflow_ref`).
  A fork PR gets no OIDC token from GitHub, so it cannot ask at all.

Answers: `200` label; `401` bad token; `403` not trusted; `503` + `Retry-After` no runner free.

## Trade-offs

See also [threat-model.md](threat-model.md), "OIDC pick mode".

- Runners are **persistent**, not one-use: a job can leave files, processes and caches for
  the next job on the machine. Do not use it for repos you do not fully trust.
- GitHub assigns a job **by label**. The unique label makes the choice stick, but anyone who
  can edit a workflow of an allowed repo can write `runs-on: [self-hosted, octopus-lap]`
  without picking. The pick gives balancing and a reservation; the trust boundary is
  "who can push to the allowed repos", exactly as for any self-hosted runner.
- The coordinator tracks busy runners by its own reservations, not by what the runner
  reports (a plain runner does not know). A job sent to a runner by other means is invisible.

## Not covered here

Verified with generated keys and a fake JWKS in the tests, not yet against GitHub's real OIDC
service and a real runner.
