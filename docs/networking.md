# Networking: bootstrap, NAT and node roles

Verified against `liblogosdelivery` (logos-delivery `v0.39.0-45`) with `tests/test_node_connectivity.py`.

## Config shape
`octopus.transport.config` builds the config passed to `NodeWrapper.create_and_start`.
The network is fixed internally to `logos.test`, whose fleet provides discovery and bootstrap; Octopus traffic uses
its own `/octopus/1/...` content topics. Coordinators are still needed for GitHub and scheduling. An explicit
`entry-node` list is added on top of the fleet entry nodes (logos-delivery concatenates both), so use it to connect to specific coordinators.

## Coordinator (public IP)
- `coordinator_config(public_ip, tcp_port=60000, entry_nodes=[other coordinators])`.
- Open TCP `tcp_port` (and UDP `tcp_port` for QUIC) in the firewall.
- There is no announce-address option (`ext-multiaddr` and `dns4-domain-name` are not exposed). The public IP is advertised with `nat = "extip:<ip>"`. Use a static IP; a DNS name cannot be advertised yet.
- The coordinator's multiaddr (`MyMultiaddresses` node info, `/ip4/<ip>/tcp/<port>/p2p/<peerId>`) is what runners use as entry node.

## Runner (laptop, behind NAT)
- `runner_config(entry_nodes=(), nat=None|"any")` (entry nodes optional). It dials out, so a plain NAT works without port forwarding: the coordinator relays gossip to it.
- Without `nat` (or `OCTOPUS_NAT`), a runner uses `nat = "none"`. The library's own default is `"any"` (UPnP/NAT-PMP), and its port-mapping service often crashes the node when it stops (#64).
- `nat="any"` additionally tries UPnP/NAT-PMP.
- Mode is `Core`. `Edge` (client-only) would save resources but has not been evaluated.

## Environment variables (`config.from_env`)
`OCTOPUS_ENTRY_NODES` (comma separated), `OCTOPUS_PUBLIC_IP` (coordinators), `OCTOPUS_TCP_PORT`, `OCTOPUS_NAT`, `OCTOPUS_ROOT_KEY` (runners, see below), `OCTOPUS_LOG_LEVEL` (all roles: the library's `log-level`, `INFO` by default).

The library is compiled with every TRACE line in it, so it filters by `log-level` only when it is also built with `-d:chronicles_runtime_filtering=on`. The CI and `lib-latest` builds are (`.github/actions/liblogosdelivery`); an older build writes thousands of TRACE and DEBUG lines a second whatever the level (#93).

## Coordinator discovery (signed adverts, pinned root key)

Runners do not need a coordinator list. The logos.test network is public, so anyone can publish on
the `/octopus/1/coordinators/proto` topic; trust therefore comes from a **root key** pinned in the
runner (`octopus runner setup --root-key <key>` or `OCTOPUS_ROOT_KEY`), not from the topic.

- **Root key** (Ed25519, offline): `octopus root keygen`. Signs one **certificate** per coordinator
  (`octopus root authorize --enc-pub ... --sign-pub ... --days 90`) binding the coordinator id, its
  X25519 encryption key (the key job offers are encrypted to, see `architecture.md`), its Ed25519
  signing key and an expiry.
- **Coordinator keys**: `octopus coordinator keygen` creates both keys and prints what the root holder
  needs. The coordinator publishes an **advert** every 30s: certificate, multiaddrs, capabilities,
  `sent_at`, `expires_at` (at most 5 minutes), signed with its signing key.
- **Runner**: `CoordinatorDirectory` verifies each advert and keeps live coordinators; a new one is
  trusted for job offers (`OfferReceiver.add_coordinator`). A coordinator disappears after 3 missed
  adverts. `OCTOPUS_ENTRY_NODES` stays available for extra connections.

What is rejected (counted per reason in `CoordinatorDirectory.rejections`): not signed by the pinned
root, tampered advert or certificate, id not derived from the certified key, expired advert or
certificate, advert dated in the future, replay (`sent_at` not newer than the last accepted),
oversized or malformed payload, revoked id, and floods (a global limit applied before any signature
check, plus 1 advert/s per coordinator).

Threats and limits:
- *Fake coordinator*: needs the root key or a stolen coordinator key; otherwise ignored.
- *Replay*: bounded by `expires_at` and the monotonic `sent_at`. The runner forgets `sent_at` on
  restart, so a captured advert can be replayed for up to its remaining 5 minutes at most.
- *Key compromise and revocation*: certificates expire (default 90 days, re-issue to rotate). There
  is no revocation list on the wire yet: the runner-side `revoked` set is the stop-gap, and rotating
  the coordinator's keys gives it a new id. A compromised **root key** needs a new root pinned on
  every runner.
- *Trust is additive*: a coordinator learned from an advert is trusted until the runner restarts,
  even after its advert expires; an expired certificate stops new adverts but does not close an
  already open channel.
- Not done yet: wiring the publisher into `octopus coordinator start`, a default root key shipped in
  the code, and backfilling recent adverts from a store node.

## What is and is not verified
- Verified: runner finds the coordinator via `entry-node` and receives its message (`pytest -m needs_lib`).
- AutoNAT and circuit relay are always enabled in the library (`waku_switch.nim`); DCUtR is referenced in `delivery_dialer.nim`. Direct runner-to-runner hole punching is **not** verified. Octopus does not rely on it: all traffic can go through the coordinator.
- `tests/nat/` checks the relay path with runners on separate networks (not run in CI yet; needs a Linux build of the library).
