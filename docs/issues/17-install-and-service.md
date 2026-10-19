# Installation and service mode
**Labels:** cli, docs

## Tasks
- `pipx`/`uv tool` install instructions and a one-line install script; bundle or fetch the native library per OS.
- Run as a service: launchd (macOS), systemd user unit (Linux), Windows service/Task Scheduler; `octopus runner install-service`.
- Upgrade path and uninstall.

## Acceptance
- The runner survives reboot on all three OSes.

## Depends on
15.
