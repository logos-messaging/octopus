# CI: lint, tests and library build on Linux, macOS and Windows
**Labels:** testing

## Tasks
- GitHub Actions: ruff + pytest (`-m "not needs_lib"`) on a 3-OS matrix, Python 3.10 to 3.14.
- A job that builds `liblogosdelivery` per OS (cached), runs `needs_lib` tests where it builds; record Windows status honestly (the bindings load a `.so` today, so Windows support may need bindings work: open an issue upstream if so).
- Pin the bindings submodule; a scheduled job to bump it.

## Acceptance
- Green matrix on `main`; clear docs for which OS runs which tests.

## Depends on
None.
