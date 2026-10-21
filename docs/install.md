# Install a runner

## Install or upgrade

macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/logos-messaging/octopus/main/scripts/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/logos-messaging/octopus/main/scripts/install.ps1 | iex
```

The script uses `pipx`, then `uv tool`, then a private virtualenv, and needs Python 3.10 or newer. It installs `main`; set `OCTOPUS_REF` to a tag or commit to pin. **Run it again to upgrade**, then restart the service (`octopus runner install-service` rewrites it and reloads it, and is safe to repeat).

The native library `liblogosdelivery` is not bundled yet: build it as described in the README ("Develop"). The logos-delivery Python bindings only load a Linux/macOS library today, so the Windows runner is not usable end to end until that is solved upstream.

## Configure

```bash
octopus runner setup --coordinator <coordinator key>   # or --root-key <key>
octopus runner allow <org/repo>
```

## Start automatically

```bash
octopus runner install-service     # also: uninstall-service, service-status
```

| OS | Mechanism | Starts | Restarts after a crash | Logs |
|---|---|---|---|---|
| macOS | launchd LaunchAgent `~/Library/LaunchAgents/org.logos-messaging.octopus.runner.plist` | at login | yes, 30 s throttle | `~/Library/Logs/octopus/runner.log` |
| Linux | `systemd --user` unit `~/.config/systemd/user/octopus-runner.service` | at login; at boot with linger | yes, not for exit codes 2 and 3 (bad usage or settings) | `journalctl --user -u octopus-runner -f` |
| Windows | Task Scheduler task "Octopus Runner" | at logon | not after the process exits with an error (see below) | Task Scheduler history |

The service runs `python -m octopus runner start` from the runner's data directory (so nothing relative is written to `/` or `C:\Windows\System32`) with the interpreter that ran `install-service`, so a pipx or virtualenv install keeps working. `OCTOPUS_BINDINGS` and `OCTOPUS_DATA_DIR`, if set when you install, are baked into the service (Windows tasks cannot carry them: set them with `setx`). Relative paths are made absolute at install time.

**Surviving a reboot.** Per-user services start when the user logs in. On Linux, to start at boot without a login, an admin runs `loginctl enable-linger <user>`; `install-service` prints this when linger is off. On a laptop without auto-login, macOS and Windows start the runner at the next login.

`octopus runner stop` stops the running process; on macOS and Linux the service manager does not restart it after a clean stop. **Known issue:** until the library crash on node stop is fixed upstream (#64), a stop can exit with an error some of the time, and then launchd or systemd start the runner again. To stop it for good, use `octopus runner uninstall-service` (or `launchctl bootout` / `systemctl --user stop octopus-runner`). `uninstall-service` removes the automatic start while keeping your settings, keys and allowlist.

## Uninstall

```bash
octopus runner uninstall-service
pipx uninstall octopus        # or: uv tool uninstall octopus, or delete the virtualenv
```
