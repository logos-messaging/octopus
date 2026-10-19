from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

# Hard-coded network parameters (the values of the Logos test network), so Octopus
# does not depend on a library preset. Octopus traffic uses its own content topics.
CLUSTER_ID = 2
NUM_SHARDS = 8
DEFAULT_ENTRY_NODES = (
    "/dns4/node-01.do-ams3.logos.test.status.im/tcp/30303/p2p/16Uiu2HAmQ9X2xDfPG3uL77V9piYDhjq14JhKCtcmNYsTMKNqrKCj",
    "/dns4/node-02.do-ams3.logos.test.status.im/tcp/30303/p2p/16Uiu2HAmB8NYprrfQrgWVzsJtYWkfjsXbmJEGNMG6othXsQ53BwG",
    "/dns4/node-01.gc-us-central1-a.logos.test.status.im/tcp/30303/p2p/16Uiu2HAmF8WtwGPmeGHgYAX2277jHgy5cW9F7zsB8EqUjBZQAZQ3",
    "/dns4/node-02.gc-us-central1-a.logos.test.status.im/tcp/30303/p2p/16Uiu2HAmUuXhUW9bdJpzN1kfDziFiUZo4bszTk66cvr7uuyCHXR7",
    "/dns4/node-01.ac-cn-hongkong-c.logos.test.status.im/tcp/30303/p2p/16Uiu2HAmL3oU95jh1BZHozn3uNhx8HEneirgr8M1jEAapzXGDqRF",
    "/dns4/node-02.ac-cn-hongkong-c.logos.test.status.im/tcp/30303/p2p/16Uiu2HAm28CoBZjpyxsanC8tQpbvZ7bZJnVYuB1EgFzb571qpWsV",
)
DEFAULT_TCP_PORT = 60000

ENTRY_NODES_ENV = "OCTOPUS_ENTRY_NODES"
PUBLIC_IP_ENV = "OCTOPUS_PUBLIC_IP"
TCP_PORT_ENV = "OCTOPUS_TCP_PORT"
NAT_ENV = "OCTOPUS_NAT"


def _base(tcp_port: int, entry_nodes: Sequence[str], nat: str | None) -> dict:
    overrides: dict = {
        "cluster-id": CLUSTER_ID,
        "num-shards-in-network": NUM_SHARDS,
        "tcp-port": tcp_port,
        "entry-node": [*DEFAULT_ENTRY_NODES, *entry_nodes],
    }
    if nat:
        overrides["nat"] = nat
    return {"mode": "Core", "messagingOverrides": overrides}


def coordinator_config(
    public_ip: str,
    *,
    tcp_port: int = DEFAULT_TCP_PORT,
    entry_nodes: Sequence[str] = (),
) -> dict:
    """A coordinator has a public IP and acts as entry node and relay for NAT'd runners.

    logos-delivery has no announce-address option, so the public IP is advertised
    through `nat = extip:<ip>`. Other coordinators go in `entry_nodes`.
    """
    if not public_ip:
        raise ValueError("coordinator_config: public_ip is required")
    return _base(tcp_port, entry_nodes, f"extip:{public_ip}")


def runner_config(
    entry_nodes: Sequence[str],
    *,
    tcp_port: int = DEFAULT_TCP_PORT,
    nat: str | None = None,
) -> dict:
    """A runner dials out to the coordinators; `nat` is e.g. "any" to try UPnP/NAT-PMP."""
    if not entry_nodes:
        raise ValueError("runner_config: at least one entry node (coordinator) is required")
    return _base(tcp_port, entry_nodes, nat)


def from_env(role: str, env: Mapping[str, str] | None = None) -> dict:
    """Build the node config of `role` ("runner" or "coordinator") from OCTOPUS_* variables."""
    env = os.environ if env is None else env
    entry_nodes = [n.strip() for n in env.get(ENTRY_NODES_ENV, "").split(",") if n.strip()]
    port = int(env.get(TCP_PORT_ENV, DEFAULT_TCP_PORT))
    if role == "coordinator":
        return coordinator_config(
            env.get(PUBLIC_IP_ENV, ""), tcp_port=port, entry_nodes=entry_nodes
        )
    if role == "runner":
        return runner_config(entry_nodes, tcp_port=port, nat=env.get(NAT_ENV) or None)
    raise ValueError(f"from_env: unknown role: {role}")
