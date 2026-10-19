# LogosTransport: robust wrapper over logos-delivery-python-bindings
**Labels:** transport

## Context
`src/octopus/transport/logos.py` is a stub over `logosdelivery.NodeWrapper`. Everything else depends on it being reliable.

## Tasks
- Confirm the `onMessageReceived` / `onReceivedMessage` event JSON schema against `library/MESSAGE_EVENTS.md` in logos-delivery and fix `_on_event` (currently a guess).
- Event callbacks run on the library's thread: hand messages to a queue and dispatch on a worker so handlers can block.
- Node lifecycle: start/stop idempotent, clean teardown on SIGINT/SIGTERM, surface `onConnectionStatusChange` as a health signal.
- Reconnect/resubscribe after connection loss; do not lose subscriptions.
- Config helper: build the node config (mode, preset/cluster, entry nodes, ports, `nat`) from Octopus settings.
- Document how to locate/build `liblogosdelivery` (`OCTOPUS_BINDINGS`).

## Acceptance
- `pytest -m needs_lib` two-node test passes on Linux and macOS.
- Handlers never run on the library event thread.

## Tests
Unit tests with a fake `NodeWrapper`; the `needs_lib` integration test.

## Depends on
None.
