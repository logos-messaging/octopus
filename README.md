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

Octopus turns the laptops of a team into GitHub Actions runners. Each running Octopus
offers its machine as a runner; coordinators on VPS hosts take care of discovery,
bootstrap, NAT traversal, and handing GitHub jobs to the right machine. Nodes talk to each
other over [logos-delivery](https://github.com/logos-messaging/logos-delivery)
(`liblogosdelivery`, via the [Python bindings](https://github.com/logos-messaging/logos-delivery-python-bindings)).

**Scope: trusted internal-team PRs only.** Octopus refuses fork PRs and senders who are
not org members. It is not a public runner pool.

> Status: foundation only. The CLI commands are stubs; see the open issues for the plan.

## One CLI, two roles

```
octopus runner start|stop|status|allow   # on runner hosts (laptops)
octopus coordinator start|status          # on coordinator hosts (VPS)
octopus jobs [--failed]                   # list jobs
octopus logs <job> [-f]                   # job logs
octopus explain <job>                     # timeline and why it failed
```

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
make -C vendor/logos-delivery-python-bindings/vendor/logos-delivery liblogosdelivery
cp vendor/logos-delivery-python-bindings/vendor/logos-delivery/build/liblogosdelivery.* \
   vendor/logos-delivery-python-bindings/lib/
ln -sf liblogosdelivery.dylib vendor/logos-delivery-python-bindings/lib/liblogosdelivery.so  # macOS
pip install -e '.[logos]'
pytest -m needs_lib
```

The bindings load `lib/liblogosdelivery.so` from their own checkout. Octopus looks for them
in `vendor/logos-delivery-python-bindings`; set `OCTOPUS_BINDINGS` to use another checkout.
If the library is missing, `LogosTransport.start()` fails with an error naming the expected path.

Node mode: coordinators run `Core` (relay, public IP); runners on laptops can use `Edge`
(light client) and point `entry_nodes` at the coordinators (see `LogosConfig`).

## Layout

- `src/octopus/transport/`: `Transport` interface, `LogosTransport` (logos-delivery), `MemoryTransport` (tests)
- `src/octopus/proto/`: wire messages and content topics (`/octopus/1/...`)
- `src/octopus/policy.py`: trust policy
- `src/octopus/github/`: GitHub App auth, webhook verification and parsing, trust-policy inputs
- `src/octopus/coordinator/`: coordinator; so far the webhook HTTP endpoint
- `src/octopus/agent/`: runner agent, to be implemented
- `docs/architecture.md`: design; `docs/github-app.md`: GitHub App setup; `docs/issues/`: the issue specs

## License

MIT or Apache-2.0, at your option.
