# Networking: bootstrap, NAT and node roles

Verified against `liblogosdelivery` (logos-delivery `v0.39.0-45`) with `tests/test_node_connectivity.py`.

## Config shape
`octopus.transport.config` builds the JSON passed to `NodeWrapper.create_and_start`:
`{"mode": "Core", "preset": "logos.test", "messagingOverrides": {...}}`. Octopus joins the public Logos
test network (preset `logos.test`: its cluster, shards and fleet entry nodes), so the fleet provides
discovery and bootstrap; Octopus traffic uses its own `/octopus/1/...` content topics. Coordinators
are still needed for GitHub, scheduling and as dedicated entry nodes.
Top-level `clusterId` (as in the bindings README) is rejected by this library version.

## Coordinator (public IP)
- `coordinator_config(public_ip, tcp_port=60000, entry_nodes=[other coordinators])`.
- Open TCP `tcp_port` (and UDP `tcp_port` for QUIC) in the firewall.
- There is no announce-address option (`ext-multiaddr` and `dns4-domain-name` are not exposed). The public IP is advertised with `nat = "extip:<ip>"`. Use a static IP; a DNS name cannot be advertised yet.
- The coordinator's multiaddr (`MyMultiaddresses` node info, `/ip4/<ip>/tcp/<port>/p2p/<peerId>`) is what runners use as entry node.

## Runner (laptop, behind NAT)
- `runner_config(entry_nodes=[coordinator multiaddrs], nat=None|"any")`. It dials out, so a plain NAT works without port forwarding: the coordinator relays gossip to it.
- `nat="any"` additionally tries UPnP/NAT-PMP.
- Mode is `Core`. `Edge` (client-only) would save resources but has not been evaluated.

## Environment variables (`config.from_env`)
`OCTOPUS_ENTRY_NODES` (comma separated), `OCTOPUS_PUBLIC_IP` (coordinators), `OCTOPUS_TCP_PORT`, `OCTOPUS_NAT`.

## What is and is not verified
- Verified: runner finds the coordinator via `entry-node` and receives its message (`pytest -m needs_lib`).
- AutoNAT and circuit relay are always enabled in the library (`waku_switch.nim`); DCUtR is referenced in `delivery_dialer.nim`. Direct runner-to-runner hole punching is **not** verified. Octopus does not rely on it: all traffic can go through the coordinator.
- `tests/nat/` checks the relay path with runners on separate networks (not run in CI yet; needs a Linux build of the library).
