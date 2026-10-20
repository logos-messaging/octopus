# GitHub App

Coordinators learn about jobs from a GitHub App: GitHub sends a `workflow_job` webhook when
a job that targets `runs-on: [self-hosted, octopus]` is queued, starts and completes. The
same App's credentials are used to read the workflow run (trust checks) and, in later
steps, to register ephemeral JIT runners.

## Create the App

On the org (or user) that owns the repos: **Settings → Developer settings → GitHub Apps →
New GitHub App**.

- **Webhook**: active. **URL**: `https://<coordinator host>/github/webhook`.
  **Secret**: a long random string, e.g. `openssl rand -hex 32`. Deliveries without a valid
  `X-Hub-Signature-256` for this secret are rejected.
- **Where can this App be installed**: only on this account.
- **Subscribe to events**: **Workflow job**.

### Permissions

| Permission | Access | Used for |
|---|---|---|
| Repository: Metadata | Read-only | Mandatory for every App |
| Repository: Actions | Read-only | `workflow_job` webhooks; reading the workflow run to detect fork PRs |
| Repository: Administration | Read and write | Repo-scoped JIT runners: `generate-jitconfig`, deleting orphaned runners |
| Organization: Self-hosted runners | Read and write | Org-scoped JIT runners (instead of Administration) |
| Organization: Members | Read-only | Checking that the sender is an org member |

Octopus never writes through the Actions API, so read-only is enough there. Grant
Administration (repo scope) or Self-hosted runners (org scope) depending on where runners
are registered; both is fine. Without Members: read GitHub hides private membership, and
every sender is treated as a non-member.

### Credentials and install

1. After creating the App note its **App ID** (or Client ID; either works as JWT issuer).
2. **Generate a private key** and copy the `.pem` to the coordinator, readable only by the
   coordinator's user (`chmod 600`). It never leaves that host.
3. **Install App** on the org and pick the repos that may use Octopus.

## The webhook endpoint

The coordinator serves `POST /github/webhook` on `127.0.0.1:8080` by default
(`octopus.coordinator.webhook_server.WebhookServer`). It speaks plain HTTP, so put a
TLS-terminating reverse proxy in front, e.g. Caddy:

```
octopus.example.org {
    reverse_proxy /github/webhook 127.0.0.1:8080
}
```

What it does with a delivery, in order:

| Check | Response |
|---|---|
| Body larger than 1 MiB (checked from `Content-Length`, before reading) | `413` |
| No `Content-Length` (chunked) | `411` |
| `X-Hub-Signature-256` missing or wrong (constant-time compare) | `401` |
| No `X-GitHub-Delivery` | `400` |
| `ping` event | `200 pong` |
| Any other event, or a `workflow_job` action other than `queued`/`in_progress`/`completed` | `202 ignored` |
| Invalid JSON or malformed `workflow_job` | `400` |
| Job without the `octopus` label (case-insensitive) | `202 ignored` |
| Delivery id already processed (GitHub keeps the id on redelivery) | `200 duplicate delivery` |
| Handed to the coordinator | `202 accepted` (`500` if that fails; see below) |

Several webhook secrets can be configured at once, so the secret can be rotated without
dropping deliveries: add the new one, change it on GitHub, remove the old one.

GitHub does not retry a delivery that failed (non-2xx or timeout). After a `500`, the job
stays queued on GitHub until you **Redeliver** it by hand, which is then processed because
the id was released. To avoid depending on that, the coordinator must persist an event
before answering and reconcile queued jobs through the API on startup and periodically
(planned with the scheduler, #15).

The webhook path makes no GitHub API calls, so it always answers inside GitHub's 10 s limit.
Delivery ids are remembered in memory for 3 days; a restarted coordinator relies on the job
store to ignore repeated transitions.

## Trust-policy inputs

`octopus.github.trust.resolve_trust_inputs` turns a queued job into the policy's
`JobContext`:

- **repo**: from the payload.
- **head repo**: the workflow run's `head_repository` (`GET /repos/{o}/{r}/actions/runs/{id}`).
  For a fork PR this is the fork. A missing head repo counts as a fork.
- **event**: the run's trigger (`push`, `pull_request`, `pull_request_target`, ...). The
  policy serves only `push` and `pull_request`: `pull_request_target`, `workflow_run` and
  `issue_comment` run in the base repo but can check out fork code, so they are rejected.
- **sender** and **org membership**: `GET /orgs/{org}/members/{sender}` (204 member, 404 not).
  For a repo owned by a user account only that user counts. Bots are never members.

## Check it works

On the App's page, **Advanced → Recent deliveries** shows each delivery and the response.
The `ping` GitHub sends on creation should show `200 pong`; **Redeliver** a `workflow_job`
delivery to see `200 duplicate delivery`.
