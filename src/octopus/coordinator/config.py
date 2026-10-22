"""Coordinator settings, from `OCTOPUS_*` variables (environment over an env file).

`octopus github setup` writes the token-mode variables to `coordinator.env`; the GitHub
App variables are set by hand (docs/github-app.md). Secrets stay `SecretStr`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from ..crypto.signing import decode_signing_public
from ..github.app import DEFAULT_API_URL
from ..transport.config import (
    DEFAULT_TCP_PORT,
    ENTRY_NODES_ENV,
    PUBLIC_IP_ENV,
    TCP_PORT_ENV,
    coordinator_config,
)
from .picker import PICK_LABEL_PREFIX
from .webhook_server import DEFAULT_PATH

REPO_ENV = "OCTOPUS_GITHUB_REPO"
ALLOWED_REPOS_ENV = "OCTOPUS_ALLOWED_REPOS"
ALLOWED_ORGS_ENV = "OCTOPUS_ALLOWED_ORGS"
WEBHOOK_SECRET_ENV = "OCTOPUS_WEBHOOK_SECRET"
# The previous secret, accepted too while a rotation is under way.
WEBHOOK_SECRET_PREVIOUS_ENV = "OCTOPUS_WEBHOOK_SECRET_PREVIOUS"
WEBHOOK_URL_ENV = "OCTOPUS_WEBHOOK_URL"
WEBHOOK_LISTEN_ENV = "OCTOPUS_WEBHOOK_LISTEN"
TOKEN_ENV = "OCTOPUS_GITHUB_TOKEN"
APP_ID_ENV = "OCTOPUS_GITHUB_APP_ID"
APP_KEY_FILE_ENV = "OCTOPUS_GITHUB_APP_KEY_FILE"
API_URL_ENV = "OCTOPUS_GITHUB_API_URL"
# Other coordinators of the cluster: `id=signing-public-key`, comma separated. Empty: one coordinator.
PEERS_ENV = "OCTOPUS_COORDINATOR_PEERS"
# OIDC pick mode (docs/oidc-pick.md): on when the audience is set; needs no GitHub credential.
OIDC_AUDIENCE_ENV = "OCTOPUS_OIDC_AUDIENCE"
# The `octopus-<name>` labels that pick mode may give out, comma separated. Required in pick mode.
PICK_LABELS_ENV = "OCTOPUS_PICK_LABELS"

DEFAULT_LISTEN = "127.0.0.1:8080"
COORDINATOR_DIR = "coordinator"


class ConfigError(ValueError):
    """The coordinator settings are missing or invalid. Never contains a secret."""


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host:
        raise ConfigError(f"invalid {WEBHOOK_LISTEN_ENV}, expected host:port: {value}")
    try:
        number = int(port)
    except ValueError:
        raise ConfigError(f"invalid {WEBHOOK_LISTEN_ENV} port: {value}") from None
    if not 0 <= number < 65536:
        raise ConfigError(f"invalid {WEBHOOK_LISTEN_ENV} port: {value}")
    return host.strip("[]"), number


def parse_peers(value: str | None) -> dict[str, str]:
    """`id=public-key,...` to a dict. Raises ConfigError on a malformed entry or key."""
    peers: dict[str, str] = {}
    for entry in _list(value):
        cid, sep, key = entry.partition("=")
        cid, key = cid.strip(), key.strip()
        if not (cid and sep and key):
            raise ConfigError(f"invalid {PEERS_ENV} entry, expected id=public-key: {entry}")
        try:
            decode_signing_public(key)
        except ValueError as e:
            raise ConfigError(f"invalid {PEERS_ENV} key for coordinator: {cid}: {e}") from None
        peers[cid] = key
    return peers


def coordinator_dir(data_dir: Path | None = None) -> Path:
    """Where the coordinator keeps its key, databases and state; apart from a runner's."""
    from ..agent.runner import default_data_dir

    return (data_dir or default_data_dir()) / COORDINATOR_DIR


@dataclass
class CoordinatorConfig:
    webhook_secrets: list[SecretStr] = field(default_factory=list, repr=False)
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    webhook_path: str = DEFAULT_PATH
    webhook_url: str = ""
    github_token: SecretStr | None = field(default=None, repr=False)
    app_id: str = ""
    app_key_file: Path | None = None
    api_url: str = DEFAULT_API_URL
    allowed_repos: list[str] = field(default_factory=list)
    allowed_orgs: list[str] = field(default_factory=list)
    public_ip: str = ""
    tcp_port: int = DEFAULT_TCP_PORT
    entry_nodes: list[str] = field(default_factory=list)
    # coordinator id -> Ed25519 public key of the other coordinators (see PEERS_ENV)
    peers: dict[str, str] = field(default_factory=dict)
    oidc_audience: str = ""
    # the unique labels of the registered manual runners (see PICK_LABELS_ENV)
    pick_labels: list[str] = field(default_factory=list)

    @property
    def pick_enabled(self) -> bool:
        return bool(self.oidc_audience)

    @property
    def auth_mode(self) -> str:
        if self.app_id:
            return "app"
        return "token" if self.github_token is not None else "oidc"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CoordinatorConfig:
        """Read and validate the settings. Raises ConfigError."""
        secrets = [env.get(WEBHOOK_SECRET_ENV, ""), env.get(WEBHOOK_SECRET_PREVIOUS_ENV, "")]
        host, port = parse_listen(env.get(WEBHOOK_LISTEN_ENV) or DEFAULT_LISTEN)
        try:
            tcp_port = int(env.get(TCP_PORT_ENV) or DEFAULT_TCP_PORT)
        except ValueError:
            raise ConfigError(f"invalid {TCP_PORT_ENV}: {env.get(TCP_PORT_ENV)}") from None
        token = env.get(TOKEN_ENV, "").strip()
        key_file = env.get(APP_KEY_FILE_ENV, "").strip()
        config = cls(
            webhook_secrets=[SecretStr(s.strip()) for s in secrets if s.strip()],
            listen_host=host,
            listen_port=port,
            webhook_url=env.get(WEBHOOK_URL_ENV, "").strip(),
            github_token=SecretStr(token) if token else None,
            app_id=env.get(APP_ID_ENV, "").strip(),
            app_key_file=Path(key_file) if key_file else None,
            api_url=env.get(API_URL_ENV, "").strip() or DEFAULT_API_URL,
            allowed_repos=_list(env.get(REPO_ENV)) + _list(env.get(ALLOWED_REPOS_ENV)),
            allowed_orgs=_list(env.get(ALLOWED_ORGS_ENV)),
            public_ip=env.get(PUBLIC_IP_ENV, "").strip(),
            tcp_port=tcp_port,
            entry_nodes=_list(env.get(ENTRY_NODES_ENV)),
            peers=parse_peers(env.get(PEERS_ENV)),
            oidc_audience=env.get(OIDC_AUDIENCE_ENV, "").strip(),
            pick_labels=_list(env.get(PICK_LABELS_ENV)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.webhook_secrets:
            raise ConfigError(
                f"no webhook secret: set {WEBHOOK_SECRET_ENV} (octopus github setup writes it)"
            )
        if self.app_id:
            if self.app_key_file is None:
                raise ConfigError(f"{APP_ID_ENV} is set but {APP_KEY_FILE_ENV} is not")
            if not self.app_key_file.is_file():
                raise ConfigError(f"{APP_KEY_FILE_ENV}: no such file: {self.app_key_file}")
        elif self.github_token is None and not self.pick_enabled:
            raise ConfigError(
                f"no GitHub credentials: set {TOKEN_ENV} (token setup) or {APP_ID_ENV} and "
                f"{APP_KEY_FILE_ENV} (GitHub App), or {OIDC_AUDIENCE_ENV} (OIDC pick mode)"
            )
        if self.pick_enabled and not self.pick_labels:
            raise ConfigError(
                f"pick mode is on but no runner label is allowed: set {PICK_LABELS_ENV} to the "
                "octopus-<name> labels of the runners you registered"
            )
        for label in self.pick_labels:
            if not label.casefold().startswith(PICK_LABEL_PREFIX):
                raise ConfigError(f"invalid pick label, expected {PICK_LABEL_PREFIX}<name>: {label}")
        if not self.allowed_repos and not self.allowed_orgs:
            raise ConfigError(
                f"nothing is allowed, every job would be rejected: set {REPO_ENV}, "
                f"{ALLOWED_REPOS_ENV} or {ALLOWED_ORGS_ENV}"
            )
        for repo in self.allowed_repos:
            owner, sep, name = repo.partition("/")
            if not (owner and sep and name):
                raise ConfigError(f"invalid repo, expected owner/name: {repo}")
        if not self.public_ip:
            raise ConfigError(
                f"no public IP: set {PUBLIC_IP_ENV}; runners reach the coordinator there"
            )
        if not 0 < self.tcp_port < 65536:
            raise ConfigError(f"invalid {TCP_PORT_ENV}, expected 1-65535: {self.tcp_port}")

    def node_config(self, storage_path: Path | None = None) -> dict:
        return coordinator_config(
            self.public_ip,
            tcp_port=self.tcp_port,
            entry_nodes=self.entry_nodes,
            storage_path=storage_path,
        )


def merged_env(env_file: Path | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env file's values with the process environment on top."""
    from ..github.setup import read_env_file

    values: dict[str, str] = {}
    if env_file is not None:
        try:
            values = read_env_file(env_file)
        except OSError as e:
            raise ConfigError(f"could not read {env_file}: {e.strerror}") from None
    environ = os.environ if environ is None else environ
    values.update({k: v for k, v in environ.items() if k.startswith("OCTOPUS_")})
    return values
