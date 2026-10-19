from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

# Octopus always runs on the logos.test network (cluster, shards and fleet entry nodes).
PRESET = "logos.test"
DEFAULT_TCP_PORT = 60000

ENTRY_NODES_ENV = "OCTOPUS_ENTRY_NODES"
PUBLIC_IP_ENV = "OCTOPUS_PUBLIC_IP"
TCP_PORT_ENV = "OCTOPUS_TCP_PORT"
NAT_ENV = "OCTOPUS_NAT"


def _base(tcp_port: int, entry_nodes: Sequence[str], nat: str | None) -> dict:
    overrides: dict = {"tcp-port": tcp_port}
    if entry_nodes:
        overrides["entry-node"] = list(entry_nodes)
    if nat:
        overrides["nat"] = nat
    return {"mode": "Core", "preset": PRESET, "messagingOverrides": overrides}


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
    entry_nodes: Sequence[str] = (),
    *,
    tcp_port: int = DEFAULT_TCP_PORT,
    nat: str | None = None,
) -> dict:
    """A runner dials out, so a plain NAT works. `entry_nodes` are extra connections, added to the default fleet nodes.

    `nat` is e.g. "any" to try UPnP/NAT-PMP.
    """
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
