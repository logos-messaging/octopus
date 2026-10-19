# CLI for runner hosts
**Labels:** cli, runner

## Tasks
- `octopus runner start|stop|status` (foreground and daemon mode), `allow|deny|list`, config file at the platform config dir, `--max-jobs`, `--labels`.
- GitHub login if needed (device flow) for org membership checks, if the design requires it.
- Sensible defaults: first run needs only the coordinator entry node address.
- Clear, actionable error messages; exit codes.

## Acceptance
- A teammate can go from install to an online runner with at most three commands.

## Depends on
02, 12, 13.
