"""Turning the sandbox on: shared by `octopus ci sandbox enable` and the `octopus ci init` wizard."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

from .home import Home
from .sandbox import Sandbox

Pull = Callable[[list[str]], subprocess.CompletedProcess]


class SandboxSetupError(RuntimeError):
    """Why the sandbox could not be turned on, with the exit code a command should use."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _pull(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, timeout=1800)


def find_runtime(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The container runtime on this host. Podman first: it runs without a privileged group."""
    for runtime in ("podman", "docker"):
        if which(runtime):
            return runtime
    return None


def enable(
    home: Home,
    sandbox: Sandbox,
    *,
    pull: bool = True,
    run: Pull = _pull,
    which: Callable[[str], str | None] = shutil.which,
    say: Callable[[str], object] = lambda _text: None,
) -> list[str]:
    """Check the host, pull the image, save the settings; returns the warnings to show."""
    why = Sandbox.supported()
    if why is not None:
        raise SandboxSetupError(why, 3)
    if which(sandbox.runtime) is None:
        raise SandboxSetupError(f"{sandbox.runtime} is not installed or not on the PATH", 3)
    if pull:
        say(f"pulling {sandbox.image} ...")
        try:
            done = run(sandbox.pull_argv())
        except (OSError, subprocess.SubprocessError) as e:
            raise SandboxSetupError(f"could not pull the image: {e}", 4) from e
        if done.returncode != 0:
            raise SandboxSetupError(f"could not pull the image: exit code: {done.returncode}", 4)
    home.save_sandbox(sandbox)
    return [
        w
        for w in (sandbox.network_warning(), sandbox.group_warning(), sandbox.pin_hint())
        if w is not None
    ]
