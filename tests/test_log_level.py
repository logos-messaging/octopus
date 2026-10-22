"""The library filters its output by the `log-level` Octopus passes (#93).

It needs a library built with chronicles runtime filtering, as the CI and `lib-latest` builds
are (.github/actions/liblogosdelivery). An older build writes every TRACE and DEBUG line.
"""

import socket
import time

import pytest

pytestmark = pytest.mark.needs_lib


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_info_hides_trace_and_debug(capfd, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from octopus.transport.logos import LogosConfig, LogosTransport

    node = LogosTransport(
        LogosConfig(
            overrides={
                "tcp-port": _free_port(socket.SOCK_STREAM),
                "discv5-udp-port": _free_port(socket.SOCK_DGRAM),
                "quic-port": _free_port(socket.SOCK_DGRAM),
                "nat": "none",
                "cluster-id": 4242,
                "num-shards-in-network": 1,
                "log-level": "INFO",
            }
        )
    )
    node.start()
    try:
        time.sleep(5)  # a fresh node is busy: dials, discovery, gossip
    finally:
        node.stop()
    # The library writes to file descriptor 1 itself, which capfd captures.
    lines = capfd.readouterr().out.splitlines()
    noisy = [line for line in lines if line.startswith(("TRC ", "DBG "))]
    assert any(line.startswith("INF ") for line in lines), "no library output was captured"
    assert not noisy, (
        f"{len(noisy)} TRACE/DEBUG lines at log-level INFO: was the library built with "
        f"-d:chronicles_runtime_filtering=on? First: {noisy[0][:160]}"
    )
