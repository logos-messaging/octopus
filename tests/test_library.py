import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest

from octopus.transport import library


def test_asset_names():
    assert library.asset_name("Linux", "x86_64") == "liblogosdelivery-linux-x86_64.so"
    assert library.asset_name("Linux", "AMD64") == "liblogosdelivery-linux-x86_64.so"
    assert library.asset_name("Darwin", "arm64") == "liblogosdelivery-macos-arm64.dylib"
    assert library.asset_name("Darwin", "aarch64") == "liblogosdelivery-macos-arm64.dylib"


def test_no_asset_for_windows_or_unknown_cpu():
    with pytest.raises(library.LibraryError, match="Linux and macOS"):
        library.asset_name("Windows", "AMD64")
    with pytest.raises(library.LibraryError, match="architecture: riscv64"):
        library.asset_name("Linux", "riscv64")


def test_bindings_root_prefers_the_environment(tmp_path):
    assert library.bindings_root({"OCTOPUS_BINDINGS": str(tmp_path)}) == tmp_path.resolve()


def test_library_path_is_beside_the_package(tmp_path):
    assert library.library_path(tmp_path) == tmp_path / "lib" / "liblogosdelivery.so"


def test_install_file_so(tmp_path):
    src = tmp_path / "built.so"
    src.write_bytes(b"lib")
    out = library.install_file(src, tmp_path / "root")
    assert out.read_bytes() == b"lib" and not out.is_symlink()


def test_install_file_dylib_keeps_the_real_file_and_links_the_so_name(tmp_path):
    src = tmp_path / "liblogosdelivery.dylib"
    src.write_bytes(b"lib")
    out = library.install_file(src, tmp_path / "root")
    assert out.is_symlink() and out.read_bytes() == b"lib"
    assert (out.parent / "liblogosdelivery.dylib").read_bytes() == b"lib"
    library.install_file(src, tmp_path / "root")  # again: replaces the link


def test_install_file_missing_source(tmp_path):
    with pytest.raises(library.LibraryError, match="not a file"):
        library.install_file(tmp_path / "nope", tmp_path)


def test_parse_sums():
    assert library.parse_sums("AB  x.so\ncd *y.dylib\n\njunk\n") == {"x.so": "ab", "y.dylib": "cd"}


def _client(files: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(library.platform, "system", lambda: "Linux")
    monkeypatch.setattr(library.platform, "machine", lambda: "x86_64")
    return "liblogosdelivery-linux-x86_64.so"


def test_download_verifies_and_saves(tmp_path, linux):
    data = b"native-library"
    sums = f"{hashlib.sha256(data).hexdigest()}  {linux}\n".encode()
    out = library.download(
        "lib-latest", tmp_path, _client({linux: data, "SHA256SUMS": sums}), "https://x/r"
    )
    assert out.name == linux and out.read_bytes() == data


def test_download_rejects_a_wrong_checksum(tmp_path, linux):
    sums = f"{'0' * 64}  {linux}\n".encode()
    with pytest.raises(library.LibraryError, match="checksum mismatch"):
        library.download("t", tmp_path, _client({linux: b"x", "SHA256SUMS": sums}), "https://x/r")
    assert not (tmp_path / linux).exists()


def test_download_without_a_checksum_for_the_asset(tmp_path, linux):
    with pytest.raises(library.LibraryError, match="no library for this platform.*--file"):
        library.download("t", tmp_path, _client({linux: b"x", "SHA256SUMS": b""}), "https://x/r")


def test_download_http_error_names_the_url(tmp_path, linux):
    with pytest.raises(library.LibraryError, match="could not download: https://x/r/SHA256SUMS"):
        library.download("t", tmp_path, _client({}), "https://x/r")


def test_cli_lib_install_from_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    monkeypatch.setenv("OCTOPUS_BINDINGS", str(tmp_path / "root"))
    src = tmp_path / "x.so"
    src.write_bytes(b"lib")
    res = CliRunner().invoke(app, ["lib", "install", "--file", str(src)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "root" / "lib" / "liblogosdelivery.so").read_bytes() == b"lib"
    res = CliRunner().invoke(app, ["lib", "status"])
    assert res.exit_code == 0 and "present" in res.output


def test_cli_lib_status_reports_missing(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from octopus.cli import app

    monkeypatch.setenv("OCTOPUS_BINDINGS", str(tmp_path))
    res = CliRunner().invoke(app, ["lib", "status"])
    assert res.exit_code == 1 and "missing" in res.output


def test_pyproject_pins_the_same_bindings_commit_as_the_submodule():
    root = Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD:vendor/logos-delivery-python-bindings"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    assert f"logos-delivery-python-bindings/archive/{sha}.tar.gz" in (root / "pyproject.toml").read_text()


def test_download_enforces_the_limit_and_keeps_no_partial_file(tmp_path, linux, monkeypatch):
    data = b"0123456789"
    sums = f"{hashlib.sha256(data).hexdigest()}  {linux}\n".encode()
    files = {linux: data, "SHA256SUMS": sums}
    monkeypatch.setattr(library, "MAX_LIBRARY_BYTES", 9)
    with pytest.raises(library.LibraryError, match="larger than 9 bytes"):
        library.download("t", tmp_path, _client(files), "https://x/r")
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(library, "MAX_LIBRARY_BYTES", 10)
    assert library.download("t", tmp_path, _client(files), "https://x/r").read_bytes() == data


def test_the_limit_fits_the_release_build():
    # The Linux library of the release workflow is about 270 MB (279182592 bytes).
    assert library.MAX_LIBRARY_BYTES > 300 * 1000 * 1000


def test_a_failed_download_leaves_no_partial_file(tmp_path, linux):
    sums = f"{'0' * 64}  {linux}\n".encode()
    with pytest.raises(library.LibraryError, match="checksum mismatch"):
        library.download("t", tmp_path, _client({linux: b"x", "SHA256SUMS": sums}), "https://x/r")
    assert list(tmp_path.iterdir()) == []
