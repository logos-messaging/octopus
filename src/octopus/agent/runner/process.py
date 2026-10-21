from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
JIT_ENV = "ACTIONS_RUNNER_INPUT_JITCONFIG"
_KILL_GRACE_S = 5
_LINKED_DIRS = ("bin", "externals")  # read-only runtime, hard-linked instead of copied
# Runner.Listener prints "<utc time>: Running job: <name>" when GitHub gives it a job.
JOB_STARTED = "Running job: "

OutputHandler = Callable[[str], None]


@dataclass
class ProcessOutcome:
    exit_code: int | None
    timed_out: bool = False
    stopped: bool = False
    last_line: str = ""
    # GitHub gave the runner no job before `start_timeout_s`.
    no_job: bool = False


def make_job_dir(cache: Path, job_dir: Path) -> None:
    """Fresh copy of the runner tree: actions/runner writes its config and _work beside itself."""

    def copy(src: str, dst: str) -> str:
        rel = Path(src).relative_to(cache)
        if rel.parts and rel.parts[0] in _LINKED_DIRS:
            try:
                os.link(src, dst)
                return dst
            except OSError:
                pass
        return shutil.copy2(src, dst)

    shutil.copytree(cache, job_dir, symlinks=True, copy_function=copy)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None and not IS_WINDOWS:
        # The leader is gone but children may remain in the group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_runner(
    job_dir: Path,
    jit_config: str,
    *,
    timeout_s: float,
    stop: threading.Event,
    on_output: OutputHandler,
    pass_as_arg: bool = False,
    on_started: Callable[[subprocess.Popen], None] | None = None,
    start_timeout_s: float | None = None,
) -> ProcessOutcome:
    """Run the runner in `job_dir` and wait for it. Raises OSError if it cannot start.

    With `start_timeout_s`, the runner is stopped when it shows no job in that time.
    """
    script = "run.cmd" if IS_WINDOWS else "run.sh"
    cmd = ["cmd.exe", "/c", script] if IS_WINDOWS else [str(job_dir / script)]
    env = dict(os.environ)
    if pass_as_arg:
        cmd += ["--jitconfig", jit_config]
    else:
        env[JIT_ENV] = jit_config
    popen_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if IS_WINDOWS
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        cmd,
        cwd=job_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        **popen_kwargs,
    )
    if on_started is not None:
        on_started(proc)
    last = {"line": ""}
    job_started = threading.Event()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.strip():
                last["line"] = line
            if JOB_STARTED in line:
                job_started.set()
            try:
                on_output(line)
            except Exception as e:  # noqa: BLE001 - a failing consumer must not kill the job
                log.warning("output handler failed: %s", type(e).__name__)

    reader = threading.Thread(target=pump, name="octopus-runner-output", daemon=True)
    reader.start()

    timed_out = stopped = no_job = False
    waited = 0.0
    while True:
        try:
            proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            waited += 0.2
        if stop.is_set():
            stopped = True
        elif waited >= timeout_s:
            timed_out = True
        elif (
            start_timeout_s is not None and waited >= start_timeout_s and not job_started.is_set()
        ):
            no_job = True
        if stopped or timed_out or no_job:
            kill_tree(proc)
            proc.wait()
            break
    if not IS_WINDOWS:
        kill_tree(proc)  # reap stragglers still in the process group
    reader.join(timeout=5)
    return ProcessOutcome(
        exit_code=proc.returncode,
        timed_out=timed_out,
        stopped=stopped,
        last_line=last["line"],
        no_job=no_job,
    )
