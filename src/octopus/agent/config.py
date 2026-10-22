from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..crypto import KeyMaterialError, decode_public_key, decode_signing_public, node_id
from ..transport.config import (
    DEFAULT_TCP_PORT,
    ENTRY_NODES_ENV,
    NAT_ENV,
    ROOT_KEY_ENV,
    TCP_PORT_ENV,
    env_log_level,
    runner_config,
)
from .trust import default_config_path


class ConfigError(ValueError):
    """The runner settings are unreadable or invalid."""


def settings_path(allowlist_path: Path | None = None) -> Path:
    """Settings live next to the allowlist, so `--config` selects both."""
    base = allowlist_path or default_config_path()
    return base.with_name(f"{base.stem}.settings.json")


def coordinator_id(pubkey: str) -> str:
    try:
        return node_id(decode_public_key(pubkey))
    except KeyMaterialError as e:
        raise ConfigError(f"could not parse coordinator key: {pubkey}: {e}") from e


# max_jobs = 0 means no limit on parallel jobs. Coordinators compare busy with capacity, so an
# unlimited runner advertises this many slots.
UNLIMITED_JOBS = 0
UNLIMITED_CAPACITY = 1024


def parse_max_jobs(text: str) -> int:
    """A number of at least 1, or "unlimited" (stored as 0)."""
    value = text.strip().lower()
    if value in ("unlimited", "no limit"):
        return UNLIMITED_JOBS
    try:
        number = int(value)
    except ValueError as e:
        raise ConfigError(f"invalid max jobs, expected a number or unlimited: {text}") from e
    if number < 1:
        raise ConfigError(f"invalid max jobs, expected at least 1 or unlimited: {text}")
    return number


def format_max_jobs(max_jobs: int) -> str:
    return "unlimited" if max_jobs == UNLIMITED_JOBS else str(max_jobs)


@dataclass
class RunnerConfig:
    entry_nodes: list[str] = field(default_factory=list)
    # coordinator public keys: a coordinator's id is derived from its key
    coordinators: list[str] = field(default_factory=list)
    # Pinned root key (Ed25519, base64url): coordinators it authorises are discovered from adverts.
    root_key: str | None = None
    labels: list[str] = field(default_factory=list)
    max_jobs: int = 1
    tcp_port: int = DEFAULT_TCP_PORT
    nat: str | None = None

    @property
    def capacity(self) -> int:
        """Job slots to advertise to coordinators."""
        return UNLIMITED_CAPACITY if self.max_jobs == UNLIMITED_JOBS else self.max_jobs

    def has_free_slot(self, active: int) -> bool:
        return self.max_jobs == UNLIMITED_JOBS or active < self.max_jobs

    def validate(self) -> None:
        if self.max_jobs < UNLIMITED_JOBS:
            raise ConfigError(
                f"invalid max_jobs, expected at least 1 or 0 for unlimited: {self.max_jobs}"
            )
        if not 0 < self.tcp_port < 65536:
            raise ConfigError(f"invalid tcp_port, expected 1-65535: {self.tcp_port}")
        for pubkey in self.coordinators:
            coordinator_id(pubkey)
        if self.root_key is not None:
            try:
                decode_signing_public(self.root_key)
            except KeyMaterialError as e:
                raise ConfigError(f"could not parse root key: {e}") from e

    def trusted_coordinators(self) -> dict[str, str]:
        return {coordinator_id(k): k for k in self.coordinators}

    def node_config(self, storage_path: Path | None = None) -> dict:
        return runner_config(
            self.entry_nodes, tcp_port=self.tcp_port, nat=self.nat, storage_path=storage_path
        )

    @classmethod
    def load(cls, path: Path) -> RunnerConfig:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            raise ConfigError(f"could not read settings: {path}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"could not read settings: not an object: {path}")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ConfigError(f"could not read settings: unknown keys: {path}: {unknown}")
        try:
            config = cls(**raw)
        except TypeError as e:
            raise ConfigError(f"could not read settings: {path}: {e}") from e
        config.validate()
        return config

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.__dict__, indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def merged(
        self,
        env: Mapping[str, str],
        *,
        entry_nodes: Sequence[str] = (),
        coordinators: Sequence[str] = (),
        labels: Sequence[str] = (),
        max_jobs: int | None = None,
        root_key: str | None = None,
    ) -> RunnerConfig:
        """Precedence: flags over environment over the saved file."""
        env_nodes = [n.strip() for n in env.get(ENTRY_NODES_ENV, "").split(",") if n.strip()]
        try:
            port = int(env[TCP_PORT_ENV]) if env.get(TCP_PORT_ENV) else self.tcp_port
        except ValueError as e:
            raise ConfigError(f"invalid {TCP_PORT_ENV}: {env[TCP_PORT_ENV]}") from e
        try:
            env_log_level(env)  # read again by node_config(); fail here, before the node
        except ValueError as e:
            raise ConfigError(str(e)) from None
        merged = RunnerConfig(
            entry_nodes=list(entry_nodes) or env_nodes or list(self.entry_nodes),
            coordinators=list(coordinators) or list(self.coordinators),
            root_key=root_key or env.get(ROOT_KEY_ENV) or self.root_key,
            labels=list(labels) or list(self.labels),
            max_jobs=max_jobs if max_jobs is not None else self.max_jobs,
            tcp_port=port,
            nat=env.get(NAT_ENV) or self.nat,
        )
        merged.validate()
        return merged
