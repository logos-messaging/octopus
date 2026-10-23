# Contributing to Octopus

Thanks for helping. Octopus lends team laptops as GitHub Actions runners over logos-delivery; read the [README](README.md) and [docs/architecture.md](docs/architecture.md) first.

## Set up

Needs Python >= 3.10.

```bash
git clone --recurse-submodules https://github.com/logos-messaging/octopus
cd octopus
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

If you forgot `--recurse-submodules`: `git submodule update --init`.

## Tests

```bash
pytest                 # unit tests; needs no native library
ruff check .           # lint
```

Tests that need the native `liblogosdelivery` are marked `needs_lib` and skipped by default (see `addopts` in `pyproject.toml`). To run them, build the library and expose it to the bindings, as described in the README ("Develop"), then:

```bash
pip install -e '.[logos]'
pytest -m needs_lib
```

`OCTOPUS_BINDINGS` points at another bindings checkout. The NAT tests in `tests/nat/` need a Linux build of the library and Docker.

Write new code against the `Transport` interface and test it over `MemoryTransport`; add a `needs_lib` test only when the real library behaviour matters. Tests must not touch the network, the real GitHub API or the user's config dir (use `tmp_path` and the fakes in `tests/`).

## Code style
- ruff, 100 columns.
- Error strings start with context and end it with a colon before the value: `f"could not load config from: {path}"`.
- Secrets (JIT config, tokens, keys) use `SecretStr`, are never logged and never appear in exceptions.
- Files holding secrets are written atomically with mode 0600.
- Everything unknown fails closed in the trust policy.

## Issues, labels and milestone
- Pick an open issue and assign yourself before starting; check its "Depends on" list. Specs live in `docs/issues/`.
- Labels: `transport`, `coordinator`, `runner`, `cli`, `github`, `testing`, `docs`, `security`, `automation`.
- Work is tracked in the **MVP** milestone and the epic issue.

## Pull requests
- Branch off `main`; one issue per PR, with `Closes #N` in the description and the MVP milestone set.
- Keep commits focused and explain *why* in the message, not the diff.
- Say what you did not verify (for example not run against the real GitHub API, or untested on Windows).
- The PR is auto-assigned to its author. Keep docs in sync: update `docs/` when behaviour changes, and the [threat model](docs/threat-model.md) when you touch trust, keys or secrets.

## Security
Do not open public issues for vulnerabilities; contact the maintainers privately. See the [threat model](docs/threat-model.md).

## License
By contributing you agree your work is licensed under MIT or Apache-2.0, at the project's option.

<!-- demo: this line only exists to start a CI run for the video; the PR is closed unmerged -->
