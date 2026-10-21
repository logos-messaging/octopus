from __future__ import annotations

import hashlib
import http.server
import io
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest

from octopus.agent.runner import RunnerManager
from octopus.agent.runner.download import RunnerDownloadError, ensure_installed
from octopus.agent.runner.release import (
    UnsupportedPlatformError,
    asset_for,
    platform_key,
)
from octopus.proto import FailureReason

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake runner is a POSIX shell script")

JIT = "eyJ0b3AtLXNlY3JldCJ9-jit-config"

RUN_SH = """#!/bin/sh
if [ "$FAKE_MODE" = idle ]; then
  echo "Listening for Jobs"
  sleep 60 &
  echo $! > "$FAKE_PIDFILE"
  echo "$$" >> "$FAKE_PIDFILE"
  wait
fi
echo "Running job: build"
echo "config=$ACTIONS_RUNNER_INPUT_JITCONFIG args=$*"
case "$FAKE_MODE" in
  ok) echo "Job completed"; exit 0 ;;
  fail) echo "Error: step failed"; exit 3 ;;
  hang)
    sleep 60 &
    echo $! > "$FAKE_PIDFILE"
    echo "$$" >> "$FAKE_PIDFILE"
    wait ;;
esac
"""


def make_archive(files: dict[str, bytes | str], executable=("run.sh",)) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            raw = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o755 if name in executable else 0o644
            t.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


@pytest.fixture
def fake_archive() -> bytes:
    return make_archive(
        {"run.sh": RUN_SH, "bin/Runner.Listener": "binary", "externals/node/node": "node"}
    )


class Server:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    servers: list[Server] = []

    def _serve(payload: bytes, status: int = 200) -> Server:
        s = Server(payload, status)
        servers.append(s)
        return s

    yield _serve
    for s in servers:
        s.close()


def asset_for_payload(server: Server, payload: bytes, sha: str | None = None):
    digest = sha or hashlib.sha256(payload).hexdigest()
    return asset_for("linux-x64", version="9.9.9", checksums={"linux-x64": digest}, base_url=server.url)


def manager(tmp_path, server, payload, **kw) -> RunnerManager:
    return RunnerManager(tmp_path / "data", asset=asset_for_payload(server, payload), **kw)


# release table


def test_platform_key():
    assert platform_key("Darwin", "arm64") == "osx-arm64"
    assert platform_key("Linux", "x86_64") == "linux-x64"
    assert platform_key("Windows", "AMD64") == "win-x64"
    with pytest.raises(UnsupportedPlatformError):
        platform_key("Plan9", "mips")


def test_asset_names():
    assert asset_for("win-x64").name.endswith(".zip")
    assert asset_for("osx-arm64").name.endswith(".tar.gz")
    assert "/v2.338.0/actions-runner-linux-x64-2.338.0.tar.gz" in asset_for("linux-x64").url


# download


def test_download_and_cache_hit(tmp_path, serve, fake_archive):
    server = serve(fake_archive)
    asset = asset_for_payload(server, fake_archive)
    first = ensure_installed(asset, tmp_path)
    assert (first / "run.sh").is_file()
    assert ensure_installed(asset, tmp_path) == first
    assert server.hits == 1


def test_checksum_mismatch_caches_nothing(tmp_path, serve, fake_archive):
    server = serve(fake_archive)
    asset = asset_for_payload(server, fake_archive, sha="0" * 64)
    with pytest.raises(RunnerDownloadError, match="checksum"):
        ensure_installed(asset, tmp_path)
    assert not (tmp_path / "runner" / "9.9.9").exists()
    assert [p for p in (tmp_path / "runner").iterdir()] == []


def test_http_error(tmp_path, serve):
    server = serve(b"nope", status=404)
    with pytest.raises(RunnerDownloadError, match="404"):
        ensure_installed(asset_for_payload(server, b"nope"), tmp_path)


def test_truncated_archive(tmp_path, serve, fake_archive):
    bad = fake_archive[: len(fake_archive) // 2]
    server = serve(bad)
    with pytest.raises(RunnerDownloadError, match="extract"):
        ensure_installed(asset_for_payload(server, bad), tmp_path)
    assert not (tmp_path / "runner" / "9.9.9").exists()


def test_unsafe_archive_member_rejected(tmp_path, serve):
    evil = make_archive({"../escape.txt": "x"})
    server = serve(evil)
    with pytest.raises(RunnerDownloadError, match="unsafe"):
        ensure_installed(asset_for_payload(server, evil), tmp_path)
    assert not (tmp_path / "escape.txt").exists()


def test_old_versions_pruned(tmp_path, serve, fake_archive):
    old = tmp_path / "runner" / "1.0.0"
    old.mkdir(parents=True)
    server = serve(fake_archive)
    ensure_installed(asset_for_payload(server, fake_archive), tmp_path)
    assert not old.exists()


def test_manager_reports_download_failure(tmp_path, serve, fake_archive):
    server = serve(fake_archive)
    asset = asset_for_payload(server, fake_archive, sha="0" * 64)
    result = RunnerManager(tmp_path / "data", asset=asset).run_job("j1", JIT)
    assert result.failure == FailureReason.RUNNER_DOWNLOAD_FAILED
    assert JIT not in result.detail


# running


def job_dirs(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "data").glob("job-*")]


@posix_only
def test_run_ok_streams_output_and_cleans_up(tmp_path, serve, fake_archive, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    server = serve(fake_archive)
    lines: list[str] = []
    result = manager(tmp_path, server, fake_archive).run_job("j1", JIT, on_output=lines.append)
    assert result.failure is None and result.exit_code == 0
    assert lines[0] == "Running job: build" and lines[-1] == "Job completed"
    assert job_dirs(tmp_path) == []


@posix_only
def test_jit_config_never_leaks(tmp_path, serve, fake_archive, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_MODE", "fail")
    server = serve(fake_archive)
    lines: list[str] = []
    with caplog.at_level("DEBUG"):
        result = manager(tmp_path, server, fake_archive).run_job("j1", JIT, on_output=lines.append)
    assert any("config=***" in line for line in lines)
    assert JIT not in "\n".join(lines) + result.detail + caplog.text


@posix_only
def test_jit_as_arg_fallback(tmp_path, serve, fake_archive, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    server = serve(fake_archive)
    lines: list[str] = []
    manager(tmp_path, server, fake_archive, pass_jit_as_arg=True).run_job(
        "j1", JIT, on_output=lines.append
    )
    assert any("args=--jitconfig ***" in line for line in lines)


@posix_only
def test_nonzero_exit_is_job_failed(tmp_path, serve, fake_archive, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    server = serve(fake_archive)
    result = manager(tmp_path, server, fake_archive).run_job("j1", JIT)
    assert result.failure == FailureReason.JOB_FAILED
    assert result.exit_code == 3
    assert "Error: step failed" in result.detail
    assert job_dirs(tmp_path) == []


@posix_only
def test_cannot_start_is_runner_start_failed(tmp_path, serve, monkeypatch):
    payload = make_archive({"run.sh": "#!/nonexistent/interpreter\n", "bin/x": "x"})
    server = serve(payload)
    result = manager(tmp_path, server, payload).run_job("j1", JIT)
    assert result.failure == FailureReason.RUNNER_START_FAILED
    assert job_dirs(tmp_path) == []


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pids: list[int], timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


@posix_only
def test_timeout_kills_process_tree(tmp_path, serve, fake_archive, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    server = serve(fake_archive)
    result = manager(tmp_path, server, fake_archive, max_job_seconds=1).run_job("j1", JIT)
    assert result.timed_out and result.failure == FailureReason.JOB_FAILED
    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2 and wait_dead(pids)
    assert job_dirs(tmp_path) == []


@posix_only
def test_runner_with_no_job_is_stopped_and_frees_the_slot(tmp_path, serve, fake_archive, monkeypatch):
    # GitHub gave the JIT runner no job: the run was cancelled, or another runner took it (#73).
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_MODE", "idle")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    server = serve(fake_archive)
    result = manager(tmp_path, server, fake_archive, job_start_seconds=1).run_job("j1", JIT)
    assert result.failure == FailureReason.CANCELLED and not result.timed_out
    assert "no job in 1 s" in result.detail
    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2 and wait_dead(pids)
    assert job_dirs(tmp_path) == []


@posix_only
def test_a_job_that_started_is_not_stopped_by_the_start_timeout(
    tmp_path, serve, fake_archive, monkeypatch
):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    server = serve(fake_archive)
    result = manager(
        tmp_path, server, fake_archive, job_start_seconds=0.5, max_job_seconds=2
    ).run_job("j1", JIT)
    assert result.timed_out and result.failure == FailureReason.JOB_FAILED


@posix_only
def test_stop_kills_process_tree(tmp_path, serve, fake_archive, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    server = serve(fake_archive)
    mgr = manager(tmp_path, server, fake_archive)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=mgr.run_job("j1", JIT)))
    t.start()
    end = time.time() + 10
    while not pidfile.exists() and time.time() < end:
        time.sleep(0.1)
    mgr.stop()
    t.join(timeout=15)
    assert out["r"].failure == FailureReason.RUNNER_LOST
    pids = [int(p) for p in pidfile.read_text().split()]
    assert wait_dead(pids)
    assert job_dirs(tmp_path) == []


@posix_only
def test_failing_output_consumer_does_not_break_job(tmp_path, serve, fake_archive, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    server = serve(fake_archive)

    def boom(line: str) -> None:
        raise RuntimeError("consumer bug")

    result = manager(tmp_path, server, fake_archive).run_job("j1", JIT, on_output=boom)
    assert result.failure is None
    assert job_dirs(tmp_path) == []


@posix_only
def test_cache_not_modified_by_job(tmp_path, serve, fake_archive, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    server = serve(fake_archive)
    mgr = manager(tmp_path, server, fake_archive)
    cache = mgr.ensure_installed()
    before = sorted(p.name for p in cache.rglob("*"))
    mgr.run_job("j1", JIT)
    assert sorted(p.name for p in cache.rglob("*")) == before
