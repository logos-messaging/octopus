# Running a coordinator in production

How to bring up a coordinator on a fresh VPS, watch it, add a second one and rotate its
secrets. What the coordinator does is in [architecture.md](architecture.md); its settings are
in `deploy/coordinator.env.example`.

> **Status.** The pieces in `deploy/` and `octopus.coordinator.ops` (metrics, `/healthz`,
> `/readyz`) are in place. `octopus coordinator start` is added by #23, and it must start the
> ops endpoint before this runbook works end to end; see [Wiring](#wiring-for-the-coordinator-service).
> The Docker image and the systemd unit have not been run on a real VPS yet.

## 1. What you need

- A VPS with a **public IPv4**, 1 vCPU, 1 GiB RAM and 10 GiB of disk is enough to start. Debian 12
  or Ubuntu 22.04+ (the unit file uses systemd credentials, systemd >= 250).
- A **DNS name** for the webhook, e.g. `octopus.example.org`, pointing at the VPS.
- A GitHub App or a fine-grained token for the served repos ([github-app.md](github-app.md),
  [github-setup.md](github-setup.md)).

## 2. Firewall

| Port | Who | Why |
|---|---|---|
| 22/tcp | you | SSH |
| 80/tcp, 443/tcp | the internet | TLS for the GitHub webhook (and the certificate challenge) |
| 60000/tcp and 60000/udp | the internet | the logos-delivery node runners connect to (`OCTOPUS_TCP_PORT`, UDP for QUIC) |
| 8080/tcp | **nobody** (loopback) | webhook HTTP behind the TLS proxy |
| 9100/tcp | **nobody** (loopback or your monitoring network) | `/healthz`, `/readyz`, `/metrics` |

```bash
ufw default deny incoming && ufw default allow outgoing
ufw allow 22/tcp && ufw allow 80,443/tcp && ufw allow 60000
ufw enable
```

Do not publish 8080 or 9100. `/metrics` has no authentication.

## 3. Install

Pick one.

### A. Docker Compose

```bash
git clone --recurse-submodules https://github.com/logos-messaging/octopus && cd octopus
cp deploy/coordinator.env.example deploy/coordinator.env && chmod 600 deploy/coordinator.env
$EDITOR deploy/coordinator.env          # secrets, OCTOPUS_PUBLIC_IP, allowed repos, OCTOPUS_DOMAIN
cp /path/to/app-key.pem deploy/app-key.pem   # App mode (+ OCTOPUS_GITHUB_APP_KEY_FILE=/run/secrets/app-key)
# token mode: touch deploy/app-key.pem (compose needs the file to exist)
chmod 600 deploy/app-key.pem
docker compose -f deploy/docker-compose.yml up -d --build
```

The first build compiles `liblogosdelivery` and takes a while. Caddy fetches the certificate
for `OCTOPUS_DOMAIN` on its own. Add `--profile monitoring` to also run Prometheus on
`127.0.0.1:9090`.

### B. systemd on the host

```bash
useradd --system --home-dir /var/lib/octopus --shell /usr/sbin/nologin octopus
python3 -m venv /opt/octopus/venv
git clone --recurse-submodules https://github.com/logos-messaging/octopus /opt/octopus/src
# Build liblogosdelivery and put it where the bindings load it (README, "Develop")
/opt/octopus/venv/bin/pip install '/opt/octopus/src[logos]'

install -d -m 0750 -o root -g octopus /etc/octopus
install -m 0640 -o root -g octopus deploy/coordinator.env.example /etc/octopus/coordinator.env
# App mode: also install the key and set OCTOPUS_GITHUB_APP_KEY_FILE=/etc/octopus/app-key.pem
install -m 0640 -o root -g octopus /path/to/app-key.pem /etc/octopus/app-key.pem
install -m 0644 deploy/octopus-coordinator.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now octopus-coordinator
```

Put Caddy (or nginx) in front of `127.0.0.1:8080`, forwarding only `/github/webhook`; the
`deploy/Caddyfile` is a working example. Add `OCTOPUS_BINDINGS=/opt/octopus/src/vendor/logos-delivery-python-bindings`
to the env file so the coordinator finds the library.

The unit does not restart on exit codes 2 and 3 (bad usage or settings): fix the settings and
`systemctl restart`. State is in `/var/lib/octopus/coordinator/`.

## 4. Point GitHub at it

In the App (or the repo's webhook settings): payload URL `https://<OCTOPUS_DOMAIN>/github/webhook`,
content type `application/json`, the secret from `OCTOPUS_WEBHOOK_SECRET`, event **Workflow jobs**.
GitHub's *Recent deliveries* tab should show `200 pong` for the ping.

## 5. Check it works

```bash
curl -fsS http://127.0.0.1:9100/healthz      # ok            (the process is up)
curl -fsS http://127.0.0.1:9100/readyz       # {"ready": true, ...}   (node connected, store writable)
curl -fsS http://127.0.0.1:9100/metrics | grep ^octopus_
curl -fsS https://<OCTOPUS_DOMAIN>/metrics   # must be 404: ops is not published
```

`/readyz` answers 503 with the failing check in the body; `/healthz` stays 200 as long as the
process serves requests, so use `/readyz` for load balancers and the Docker health check, and
`/healthz` only for "should this process be restarted".

### Metrics

| Metric | Meaning |
|---|---|
| `octopus_runners_online` | runners announcing presence |
| `octopus_runner_slots{kind="capacity\|busy"}` | job slots of the online runners |
| `octopus_jobs{state}` | jobs not yet finished, by state (computed from the job store at scrape time) |
| `octopus_job_transitions_total{state}` | state changes since start |
| `octopus_job_failures_total{reason}` | failed jobs by failure reason (`octopus explain <job>` shows the story) |
| `octopus_offers_total{outcome}` | offers made: `accepted`, `rejected`, `timeout`, `send-failed`, `no-key` |
| `octopus_offer_latency_seconds` | histogram: offer sent to runner's answer |
| `octopus_webhook_events_total{event,outcome}` | deliveries: `accepted`, `ignored`, `duplicate`, `bad-signature`, `bad-request`, `error` |
| `octopus_coordinator_info{version}` | constant 1, carries the version |

Counters reset when the coordinator restarts (Prometheus handles that in `rate()`/`increase()`).
`deploy/prometheus/alerts.yml` has starting alerts: coordinator down, no runners online, jobs
queued with no free slot, offers timing out, jobs failing on the coordinator side, webhook
deliveries rejected.

## 6. Day-2 operations

**Back up** `coordinator.key` (the node key: runners trust its public half, losing it means a new
coordinator identity) and, if you want history, `jobs.db` and `logs.db` (SQLite, kept 30 days).
Stop the coordinator or use `sqlite3 .backup` for a consistent copy.

**Upgrade**: `git pull --recurse-submodules`, reinstall (or rebuild the image), restart. Jobs in
flight when it stops are re-queued or failed as `runner-lost`; check `octopus jobs --failed`.

**Logs**: `journalctl -u octopus-coordinator -f` or `docker compose logs -f coordinator`. Secrets
are never logged.

### Adding a second coordinator

1. Repeat sections 1 to 3 on another VPS with its own `OCTOPUS_PUBLIC_IP`.
2. Give each coordinator its **own** node key (`octopus coordinator keygen`); never copy
   `coordinator.key`. If you use signed discovery, have the root-key holder issue a certificate
   for the new one ([networking.md](networking.md)). Runners then learn it by itself.
3. Set `OCTOPUS_ENTRY_NODES` on each coordinator to the other's multiaddr (shown on start).
4. **Webhook**: GitHub delivers each event to one URL. Until failover (#17) lands, run one
   coordinator as active and keep the second as a warm standby: if the active one dies,
   change the DNS record or the App's webhook URL to the standby. Both must share
   `OCTOPUS_WEBHOOK_SECRET` (or have the old one in `_PREVIOUS`) and the same allowlist. Do not
   point GitHub at both: they would offer the same job twice.

### Rotating secrets

| Secret | Procedure |
|---|---|
| Webhook secret | 1. Put the new value in `OCTOPUS_WEBHOOK_SECRET` and the old one in `OCTOPUS_WEBHOOK_SECRET_PREVIOUS`; restart. 2. Change the secret on GitHub. 3. When *Recent deliveries* shows only `200/202`, remove `_PREVIOUS`; restart. |
| GitHub App private key | Generate a second key on the App page, replace `app-key.pem`, restart (the key is checked on start), then delete the old key on GitHub. |
| Fine-grained token | Create the new token, replace `OCTOPUS_GITHUB_TOKEN`, restart, revoke the old one. |
| Node key / certificate | Certificates expire (default 90 days): re-issue with `octopus root authorize`. A new node key is a new coordinator id; see [networking.md](networking.md) on revocation. |

## 7. Troubleshooting

| Symptom | Look at |
|---|---|
| GitHub shows deliveries as failed | `401`: secret mismatch (see rotation). Timeout or `502`: proxy cannot reach `127.0.0.1:8080`. GitHub does **not** retry; use *Redeliver*. |
| `octopus_webhook_events_total{outcome="bad-signature"}` rising | wrong secret after a rotation, or someone probing the URL |
| Jobs stay `queued` | `octopus_runners_online`, `octopus_runner_slots`; runners need the label `octopus` and a matching OS/arch |
| `octopus_offers_total{outcome="timeout"}` rising | runners offline or behind a broken relay path; check the node's port 60000 is reachable from outside |
| Container is `unhealthy` | `curl 127.0.0.1:9100/readyz` inside the container for the failing check |

## Wiring for the coordinator service

`octopus.coordinator.ops` is deliberately independent of the service so it can be tested alone.
`octopus coordinator start` should:

```python
metrics = CoordinatorMetrics(registry, job_store)
listen = parse_metrics_listen(env.get(METRICS_LISTEN_ENV))   # None when "off"
if listen:
    ops = OpsServer(metrics.render, checks=lambda: {"node": ..., "store": ...}, *listen)
    ops.start()
```

and call the hooks where things happen: `metrics.job_state(state, reason)` on every job
transition, `offer_started(job_id)` / `offer_finished(job_id, outcome)` around an offer, and
`metrics.webhook(event, webhook_outcome(resp.status, resp.message))` for each webhook answer.
