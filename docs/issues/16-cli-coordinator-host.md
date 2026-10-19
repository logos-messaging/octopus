# CLI for coordinator hosts and job inspection
**Labels:** cli, coordinator

## Tasks
- `octopus coordinator start|status`: config validation, start webhook server, node, scheduler.
- `octopus jobs [--failed]`, `octopus logs <job> [-f]`, `octopus explain <job>`: query a coordinator (over the transport or a local admin API) and render the timeline, failure reason and last log lines.
- Machine-readable output (`--json`).

## Acceptance
- `explain` of each injected failure from the integration suite shows the right reason.

## Depends on
08, 09, 14.
