"""Announce a manual runner's unique label, so a coordinator in OIDC pick mode can offer it.

Only presence is published: the runner still takes its jobs from GitHub, not from the
coordinator (docs/oidc-pick.md).
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..crypto import encode_public_key, load_or_create_private_key, node_id
from ..transport import Transport
from .presence import PresencePublisher, RunnerInfo
from .runner_agent import KEY_FILE

TransportFactory = Callable[[], Transport]


def default_transport_factory(data_dir: Path) -> TransportFactory:
    def make() -> Transport:
        from ..transport.logos import LogosTransport
        from .config import RunnerConfig, settings_path

        settings = RunnerConfig.load(settings_path())
        return LogosTransport(settings.node_config(data_dir / "node"))

    return make


@contextmanager
def announce(
    label: str,
    data_dir: Path,
    factory: TransportFactory | None = None,
    *,
    target: str | None = None,
) -> Iterator[str]:
    """Publish presence with `label` while the block runs. Yields the runner id.

    `target` is the repo (`owner/name`) or org (`owner`) the runner is registered on, so that a
    coordinator in pick mode gives it only jobs of that repo or org.
    """
    key = load_or_create_private_key(data_dir / KEY_FILE)
    transport = (factory or default_transport_factory(data_dir))()
    transport.start()
    publisher = PresencePublisher(
        transport,
        RunnerInfo(
            runner_id=node_id(key.public_key()),
            os=platform.system().lower(),
            arch=platform.machine().lower(),
            labels=(label,),
            pubkey=encode_public_key(key.public_key()),
            target=target,
        ),
    )
    publisher.start()
    try:
        yield node_id(key.public_key())
    finally:
        publisher.stop()
        transport.stop()
