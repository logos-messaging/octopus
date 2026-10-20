from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

from .release import RunnerAsset


class RunnerDownloadError(RuntimeError):
    """The runner release could not be downloaded, verified or extracted."""


def default_data_dir() -> Path:
    env = os.environ.get("OCTOPUS_DATA_DIR")
    if env:
        return Path(env)
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "octopus"
    if os.uname().sysname == "Darwin":
        return home / "Library" / "Application Support" / "octopus"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "octopus"


def install_dir(data_dir: Path, asset: RunnerAsset) -> Path:
    return data_dir / "runner" / asset.version


def ensure_installed(
    asset: RunnerAsset, data_dir: Path, *, client: httpx.Client | None = None
) -> Path:
    """Return the cached runner tree, downloading and verifying it on first use."""
    dest = install_dir(data_dir, asset)
    if dest.is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".install-", dir=dest.parent))
    try:
        archive = work / asset.name
        _download(asset, archive, client)
        staging = work / "tree"
        staging.mkdir()
        _extract(archive, staging, asset.is_zip)
        try:
            staging.rename(dest)
        except OSError:
            if not dest.is_dir():  # a concurrent install may have won the rename
                raise
    except RunnerDownloadError:
        raise
    except Exception as e:
        raise RunnerDownloadError(f"could not install runner: {asset.name}: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _prune_old(dest.parent, keep=dest)
    return dest


def _download(asset: RunnerAsset, path: Path, client: httpx.Client | None) -> None:
    digest = hashlib.sha256()
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60)
    try:
        with client.stream("GET", asset.url) as resp:
            if resp.status_code != 200:
                raise RunnerDownloadError(
                    f"could not download runner: HTTP {resp.status_code}: {asset.url}"
                )
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes():
                    digest.update(chunk)
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise RunnerDownloadError(f"could not download runner: {asset.url}: {e}") from e
    finally:
        if own:
            client.close()
    if digest.hexdigest() != asset.sha256:
        raise RunnerDownloadError(
            f"could not verify runner checksum: {asset.name}: "
            f"expected {asset.sha256}, got {digest.hexdigest()}"
        )


def _check_member(name: str, root: Path) -> None:
    target = (root / name).resolve()
    if os.path.isabs(name) or not target.is_relative_to(root.resolve()):
        raise RunnerDownloadError(f"could not extract runner: unsafe path in archive: {name}")


def _extract(archive: Path, root: Path, is_zip: bool) -> None:
    try:
        if is_zip:
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    _check_member(name, root)
                z.extractall(root)
        else:
            with tarfile.open(archive) as t:
                for m in t.getmembers():
                    _check_member(m.name, root)
                    if m.issym() or m.islnk():
                        link = m.linkname if m.islnk() else os.path.join(
                            os.path.dirname(m.name), m.linkname
                        )
                        _check_member(link, root)
                    if m.isdev():
                        raise RunnerDownloadError(
                            f"could not extract runner: device file in archive: {m.name}"
                        )
                if hasattr(tarfile, "data_filter"):
                    t.extractall(root, filter="data")
                else:
                    # Python < 3.10.12 has no extraction filters; every member was
                    # checked above (paths, links, devices).
                    t.extractall(root)
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
        raise RunnerDownloadError(f"could not extract runner: {archive.name}: {e}") from e


def _prune_old(parent: Path, keep: Path) -> None:
    for entry in parent.iterdir():
        if entry.is_dir() and entry != keep and not entry.name.startswith(".install-"):
            shutil.rmtree(entry, ignore_errors=True)
