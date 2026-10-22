from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

# Octopus always runs on the logos.test network (cluster, shards and fleet entry nodes).
PRESET = "logos.test"
DEFAULT_TCP_PORT = 60000

ENTRY_NODES_ENV = "OCTOPUS_ENTRY_NODES"
PUBLIC_IP_ENV = "OCTOPUS_PUBLIC_IP"
TCP_PORT_ENV = "OCTOPUS_TCP_PORT"
NAT_ENV = "OCTOPUS_NAT"
ROOT_KEY_ENV = "OCTOPUS_ROOT_KEY"
RUNNER_DEFAULT_NAT = "none"
LOG_LEVEL_ENV = "OCTOPUS_LOG_LEVEL"
# The library writes TRACE and DEBUG lines nonstop (thousands a second); INFO keeps its
# lifecycle messages, warnings and errors.
DEFAULT_LOG_LEVEL = "INFO"
# The chronicles levels, which the library's `log-level` accepts in any case.
LOG_LEVELS = ("NONE", "TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "ERROR", "FATAL")


def env_log_level(env: Mapping[str, str] | None = None) -> str:
    """The library log level: OCTOPUS_LOG_LEVEL, else INFO. Raises ValueError if invalid.

    The library refuses to start a node with an unknown level, so it is checked here first.
    """
    env = os.environ if env is None else env
    value = (env.get(LOG_LEVEL_ENV) or DEFAULT_LOG_LEVEL).strip().upper()
    if value not in LOG_LEVELS:
        raise ValueError(
            f"invalid {LOG_LEVEL_ENV}, expected one of {', '.join(LOG_LEVELS)}: {value}"
        )
    return value


def _base(
    tcp_port: int,
    entry_nodes: Sequence[str],
    nat: str | None,
    storage_path: Path | None,
    log_level: str | None = None,
) -> dict:
    # The level only filters in a library built with chronicles runtime filtering, as the
    # release and CI builds are (.github/actions/liblogosdelivery).
    overrides: dict = {"tcp-port": tcp_port, "log-level": log_level or env_log_level()}
    if entry_nodes:
        overrides["entry-node"] = list(entry_nodes)
    if nat:
        overrides["nat"] = nat
    if storage_path is not None:
        # The node keeps the state of its reliable channels (SDS) there. The library's
        # default is ./data in the working dir, which two processes can share.
        overrides["local-storage-path"] = str(storage_path)
    return {"mode": "Core", "preset": PRESET, "messagingOverrides": overrides}


def coordinator_config(
    public_ip: str,
    *,
    tcp_port: int = DEFAULT_TCP_PORT,
    entry_nodes: Sequence[str] = (),
    storage_path: Path | None = None,
    log_level: str | None = None,
) -> dict:
    """A coordinator has a public IP and acts as entry node and relay for NAT'd runners.

    logos-delivery has no announce-address option, so the public IP is advertised
    through `nat = extip:<ip>`. Other coordinators go in `entry_nodes`.
    """
    if not public_ip:
        raise ValueError("coordinator_config: public_ip is required")
    return _base(tcp_port, entry_nodes, f"extip:{public_ip}", storage_path, log_level)


def runner_config(
    entry_nodes: Sequence[str] = (),
    *,
    tcp_port: int = DEFAULT_TCP_PORT,
    nat: str | None = None,
    storage_path: Path | None = None,
    log_level: str | None = None,
) -> dict:
    """A runner dials out, so a plain NAT works. `entry_nodes` are extra connections, added to the default fleet nodes.

    `nat` is e.g. "any" to try UPnP/NAT-PMP. The default is "none": the library's own
    default is "any", and its port-mapping service often crashes the node when it stops
    (#64). A runner does not need a port mapping, because the coordinators relay for it.
    """
    return _base(tcp_port, entry_nodes, nat or RUNNER_DEFAULT_NAT, storage_path, log_level)


def from_env(role: str, env: Mapping[str, str] | None = None) -> dict:
    """Build the node config of `role` ("runner" or "coordinator") from OCTOPUS_* variables."""
    env = os.environ if env is None else env
    entry_nodes = [n.strip() for n in env.get(ENTRY_NODES_ENV, "").split(",") if n.strip()]
    port = int(env.get(TCP_PORT_ENV, DEFAULT_TCP_PORT))
    level = env_log_level(env)
    if role == "coordinator":
        return coordinator_config(
            env.get(PUBLIC_IP_ENV, ""), tcp_port=port, entry_nodes=entry_nodes, log_level=level
        )
    if role == "runner":
        return runner_config(
            entry_nodes, tcp_port=port, nat=env.get(NAT_ENV) or None, log_level=level
        )
    raise ValueError(f"from_env: unknown role: {role}")
