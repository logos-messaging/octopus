from __future__ import annotations

import platform
from dataclasses import dataclass

# Pinned actions/runner release. Refresh with scripts/update_runner_version.py and review the diff:
# the checksums are never looked up at runtime.
RUNNER_VERSION = "2.338.0"
RUNNER_SHA256 = {
    "win-x64": "f48e0750a21812bca5f82de5f7f5aeae71abee647fab5a582f1742d07eba455f",
    "win-arm64": "a57a4d29c0dcf02210a16b01906ed76cb8091b26d728c41232736c0f627a557e",
    "osx-x64": "dea7a58796ce215fc424a8b27c0fdd9b813fa5fc03d109a4a1d38131d6bfa1a3",
    "osx-arm64": "df4cebda25c86a886ed204e49fee63f5c2e7cec5f447b5c98440a826bbdf9df2",
    "linux-x64": "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32",
    "linux-arm64": "628b4a7258487b80c1d3c221095a7ded349f5ae0175fd3dfab708d07428f041b",
}
RELEASE_BASE_URL = "https://github.com/actions/runner/releases/download"

_OS = {"darwin": "osx", "linux": "linux", "windows": "win"}
_ARCH = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}


class UnsupportedPlatformError(RuntimeError):
    """No actions/runner release for this OS and architecture."""


@dataclass(frozen=True)
class RunnerAsset:
    key: str  # e.g. "linux-x64"
    version: str
    name: str
    url: str
    sha256: str

    @property
    def is_zip(self) -> bool:
        return self.name.endswith(".zip")


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    try:
        return f"{_OS[system]}-{_ARCH[machine]}"
    except KeyError:
        raise UnsupportedPlatformError(
            f"could not pick a runner release for platform: {system}/{machine}"
        ) from None


def asset_for(
    key: str,
    *,
    version: str = RUNNER_VERSION,
    checksums: dict[str, str] | None = None,
    base_url: str = RELEASE_BASE_URL,
) -> RunnerAsset:
    checksums = RUNNER_SHA256 if checksums is None else checksums
    if key not in checksums:
        raise UnsupportedPlatformError(f"could not find a runner checksum for platform: {key}")
    ext = "zip" if key.startswith("win-") else "tar.gz"
    name = f"actions-runner-{key}-{version}.{ext}"
    return RunnerAsset(
        key=key,
        version=version,
        name=name,
        url=f"{base_url}/v{version}/{name}",
        sha256=checksums[key],
    )
