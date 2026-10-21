# Manual runner: a plain GitHub runner with a one-hour token

`octopus init --role manual` registers this machine with GitHub using a **registration
token you copy from the GitHub UI**. Octopus never stores the token and never puts it on a
command line. Use it to try a workflow on a laptop without giving any Octopus component a
standing GitHub credential.

**It is not the Octopus flow.** The runner takes jobs from GitHub directly: there is no
coordinator, no scheduling, no encrypted offer and no Octopus trust policy. Any workflow in the
target whose `runs-on` matches the runner's labels runs on this machine. Register it only on
repos you trust, and read [threat-model.md](threat-model.md) first.

## Register and run

```bash
octopus init --role manual        # or: octopus init, and answer "manual" to the role question
```

1. Octopus asks for the target: a repo (`acme/app`), an org (`acme`) or a https URL.
2. It prints the page to open (`https://github.com/acme/app/settings/actions/runners/new`).
   Copy the token shown in the `./config.sh` command there.
3. Paste it at the hidden prompt (or set `OCTOPUS_REGISTRATION_TOKEN`; it is never passed on to the runner). It also
   asks for extra labels and a runner name; an empty answer takes the default.
4. Octopus downloads and verifies the pinned `actions/runner` release (the same one the
   coordinator flow uses), registers the runner with the label `octopus`, and runs it in the
   foreground. Ctrl-C stops it; it stays registered.

Answer "no" to "Run it now?" to register only. Run `octopus init --role manual` again with the same
target to start it later: it is already registered, so no token is asked. To register again, remove
it first (below).

`self-hosted`, the OS and the arch labels are added by GitHub, `octopus` by Octopus. The runner tree
lives in `<data dir>/manual-runner/<owner>__<repo>/` (`--data-dir` picks another data dir).

A workflow uses it with `runs-on: [self-hosted, octopus]` (add the OS label, such as `macOS`, to
pick this machine).

## Remove it

```bash
octopus runner unregister-manual acme/app
```

It asks for a removal token (shown on the runners page of the repo; or set `OCTOPUS_REMOVAL_TOKEN`: a registration token does not work for removal), unregisters the runner and
deletes its local files (`--keep-files` keeps them). A runner that is only stopped stays listed in
GitHub as offline until it is removed.

## Shared account and files

A manual runner runs jobs as your user, with no trust policy, and its `bin/` and `externals/` are
hard-linked to the verified runner cache that the Octopus (coordinator) runner also uses from the same
data dir. A job on a manual runner can therefore change files that later Octopus jobs on this
machine run. If you run both on one machine, use a separate user or a separate `--data-dir` for the
manual runner.

## Tokens

- The registration and removal tokens expire after one hour and are bound to the target.
- They are passed to `config.sh` through the environment (`ACTIONS_RUNNER_INPUT_TOKEN`), so they
  do not show in `ps`, and they are masked in any error Octopus prints.
- After registration the runner keeps its own credential in its tree (`.credentials`,
  `.credentials_rsaparams`). Protect that directory like a key.
