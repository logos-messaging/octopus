# Manual runner: a plain GitHub runner with a one-hour token

`octopus runner register-manual` registers this machine with GitHub using a **registration
token you copy from the GitHub UI**. Octopus never stores the token and never puts it on a
command line. Use it to try a workflow on a laptop without giving any Octopus component a
standing GitHub credential.

**It is not the Octopus flow.** The runner takes jobs from GitHub directly: there is no
coordinator, no scheduling, no encrypted offer and no Octopus trust policy. Any workflow in the
target whose `runs-on` matches the runner's labels runs on this machine. Register it only on
repos you trust, and read [threat-model.md](threat-model.md) first.

## Register and run

```bash
octopus runner register-manual acme/app          # a repo; or: acme (an org), or a https URL
```

1. Octopus prints the page to open (`https://github.com/acme/app/settings/actions/runners/new`).
   Copy the token shown in the `./config.sh` command there.
2. Paste it at the hidden prompt (or set `OCTOPUS_REGISTRATION_TOKEN` for that one command).
3. Octopus downloads and verifies the pinned `actions/runner` release (the same one the
   coordinator flow uses), registers the runner with the label `octopus`, and runs it in the
   foreground. Ctrl-C stops it; it stays registered.

Run the same command again to start it later: it is already registered, so no token is asked.

| Option | Meaning |
|---|---|
| `--labels L` | Extra labels (repeatable). `self-hosted`, the OS and the arch are added by GitHub, `octopus` by Octopus. |
| `--name N` | Runner name in GitHub (default: `octopus-manual-<host>`). |
| `--no-start` | Register only. |
| `--reconfigure` | Register again (a new token is asked). |
| `--data-dir D` | Octopus data dir. The runner tree lives in `D/manual-runner/<owner>__<repo>/`. |

A workflow uses it with `runs-on: [self-hosted, octopus]` (add the OS label, such as `macOS`, to
pick this machine).

## Remove it

```bash
octopus runner unregister-manual acme/app
```

It asks for a removal token (shown on the runners page of the repo), unregisters the runner and
deletes its local files (`--keep-files` keeps them). A runner that is only stopped stays listed in
GitHub as offline until it is removed.

## Tokens

- The registration and removal tokens expire after one hour and are bound to the target.
- They are passed to `config.sh` through the environment (`ACTIONS_RUNNER_INPUT_TOKEN`), so they
  do not show in `ps`, and they are masked in any error Octopus prints.
- After registration the runner keeps its own credential in its tree (`.credentials`,
  `.credentials_rsaparams`). Protect that directory like a key.
