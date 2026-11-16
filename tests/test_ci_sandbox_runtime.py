"""The container sandbox against a real Docker or Podman (#138, "Tests").

The other sandbox tests check the command line against a fake runtime. These run real legs
through `GitExecutor` and the real `Sandbox`, so they only run with OCTOPUS_TEST_CONTAINER=docker
or podman on a Linux host made for it (CI): they pull an image, open short-lived listeners and,
for the network tests, use passwordless sudo to set up a network namespace standing in for a LAN
host and a metadata service. They only ever remove the container of their own leg.

Escape attempts that must fail today are ordinary tests. The two that still work (the default
network reaching the host, a LAN host and a metadata-like address) are strict xfails that only
accept `GapStillOpen`: closing the gap turns them red, and any other failure is a real failure.
"""

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from test_ci_light import make_repo

from octopus.ci.engine import LegInfo
from octopus.ci.exec import GitExecutor
from octopus.ci.sandbox import Sandbox

RUNTIME = os.environ.get("OCTOPUS_TEST_CONTAINER", "")
IMAGE = os.environ.get(
    "OCTOPUS_TEST_CONTAINER_IMAGE",
    "docker.io/library/ubuntu:24.04"
    "@sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8",
)
# Off-host stand-ins, reached through routing like the real ones. Not 169.254.169.254 itself,
# so a cloud host (a CI runner) keeps its real metadata service while the test runs.
NETNS = "octopus-test-net"
LAN_HOST = "10.244.77.2"
METADATA_LIKE = "169.254.77.2"

pytestmark = [
    pytest.mark.needs_container,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or RUNTIME not in ("docker", "podman"),
        reason="runs a real runtime: set OCTOPUS_TEST_CONTAINER=docker or podman on Linux",
    ),
]


# A stand-in service inside the namespace: accepts and closes, like any TCP server.
STANDIN_SERVER = """\
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("{addr}", {port}))
s.listen()
while True:
    c, _ = s.accept()
    c.close()
"""


class GapStillOpen(AssertionError):
    """A gap #138 will close is still open: what the strict xfails accept, and nothing else."""


@pytest.fixture(scope="module", autouse=True)
def pulled_image():
    subprocess.run([RUNTIME, "pull", "-q", IMAGE], check=True, capture_output=True, timeout=900)


def linux_leg(repo: str, sha: str) -> LegInfo:
    return LegInfo(
        leg_id=f"{repo}:1:1:linux",
        repo=repo,
        sha=sha,
        ref="refs/heads/main",
        event="push",
        run_id="1",
        attempt="1",
        os="linux",
        reply_pub="x",
        workflow="w",
    )


def container_name(leg: LegInfo) -> str:
    """The name `GitExecutor` gives this leg's container (exec.py), so cleanup stays exact."""
    return "octopus-" + hashlib.sha256(leg.leg_id.encode()).hexdigest()[:12]


def containers_named(name: str) -> list[str]:
    out = subprocess.run(
        [RUNTIME, "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [n for n in out.split() if n == name]


def run_leg(tmp_path, script, *, network="bridge", timeout_s=600.0, should_stop=None):
    """Runs one leg; returns its result, its output and the leftovers of its own container."""
    src, sha = make_repo(tmp_path, script)
    leg = linux_leg(str(src), sha)
    lines: list[str] = []
    sandbox = Sandbox(image=IMAGE, runtime=RUNTIME, network=network)
    ex = GitExecutor(
        tmp_path / "work", url="file://{repo}", poll_s=0.05, timeout_s=timeout_s, sandbox=sandbox
    )
    try:
        result = ex.run(leg, lines.append, should_stop or (lambda: False))
        left = containers_named(container_name(leg))
    finally:
        for name in containers_named(container_name(leg)):
            subprocess.run([RUNTIME, "rm", "-f", name], capture_output=True, check=False)
    return result, "\n".join(lines), left


def probe(host: str, port: int) -> str:
    """A job line that says whether a TCP connection to host:port succeeds."""
    return (
        f"if timeout 5 bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>/dev/null; "
        f"then echo REACHED-{host}; else echo blocked-{host}; fi\n"
    )


def gateway_probe(port: int) -> str:
    """Probes the container's default gateway (docker0, 172.17.0.1 on stock Docker)."""
    return (
        "gw=$(awk '$2==\"00000000\"{print $3}' /proc/net/route | head -n1)\n"
        'gw=$(printf "%d.%d.%d.%d" 0x${gw:6:2} 0x${gw:4:2} 0x${gw:2:2} 0x${gw:0:2})\n'
        f'if timeout 5 bash -c "exec 3<>/dev/tcp/$gw/{port}" 2>/dev/null; '
        "then echo REACHED-gateway; else echo blocked-gateway; fi\n"
    )


def host_address() -> str:
    """This host's address on its default route: how a LAN client or the gateway reaches it."""
    out = subprocess.run(
        ["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True, check=True
    ).stdout.split()
    return out[out.index("src") + 1]


class Listener:
    """A TCP service on the host that accepts and closes, like a local dev server."""

    def __init__(self, host: str) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.close()

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept
        except OSError:
            pass
        self.sock.close()


def sudo(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True, check=False)


@pytest.fixture
def offhost():
    """A network namespace behind a veth pair: a LAN host and a metadata-like address, each
    with a listener, reached from containers through the host's routing (forwarding)."""
    if sudo("true").returncode != 0:
        pytest.skip("needs passwordless sudo to build the off-host stand-ins")
    if sudo("ip", "netns", "list").stdout.split().count(NETNS):
        pytest.skip(f"network namespace {NETNS} already exists: not ours, not touching it")
    host_if, ns_if = "octo-t0", "octo-t1"
    steps = [
        ("ip", "netns", "add", NETNS),
        ("ip", "link", "add", host_if, "type", "veth", "peer", "name", ns_if),
        ("ip", "link", "set", ns_if, "netns", NETNS),
        ("ip", "addr", "add", "10.244.77.1/30", "dev", host_if),
        ("ip", "link", "set", host_if, "up"),
        ("ip", "-n", NETNS, "addr", "add", f"{LAN_HOST}/30", "dev", ns_if),
        ("ip", "-n", NETNS, "addr", "add", f"{METADATA_LIKE}/32", "dev", ns_if),
        ("ip", "-n", NETNS, "link", "set", ns_if, "up"),
        ("ip", "-n", NETNS, "link", "set", "lo", "up"),
        ("ip", "-n", NETNS, "route", "add", "default", "via", "10.244.77.1"),
        ("ip", "route", "add", f"{METADATA_LIKE}/32", "via", LAN_HOST, "dev", host_if),
    ]
    listeners: list[subprocess.Popen] = []
    try:
        for step in steps:
            done = sudo(*step)
            assert done.returncode == 0, f"{' '.join(step)}: {done.stderr.strip()}"
        port = 18080
        for addr in (LAN_HOST, METADATA_LIKE):
            listeners.append(
                subprocess.Popen(
                    [
                        "sudo",
                        "-n",
                        "ip",
                        "netns",
                        "exec",
                        NETNS,
                        sys.executable,
                        "-c",
                        STANDIN_SERVER.format(addr=addr, port=port),
                    ],
                )
            )
        deadline = time.monotonic() + 15
        for addr in (LAN_HOST, METADATA_LIKE):
            while True:
                try:
                    socket.create_connection((addr, port), timeout=1).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, f"stand-in {addr}:{port} did not start"
                    time.sleep(0.2)
        yield port
    finally:
        # `ip netns exec` becomes the listener, so find it by its namespace, not its name
        for pid in sudo("ip", "netns", "pids", NETNS).stdout.split():
            sudo("kill", pid)
        for proc in listeners:
            proc.wait(timeout=10)
        sudo("ip", "route", "del", f"{METADATA_LIKE}/32")
        sudo("ip", "link", "del", host_if)
        sudo("ip", "netns", "del", NETNS)


def test_the_job_sees_only_its_checkout_and_leg_variables(tmp_path, monkeypatch):
    private = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".octopus-test-"))
    try:
        (private / "secret").write_text("secret")
        monkeypatch.setenv("GH_TOKEN", "OWNER-TOKEN")
        monkeypatch.setenv("OCTOPUS_TEST_OWNER_ONLY", "owner-value")
        script = (
            f"cat {private}/secret 2>/dev/null && echo READ-SECRET || echo secret-denied\n"
            "echo ghtoken=${GH_TOKEN:-none}\n"
            "echo owner-var=${OCTOPUS_TEST_OWNER_ONLY:-none}\n"
            "echo uid=$(id -u)\n"
            # the root mount itself, whatever user the job runs as (Podman adds tmpfs dirs)
            "awk '$2==\"/\"{print $4}' /proc/self/mounts | grep -qE '(^|,)ro(,|$)' "
            "&& echo root-read-only || echo ROOT-WRITABLE\n"
            "touch /work/ok && echo work-writable\n"
            "touch /tmp/ok && echo tmp-writable\n"
            "ls /var/run/docker.sock /run/podman/podman.sock 2>/dev/null "
            "&& echo RUNTIME-SOCKET || echo no-runtime-socket\n"
            # the bounding set: what a process could ever gain, even through a setuid binary
            "grep -q '^CapBnd:\\s*0000000000000000$' /proc/self/status "
            "&& echo no-capabilities || echo HAS-CAPABILITIES\n"
        )
        result, out, left = run_leg(tmp_path, script)
    finally:
        shutil.rmtree(private, ignore_errors=True)
    assert result.exit_code == 0, (result, out)
    assert "octopus: isolation: container" in out
    for expected in (
        "secret-denied",
        "ghtoken=none",
        "owner-var=none",
        "root-read-only",
        "work-writable",
        "tmp-writable",
        "no-runtime-socket",
        "no-capabilities",
    ):
        assert expected in out, out
    assert "uid=0" not in out, out
    assert left == [], f"container left after the leg: {left}"


def test_network_none_reaches_nothing(tmp_path):
    service = Listener(host_address())
    try:
        # control: the service answers from the host, so a block below is the sandbox's doing
        socket.create_connection((host_address(), service.port), timeout=5).close()
        script = probe(host_address(), service.port) + probe("1.1.1.1", 443)
        result, out, left = run_leg(tmp_path, script, network="none")
    finally:
        service.close()
    assert result.exit_code == 0, (result, out)
    assert f"blocked-{host_address()}" in out and "blocked-1.1.1.1" in out, out
    assert left == []


def test_the_default_network_reaches_the_internet(tmp_path):
    """Package managers need egress: what any network policy has to keep (the control)."""
    result, out, _ = run_leg(tmp_path, probe("1.1.1.1", 443))
    assert result.exit_code == 0, (result, out)
    assert "REACHED-1.1.1.1" in out, out


@pytest.mark.xfail(
    strict=True,
    raises=GapStillOpen,
    reason="the default network reaches services of the host (#138, task 5)",
)
def test_the_default_network_does_not_reach_host_services(tmp_path):
    service = Listener("0.0.0.0")
    try:
        script = probe(host_address(), service.port) + gateway_probe(service.port)
        result, out, _ = run_leg(tmp_path, script)
    finally:
        service.close()
    assert result.exit_code == 0, (result, out)
    assert ("REACHED-" in out or "blocked-" in out) and "gateway" in out, out
    if "REACHED-" in out:
        raise GapStillOpen(out)


@pytest.mark.xfail(
    strict=True,
    raises=GapStillOpen,
    reason="the default network reaches LAN and metadata-like addresses (#138, task 5)",
)
def test_the_default_network_does_not_reach_lan_or_metadata(tmp_path, offhost):
    script = probe(LAN_HOST, offhost) + probe(METADATA_LIKE, offhost)
    result, out, _ = run_leg(tmp_path, script)
    assert result.exit_code == 0, (result, out)
    assert LAN_HOST in out and METADATA_LIKE in out, out
    if "REACHED-" in out:
        raise GapStillOpen(out)


def test_a_failing_job_leaves_no_container(tmp_path):
    result, out, left = run_leg(tmp_path, "echo failing; exit 3\n")
    assert result.exit_code == 3, (result, out)
    assert left == [], f"container left after a failed leg: {left}"


def test_a_stopped_job_leaves_no_container(tmp_path):
    started = time.monotonic()
    marker = f"stop-{uuid.uuid4().hex}"
    result, out, left = run_leg(
        tmp_path,
        f"echo {marker}; sleep 300\n",
        should_stop=lambda: time.monotonic() - started > 15,
    )
    assert result.exit_code == 143 and result.detail == "stopped", (result, out)
    assert marker in out, out
    assert left == [], f"container left after a stopped leg: {left}"


def test_a_timed_out_job_leaves_no_container(tmp_path):
    result, out, left = run_leg(tmp_path, "sleep 300\n", timeout_s=15)
    assert result.exit_code == 143 and result.detail == "timed out", (result, out)
    assert left == [], f"container left after a timed-out leg: {left}"
