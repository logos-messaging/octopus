# Bootstrap, NAT traversal and coordinator node mode
**Labels:** transport, coordinator

## Context
Runners sit behind NATs; coordinators have public IPs and must act as entry nodes and relays.

## Tasks
- Coordinator node config: Core mode, public listen address, discv5/entry-node settings, preset/cluster for Octopus.
- Runner node config: Edge or Core with `entry-node` list pointing at coordinators; optional `nat` strategy.
- Investigate and document what liblogosdelivery supports for NAT traversal (AutoNAT, circuit relay, hole punching: check `waku_switch.nim`); recommend defaults.
- A reproducible 3-network docker-compose where a runner behind NAT can exchange messages with a coordinator and another runner.

## Acceptance
- Documented, working default config for both roles; the compose test passes.

## Tests
See issue 18.

## Depends on
01.
