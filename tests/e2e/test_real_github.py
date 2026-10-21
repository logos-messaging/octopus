"""Nightly end-to-end test against a real GitHub repo (#26). See docs/e2e.md.

One coordinator and one runner run on this machine as `octopus` processes, with the real
liblogosdelivery. The test pushes a branch `octopus-e2e/pass/...` to the e2e repo, which
starts the workflow `octopus-e2e-pass` (job `pass`). Then it pushes `octopus-e2e/fail/...`,
which starts `octopus-e2e-fail` (job `fail`, a step exits 3). The relay (`relay.py`) brings the webhook deliveries from GitHub to the coordinator.

Settings (environment):
- `OCTOPUS_E2E_REPO`: the e2e repo, owner/name.
- `OCTOPUS_E2E_TOKEN`: a token for that repo (see docs/e2e.md for its permissions).
- `OCTOPUS_E2E_DATA_DIR`: optional. The runner data dir. Keep it between runs, so that the
  runner does not download `actions/runner` each time.
- `OCTOPUS_E2E_ARTIFACTS`: optional. The process logs go there (else to `tmp_path`).
- `OCTOPUS_E2E_REQUIRED=1`: fail, not skip, when the settings are missing.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from octopus.github.setup import read_env_file
from octopus.github.token import TokenClient

from .relay import DeliveryRelay
from .repo import BRANCH_PREFIX, E2ERepo

pytestmark = pytest.mark.e2e

# GitHub records deliveries for this URL. It never resolves (RFC 2606).
WEBHOOK_URL = "https://octopus-e2e.invalid/github/webhook"
# The first run downloads actions/runner, so the jobs can take some minutes.
JOBS_TIMEOUT_S = 20 * 60
SETTLE_TIMEOUT_S = 3 * 60
# The prefixes of the variables that the outer CI job sets. The runner gets none of them,
# so that the inner `actions/runner` does not use the settings of the outer job.
DROPPED_ENV = ("GITHUB_", "RUNNER_", "ACTIONS_", "OCTOPUS_")


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        message = f"{name} is not set (see docs/e2e.md)"
        if os.environ.get("OCTOPUS_E2E_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return value


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROPPED_ENV)}
    env.update(extra)
    return env


def _wait(what: str, check: Callable[[], Any], timeout_s: float, poll_s: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f}s: {what}")
        time.sleep(poll_s)


class Octopus:
    """Runs `octopus` commands and keeps the long-running ones."""

    def __init__(self, logs: Path) -> None:
        self.logs = logs
        self._procs: list[tuple[str, subprocess.Popen]] = []

    @staticmethod
    def argv(*args: str) -> list[str]:
        return [sys.executable, "-c", "from octopus.cli import app; app()", *args]

    def run(self, *args: str, env: dict[str, str], check: bool = True) -> str:
        proc = subprocess.run(
            self.argv(*args), env=env, capture_output=True, text=True, timeout=120, check=False
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"octopus {args[0]} {args[1] if len(args) > 1 else ''} exited "
                f"{proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc.stdout

    def start(self, name: str, *args: str, env: dict[str, str]) -> subprocess.Popen:
        out = open(self.logs / f"{name}.log", "w")  # noqa: SIM115 - closed with the process
        proc = subprocess.Popen(
            self.argv(*args), env=env, stdout=out, stderr=subprocess.STDOUT, text=True
        )
        self._procs.append((name, proc))
        return proc

    def stop_all(self) -> None:
        for _, proc in reversed(self._procs):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for name, proc in reversed(self._procs):
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                print(f"e2e: killed {name}: it did not stop in 60s")

    def check_alive(self) -> None:
        for name, proc in self._procs:
            if proc.poll() is not None:
                tail = (self.logs / f"{name}.log").read_text()[-3000:]
                raise AssertionError(f"{name} exited {proc.returncode}:\n{tail}")


@pytest.fixture
def e2e(tmp_path: Path) -> Iterator[dict[str, Any]]:
    repo_name = _setting("OCTOPUS_E2E_REPO")
    token = _setting("OCTOPUS_E2E_TOKEN")
    logs = Path(os.environ.get("OCTOPUS_E2E_ARTIFACTS") or tmp_path / "logs")
    logs.mkdir(parents=True, exist_ok=True)
    runner_data = Path(os.environ.get("OCTOPUS_E2E_DATA_DIR") or tmp_path / "runner-data")
    github = TokenClient(token)
    repo = E2ERepo(github, repo_name)
    repo.check_workflows()

    octopus = Octopus(logs)
    env_file = tmp_path / "coordinator.env"
    coord_dir = tmp_path / "coordinator-data"
    runner_config = tmp_path / "runner" / "runner.json"
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{os.getpid()}"
    pushed: list[tuple[str, str]] = []  # (branch, sha)
    ctx: dict[str, Any] = {"repo": repo, "octopus": octopus, "coord_dir": coord_dir}
    relay = None
    try:
        octopus.run(
            "github", "setup", "--repo", repo_name, "--webhook-url", WEBHOOK_URL,
            "--env-file", str(env_file), "--no-example-pr", "--no-ping", "--yes",
            env=_clean_env(GITHUB_TOKEN=token),
        )  # fmt: skip
        listen = f"127.0.0.1:{_free_port()}"
        octopus.start(
            "coordinator", "coordinator", "start", "--env-file", str(env_file),
            "--listen", listen, "--data-dir", str(coord_dir),
            env=_clean_env(OCTOPUS_PUBLIC_IP="127.0.0.1", OCTOPUS_TCP_PORT=str(_free_port())),
        )  # fmt: skip

        def coordinator_state() -> dict | None:
            octopus.check_alive()
            out = octopus.run(
                "coordinator", "status", "--json", "--data-dir", str(coord_dir),
                env=_clean_env(), check=False,
            )  # fmt: skip
            state = json.loads(out)["state"] if out.strip() else None
            return state if state and state.get("multiaddrs") else None

        state = _wait("coordinator start", coordinator_state, 120)
        entry = next(a for a in state["multiaddrs"] if "/tcp/" in a)
        runner_env = _clean_env(
            OCTOPUS_DATA_DIR=str(runner_data), OCTOPUS_TCP_PORT=str(_free_port())
        )
        octopus.run(
            "runner", "setup", "--config", str(runner_config),
            "--coordinator", state["public_key"], "--entry-node", entry, env=runner_env,
        )  # fmt: skip
        octopus.run("runner", "allow", repo_name, "--config", str(runner_config), env=runner_env)
        octopus.start("runner", "runner", "start", "--config", str(runner_config), env=runner_env)
        _wait(
            "runner online at the coordinator",
            lambda: (coordinator_state() or {}).get("runners_online", 0) >= 1,
            180,
        )

        secret = read_env_file(env_file)["OCTOPUS_WEBHOOK_SECRET"].encode()
        relay = DeliveryRelay(
            github, repo_name, repo.hook_id(WEBHOOK_URL), f"http://{listen}/github/webhook", secret
        )
        relay.skip_existing()
        relay.start()

        def push(workflow: str) -> str:
            branch = f"{BRANCH_PREFIX}{workflow}/{stamp}"
            sha = repo.push_branch(branch)
            pushed.append((branch, sha))
            return sha

        ctx.update(push=push, relay=relay)
        yield ctx
    finally:
        if relay is not None:
            relay.stop()
            print(f"e2e: relayed {len(relay.relayed)} deliveries, re-signed {relay.resigned}")
        octopus.stop_all()
        for branch, sha in pushed:
            repo.cancel_runs(sha)
            repo.delete_branch(branch)
        octopus.run(
            "github", "teardown", "--repo", repo_name, "--env-file", str(env_file), "--yes",
            env=_clean_env(GITHUB_TOKEN=token), check=False,
        )  # fmt: skip
        github.close()


def _explain(ctx: dict[str, Any], job_id: int) -> dict | None:
    out = ctx["octopus"].run(
        "explain", str(job_id), "--json", "--data-dir", str(ctx["coord_dir"]),
        env=_clean_env(), check=False,
    )  # fmt: skip
    data = json.loads(out) if out.strip() else None
    if data and data["state"] in ("finished", "failed") and (data.get("log") or {}).get("complete"):
        return data
    return None


def _no_lost_jobs(ctx: dict[str, Any]) -> Callable[[], None]:
    """Stop the wait if a process exits, or if Octopus gives up a job for a reason other
    than `job-failed`. GitHub keeps such a job in the queue, so the wait cannot end."""

    def check() -> None:
        ctx["octopus"].check_alive()
        out = ctx["octopus"].run(
            "jobs", "--failed", "--json", "--data-dir", str(ctx["coord_dir"]),
            env=_clean_env(), check=False,
        )  # fmt: skip
        for job in json.loads(out) if out.strip() else []:
            if job["reason"] != "job-failed":
                why = ctx["octopus"].run(
                    "explain", str(job["id"]), "--data-dir", str(ctx["coord_dir"]),
                    env=_clean_env(), check=False,
                )  # fmt: skip
                raise AssertionError(f"octopus gave up a job:\n{why}")

    return check


def test_jobs_run_on_an_octopus_runner(e2e: dict[str, Any]) -> None:
    """A passing job, then a failing job, run on the runner. Octopus records both correctly.

    The jobs run one after the other. GitHub gives a queued job to any runner with the
    labels of the job, so two jobs at the same time can each take the runner that Octopus
    registered for the other job.
    """
    repo: E2ERepo = e2e["repo"]
    octopus: Octopus = e2e["octopus"]
    data_dir = ("--data-dir", str(e2e["coord_dir"]))
    lost = _no_lost_jobs(e2e)

    jobs = repo.wait_jobs(e2e["push"]("pass"), {"pass"}, JOBS_TIMEOUT_S, check=lost)
    passed_job = jobs["pass"]
    assert passed_job["conclusion"] == "success", passed_job
    passed = _wait("pass job settled", lambda: _explain(e2e, passed_job["id"]), SETTLE_TIMEOUT_S)
    assert passed["state"] == "finished", passed
    assert passed["failure"] is None, passed
    assert passed["step"] == "pass", passed
    log = octopus.run("logs", str(passed_job["id"]), *data_dir, env=_clean_env())
    assert "Running job: pass" in log, log
    assert "Job pass completed with result: Succeeded" in log, log

    jobs = repo.wait_jobs(e2e["push"]("fail"), {"fail"}, JOBS_TIMEOUT_S, check=lost)
    failed_job = jobs["fail"]
    assert failed_job["conclusion"] == "failure", failed_job
    failed = _wait("fail job settled", lambda: _explain(e2e, failed_job["id"]), SETTLE_TIMEOUT_S)
    assert failed["state"] == "failed", failed
    assert failed["failure"]["reason"] == "job-failed", failed
    assert failed["failure"]["detail"] == "job fail completed with result: Failed", failed
    assert failed["step"] == "fail", failed

    names = {passed_job["runner_name"], failed_job["runner_name"]}
    _wait(
        f"runners deregistered: {sorted(names)}",
        lambda: not names & repo.runner_names(),
        SETTLE_TIMEOUT_S,
        poll_s=5,
    )
    relayed = {r.job_id for r in e2e["relay"].relayed if r.action == "queued"}
    assert {passed_job["id"], failed_job["id"]} <= relayed
