"""Running a leg's script in a throwaway container instead of as the machine owner.

The node stays trusted: it clones the commit (with the owner's git credentials) and posts the
commit status. The container gets only the checkout, a clean environment and no credentials.
Linux hosts only: a macOS leg needs a macOS VM, which is not built yet.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass

RUNTIMES = ("docker", "podman")
HOME_IN_JOB = "/tmp"
WORK_IN_JOB = "/work"

# docker's own rules: a size like 512m or 4g, a number of cpus, a network name.
_SIZE = re.compile(r"[1-9][0-9]*[bkmg]?")
_CPUS = re.compile(r"[0-9]+(\.[0-9]+)?")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# repository[:tag][@digest], optionally with a registry host and port
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*(@sha256:[0-9a-f]{64})?")


class SandboxError(ValueError):
    pass


@dataclass(frozen=True)
class Sandbox:
    """How the container is run. `image` is the machine owner's choice, never the repo's."""

    image: str
    runtime: str = "docker"
    network: str = "bridge"  # "none" cuts the job off from the network
    memory: str = "4g"
    cpus: str = "2"
    pids: int = 1024

    def __post_init__(self) -> None:
        if self.runtime not in RUNTIMES:
            raise SandboxError(f"unknown container runtime: {self.runtime}")
        if not _IMAGE.fullmatch(self.image) or self.image.startswith("-"):
            raise SandboxError(f"invalid image name: {self.image}")
        if not _SIZE.fullmatch(self.memory):
            raise SandboxError(f"invalid memory limit: {self.memory}")
        if not _CPUS.fullmatch(self.cpus):
            raise SandboxError(f"invalid cpu limit: {self.cpus}")
        if not _NAME.fullmatch(self.network):
            raise SandboxError(f"invalid network: {self.network}")
        if self.pids < 1:
            raise SandboxError(f"invalid pids limit: {self.pids}")

    @staticmethod
    def supported() -> str | None:
        """Why this host cannot sandbox, or None."""
        if sys.platform != "linux":
            return "only Linux hosts can sandbox jobs for now; macOS jobs need a VM"
        return None

    def network_warning(self) -> str | None:
        """What a job can reach through the container network, or None when it has none."""
        if self.network == "none":
            return None
        return (
            f"with network {self.network} a job can reach services of this host (through the "
            "container gateway, including this node's own port) and, on a cloud VPS, the metadata "
            "service at 169.254.169.254 (instance credentials). Block them, or use --network none "
            "if the job needs no network: see docs/decentralized-ci.md"
        )

    def group_warning(self) -> str | None:
        """Rootful Docker makes its group root on the host: say so when this user is in it."""
        if self.runtime != "docker":
            return None
        try:
            import grp

            docker_gid = grp.getgrnam("docker").gr_gid
        except (ImportError, KeyError):
            return None
        if docker_gid not in os.getgroups():
            return None
        return (
            "this user is in the docker group, which is root on the host "
            "(docker run -v /:/host ...): the dedicated user stops being a boundary. Use rootless "
            "Podman or rootless Docker for the node's user: see docs/decentralized-ci.md"
        )

    def run_argv(self, name: str, work: str, script: str, env: dict[str, str]) -> list[str]:
        """The command that runs `script` (a path below `work`) in the container."""
        argv = [
            self.runtime,
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            f"{HOME_IN_JOB}:rw,exec,nosuid,size=2g",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--network",
            self.network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
        ]
        if self.runtime == "podman":
            argv += ["--userns", "keep-id"]
        argv += ["-v", f"{work}:{WORK_IN_JOB}:rw", "-w", WORK_IN_JOB, "-e", f"HOME={HOME_IN_JOB}"]
        for key, value in sorted(env.items()):
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "bash", f"{WORK_IN_JOB}/{script}"]
        return argv

    def kill_argv(self, name: str) -> list[str]:
        return [self.runtime, "rm", "-f", name]

    def pull_argv(self) -> list[str]:
        return [self.runtime, "pull", self.image]

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str) -> Sandbox:
        try:
            raw = json.loads(text)
            return cls(**raw)
        except (ValueError, TypeError) as e:
            raise SandboxError(f"could not read the sandbox settings: {e}") from e
