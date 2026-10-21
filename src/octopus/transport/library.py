"""Where the logos-delivery Python bindings and liblogosdelivery live, and how to fetch the library.

The bindings load `<root>/lib/liblogosdelivery.so`, where `<root>` is the directory that contains
their `logosdelivery` package: the vendored checkout in a git clone, or site-packages in a pip,
pipx or uv install.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

BINDINGS_ENV = "OCTOPUS_BINDINGS"
VENDORED = Path(__file__).resolve().parents[3] / "vendor" / "logos-delivery-python-bindings"
RELEASE_REPO = "logos-messaging/octopus"
# Moving release the library workflow keeps up to date with the pinned bindings.
DEFAULT_TAG = "lib-latest"
SUMS_ASSET = "SHA256SUMS"
MAX_LIBRARY_BYTES = 256 * 1024 * 1024


class LibraryError(RuntimeError):
    """The library could not be located, fetched or installed."""


@dataclass(frozen=True)
class Status:
    root: Path | None
    library: Path | None
    library_present: bool
    asset: str | None


def _arch(machine: str) -> str:
    machine = machine.lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    raise LibraryError(f"no prebuilt library for architecture: {machine}")


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    """Release asset for this platform; the bindings only load Linux and macOS libraries."""
    system = (system or platform.system()).lower()
    arch = _arch(machine or platform.machine())
    if system == "linux":
        return f"liblogosdelivery-linux-{arch}.so"
    if system == "darwin":
        return f"liblogosdelivery-macos-{arch}.dylib"
    raise LibraryError(
        f"no prebuilt library for platform: {system}: the bindings load only Linux and macOS"
    )


def bindings_root(environ: dict[str, str] | None = None) -> Path | None:
    """OCTOPUS_BINDINGS, else the vendored checkout, else where `logosdelivery` is installed."""
    environ = os.environ if environ is None else environ
    if environ.get(BINDINGS_ENV):
        return Path(environ[BINDINGS_ENV]).expanduser().resolve()
    if (VENDORED / "logosdelivery").is_dir():
        return VENDORED
    try:
        spec = importlib.util.find_spec("logosdelivery")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        return Path(spec.origin).resolve().parent.parent
    return None


def library_path(root: Path) -> Path:
    return root / "lib" / "liblogosdelivery.so"


def status(environ: dict[str, str] | None = None) -> Status:
    root = bindings_root(environ)
    try:
        asset: str | None = asset_name()
    except LibraryError:
        asset = None
    lib = library_path(root) if root is not None else None
    return Status(root, lib, lib is not None and lib.exists(), asset)


def install_file(src: Path, root: Path) -> Path:
    """Copy a built library to where the bindings load it."""
    if not src.is_file():
        raise LibraryError(f"could not install the library, not a file: {src}")
    lib = library_path(root)
    lib.parent.mkdir(parents=True, exist_ok=True)
    tmp = lib.with_name(lib.name + ".tmp")
    shutil.copyfile(src, tmp)
    if src.suffix == ".dylib":
        # The bindings always ask for the .so name: keep the real file as .dylib.
        dylib = lib.with_suffix(".dylib")
        os.replace(tmp, dylib)
        lib.unlink(missing_ok=True)
        lib.symlink_to(dylib.name)
    else:
        os.replace(tmp, lib)
    return lib


def _get(client: httpx.Client, url: str) -> bytes:
    try:
        with client.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > MAX_LIBRARY_BYTES:
                    raise LibraryError(f"could not download: {url}: larger than the limit")
            return bytes(data)
    except httpx.HTTPError as e:
        raise LibraryError(f"could not download: {url}: {e}") from e


def parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def download(
    tag: str = DEFAULT_TAG,
    dest_dir: Path | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
) -> Path:
    """Fetch this platform's library from the Octopus release and verify its SHA-256."""
    asset = asset_name()
    base = base_url or f"https://github.com/{RELEASE_REPO}/releases/download/{tag}"
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        sums = parse_sums(_get(client, f"{base}/{SUMS_ASSET}").decode("utf-8", "replace"))
        expected = sums.get(asset)
        if expected is None:
            raise LibraryError(f"could not find the checksum, release: {tag}: asset: {asset}")
        data = _get(client, f"{base}/{asset}")
    finally:
        if own:
            client.close()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise LibraryError(
            f"checksum mismatch: asset: {asset}: expected: {expected}: got: {actual}"
        )
    out = Path(dest_dir or tempfile.mkdtemp(prefix="octopus-lib-")) / asset
    out.write_bytes(data)
    return out
