# Coordinator deployment and operations
**Labels:** coordinator, docs

## Tasks
- Dockerfile and systemd unit; config via file + env; secrets (App key, webhook secret) via files/env, never logged.
- Health endpoint and Prometheus metrics (runners online, jobs by state, offer latency, failures by reason).
- Runbook: DNS/TLS for the webhook, firewall ports, adding a second coordinator, key rotation.

## Acceptance
- A fresh VPS can be brought up from the runbook; metrics scrape works.

## Depends on
06.
