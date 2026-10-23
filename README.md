<p align="center"><img src="assets/octopus.svg" alt="Cypherpunk octopus" width="220"></p>

```
            .-"""""""-.
          .'  _     _  '.
         /   (_)   (_)   \
        |  ▀▀▀▀▀▀▀▀▀▀▀▀▀  |      O C T O P U S
        |   [ 0x4f ct ]   |      lend your laptop to your team's CI
         \   '-.___.-'   /
      .--'.  /|/|/|\|\  .'--.
     /  .-'-' / | | \ '-'-.  \
    (  (  )  (  ) (  )  (  )  )
     '-'\_/'-'\_/'-'\_/'-'\_/'-'
```

# Octopus

Octopus turns the laptops of a team into GitHub Actions runners. Nodes talk to each other over
[logos-delivery](https://github.com/logos-messaging/logos-delivery) (`liblogosdelivery`, via the
[Python bindings](https://github.com/logos-messaging/logos-delivery-python-bindings)). There are
three ways to use it, from the lightest to the heaviest.

**Scope: trusted internal-team PRs only.** Octopus refuses fork PRs and senders who are
not org members (or, for a repo set up with a token, cannot push to it). It is not a
public runner pool.

> Status: early (`v0.4.0`). It has run against real GitHub repos, but the nightly end-to-end test
> is not on yet (#86) and several rough edges are open. See the open issues for the plan.
> Read the [threat model](docs/threat-model.md) before lending a machine.

## Which one do I want?

| I want to... | Run | Details |
|---|---|---|
| Run my team's workflows on team laptops, with no server and no runner registration | `octopus ci init owner/repo` | [decentralized-ci.md](docs/decentralized-ci.md) |
| Make this laptop a plain GitHub runner for one repo (any matching job runs here) | `octopus init owner/repo` | [manual-runner.md](docs/manual-runner.md) |
| Lend a laptop to a coordinator, or run the coordinator (a VPS with a public IP) that hands GitHub jobs to laptops | `octopus init` (guided) | [Quickstarts below](#quickstart-lend-a-laptop), [coordinator.md](docs/coordinator.md) |

`octopus init owner/repo` and `octopus ci init owner/repo` look alike but are different: the first
registers a GitHub runner, the second joins a team and registers nothing.

## The CLI

```
octopus ci init owner/repo                 # join the team of a repo (docs/decentralized-ci.md)
octopus ci light install|status|disable   # macOS: run jobs as a dedicated 'octopus' account
octopus ci uninstall                       # remove that account and its folders
octopus init owner/repo                    # register this machine as a GitHub runner, as a service (docs/manual-runner.md)
octopus init --role manual                 # the same, with questions
octopus runner unregister-manual <repo>    # remove that runner
octopus init                               # guided setup of a runner or coordinator host
octopus runner setup|allow|start|status|stop   # on runner hosts (laptops)
octopus coordinator start|status           # on coordinator hosts (VPS)
octopus jobs [--failed]                    # list jobs
octopus logs <job> [-f]                    # job logs
octopus explain <job>                      # timeline and why it failed
octopus github setup|teardown              # one repo, token-based (docs/github-setup.md)
```

## Quickstart: lend a laptop

`octopus init` asks for each setting below in turn and can install the service. Or step by step:

```bash
octopus runner setup --coordinator <coordinator-public-key>   # who may send you jobs
octopus runner allow acme/app                                 # repo (org/name) or whole org
octopus runner start                                          # foreground; Ctrl-C to stop
octopus runner status                                         # from another shell
octopus runner stop
```

Settings and the allowlist live in the platform config dir (`--config` selects another
location; both files sit next to each other). `--entry-node`, `--labels` and `--max-jobs`
(a number, or `unlimited`; default 1) can be saved with `setup` or passed to `start`; `OCTOPUS_ENTRY_NODES`, `OCTOPUS_TCP_PORT`
and `OCTOPUS_NAT` override the saved file, and flags override both. Exit codes: 2 usage,
3 bad config, 4 network/library failure, 5 not running. The runner needs no GitHub login:
the coordinator checks org membership. Until coordinator discovery (#38) the coordinator's
public key is passed by hand.

## Quickstart: run a coordinator

On a host with a public IP, for one repo set up with a token:

```bash
octopus github setup --repo acme/app --webhook-url https://octopus.example.org/github/webhook
OCTOPUS_PUBLIC_IP=203.0.113.7 octopus coordinator start --env-file coordinator.env
octopus jobs                    # then, on the same host
octopus explain <job>
```

`start` prints the public key and entry node runners need. Settings, the GitHub App
alternative and the inspection commands: [docs/coordinator.md](docs/coordinator.md).

## Install

See [docs/install.md](docs/install.md): one-line install, `octopus lib install` for the native library, and `octopus runner install-service` to start at login. Update with `octopus update`.

## Develop

Needs Python >= 3.10.

```bash
git clone --recurse-submodules https://github.com/logos-messaging/octopus
cd octopus
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Integration tests need the native library (see the bindings README for details):

```bash
NIM_PARAMS=-d:chronicles_runtime_filtering=on \
   make -C vendor/logos-delivery-python-bindings/vendor/logos-delivery liblogosdelivery
cp vendor/logos-delivery-python-bindings/vendor/logos-delivery/build/liblogosdelivery.* \
   vendor/logos-delivery-python-bindings/lib/
ln -sf liblogosdelivery.dylib vendor/logos-delivery-python-bindings/lib/liblogosdelivery.so  # macOS
pip install -e '.[logos]'
pytest -m needs_lib
```

`NIM_PARAMS` lets the library filter its logs by `OCTOPUS_LOG_LEVEL` (default `INFO`); without it the
library writes every TRACE and DEBUG line (#93). Pass it in the environment, not on the `make` command
line, where it would replace the Makefile's own flags.

The bindings load `lib/liblogosdelivery.so` from their own checkout. Octopus looks for them
in `vendor/logos-delivery-python-bindings`; set `OCTOPUS_BINDINGS` to use another checkout.
If the library is missing, `LogosTransport.start()` fails with an error naming the expected path.

Node mode: coordinators run `Core` (relay, public IP); runners on laptops can use `Edge`
(light client) and point `entry_nodes` at the coordinators (see `LogosConfig`).

## CI

| Job | OS | Runs |
|---|---|---|
| `lint` | Linux | `ruff check .` |
| `test` | Linux (Python 3.10 to 3.14), macOS and Windows (3.10, 3.14) | `pytest` (unit tests, without `needs_lib`) |
| `needs_lib` | Linux, macOS | builds `liblogosdelivery` from the logos-delivery commit pinned by the bindings (cached per commit), then `pytest -m needs_lib` |
| `E2E nightly` (`e2e.yml`) | Linux, every night | a coordinator and a runner against a real GitHub repo: `pytest -m e2e` ([docs/e2e.md](docs/e2e.md)) |

Windows runs unit tests only: the bindings load `lib/liblogosdelivery.so`, so the library tests
need Windows support in the bindings first. A weekly job (`bump-bindings.yml`) opens a PR that
moves the bindings pin, so CI shows whether a new logos-delivery still works with Octopus.

Run the same checks locally with `ruff check . && pytest`.

## Layout

- `src/octopus/transport/`: `Transport` interface, `LogosTransport` (logos-delivery), `MemoryTransport` (tests)
- `src/octopus/proto/`: wire messages and content topics (`/octopus/1/...`)
- `src/octopus/policy.py`: trust policy
- `src/octopus/github/`: GitHub App and token auth, webhook verification and parsing, trust-policy inputs, repo setup
- `src/octopus/coordinator/`: coordinator process, scheduler, offers, job and log stores, webhook endpoint, inspection
- `src/octopus/agent/`: runner agent (config, presence, offers, trust, runner manager)
- `docs/architecture.md`: design; `docs/github-app.md`: GitHub App setup; `docs/issues/`: the issue specs
- `docs/github-setup.md`: one-repo setup with a token (`octopus github setup`)
- `docs/coordinator-ops.md`: running a coordinator in production (Docker/systemd, TLS, metrics, rotation); `deploy/` has the files
- `docs/e2e.md`: the nightly end-to-end test against a real GitHub repo, and how to set it up
- `docs/manual-runner.md`: a plain GitHub runner on a laptop, no coordinator
- `docs/oidc-pick.md`: OIDC pick mode, a coordinator with no GitHub credential
- `docs/decentralized-ci.md`: team machines run Linux/macOS CI over logos-delivery, no coordinator, no stored token
- `docs/threat-model.md`: what Octopus protects against and what it does not; `CONTRIBUTING.md`: how to contribute

## License

MIT or Apache-2.0, at your option.
