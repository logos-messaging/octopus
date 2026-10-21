"""What `octopus jobs`, `logs` and `explain` show, read from the coordinator's databases.

The stores are opened read-only, so inspecting is safe while the coordinator runs (SQLite
WAL lets readers and the writer work side by side) and works after it stopped.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..proto import FailureReason
from .jobs import Job, SqliteJobStore
from .jobs.model import TERMINAL
from .logs import LogStore
from .service import JOBS_DB, LOGS_DB

# One line on what each failure means and where to look.
REASON_HINTS: dict[FailureReason, str] = {
    FailureReason.NO_IDLE_RUNNER: (
        "no online runner matched the job's labels, OS and architecture in time"
    ),
    FailureReason.OFFER_TIMEOUT: "the runner did not answer the job offer in time",
    FailureReason.JITCONFIG_ERROR: (
        "GitHub refused to register the job's runner: check the token or App permissions "
        "(Administration: write)"
    ),
    FailureReason.RUNNER_DOWNLOAD_FAILED: "the runner host could not download actions/runner",
    FailureReason.RUNNER_START_FAILED: "actions/runner did not start on the runner host",
    FailureReason.POLICY_REJECTED: "the trust policy rejected the job (see the detail)",
    FailureReason.RUNNER_LOST: "the runner went offline while it had the job",
    FailureReason.JOB_FAILED: "the job ran and failed: see the log",
    FailureReason.CANCELLED: "GitHub cancelled the job before a runner took it",
}


# After a job ends its runner keeps sending the log for up to 5 minutes.
FOLLOW_IDLE_S = 360.0


class NoData(Exception):
    """There is no coordinator database where we looked."""


class Inspector:
    def __init__(self, directory: Path) -> None:
        jobs_db = directory / JOBS_DB
        if not jobs_db.is_file():
            raise NoData(f"no coordinator data in {directory}")
        try:
            self.jobs = SqliteJobStore(str(jobs_db), readonly=True)
            logs_db = directory / LOGS_DB
            self.logs = LogStore(str(logs_db), readonly=True) if logs_db.is_file() else None
        except sqlite3.Error as e:
            raise NoData(f"could not open coordinator data in {directory}: {e}") from e

    def close(self) -> None:
        self.jobs.close()
        if self.logs is not None:
            self.logs.close()

    def list_jobs(
        self,
        *,
        failed: bool = False,
        running: bool = False,
        repo: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        rows = self.jobs.list(failed=failed, running=running, repo=repo, limit=limit)
        # A job's records can be kept under another job's id (issue #58): show GitHub's id.
        return [{**job_dict(j), "id": self.jobs.viewed_as(j.id)} for j in rows]

    def explain(self, job_id: str, lines: int = 20) -> dict | None:
        data_id = self.jobs.resolve(job_id)
        job = self.jobs.get(data_id)
        if job is None:
            return None
        timeline = self.jobs.timeline(data_id)
        failure = next((e for e in reversed(timeline) if e.reason is not None), None)
        data = {**job_dict(job), "id": job_id}
        data["timeline"] = [
            {
                "ts": e.ts,
                "actor": e.actor,
                "state": e.state.value,
                "detail": e.detail,
                "reason": e.reason.value if e.reason else None,
            }
            for e in timeline
        ]
        data["failure"] = (
            {
                "reason": failure.reason.value,
                "detail": failure.detail,
                "hint": REASON_HINTS.get(failure.reason, ""),
            }
            if failure is not None and failure.reason is not None
            else None
        )
        data["log"] = self._log_summary(job, lines)
        return data

    def _log_summary(self, job: Job, lines: int) -> dict | None:
        if self.logs is None:
            return None
        streams = self.logs.streams(job.id)
        if not streams:
            return None
        st = streams[-1]
        return {
            "runner_id": st.runner_id,
            "complete": st.complete,
            "truncated": st.truncated,
            "gaps": [list(g) for g in self.logs.gaps(job.id, st.runner_id)],
            "attempts": len(streams),
            "tail": self.logs.tail(job.id, lines, runner_id=st.runner_id),
        }

    def log_chunks(self, job_id: str, runner_id: str | None = None, start: int = 0):
        if self.logs is None:
            return iter(())
        return self.logs.read(self.jobs.resolve(job_id), start, runner_id=runner_id)

    def follow(
        self,
        job_id: str,
        *,
        runner_id: str | None = None,
        poll_s: float = 1.0,
        idle_s: float = FOLLOW_IDLE_S,
        stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> Iterator[str]:
        """The job's log text as it arrives, until the job is over and its log complete.

        Only the gap-free prefix is printed, so text never comes out of order. When the job
        moves to another runner the new attempt's log follows a note. A finished job whose
        log stopped changing `idle_s` ago ends with a note that it is incomplete.
        """
        job_id = self.jobs.resolve(job_id)
        position, current = 0, runner_id
        while True:
            st = self.logs.state(job_id, runner_id) if self.logs is not None else None
            if st is not None and self.logs is not None:
                if current is None:
                    current = st.runner_id
                elif st.runner_id != current:
                    yield f"[octopus] job moved to runner {short(st.runner_id)}\n"
                    current, position = st.runner_id, 0
                for chunk in self.logs.read(job_id, position, st.contiguous, runner_id=current):
                    yield chunk.data
                    position = chunk.seq + 1
            job = self.jobs.get(job_id)
            if job is None or job.state in TERMINAL:
                if st is None:
                    return  # it never had a log (e.g. rejected before running)
                if st.complete and position > (st.final_seq or 0):
                    return
                if now() - st.updated_at >= idle_s:
                    yield "[octopus] log incomplete: the runner stopped sending\n"
                    return
            if stop():
                return
            sleep(poll_s)


def job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "repo": job.repo,
        "state": job.state.value,
        "reason": job.reason.value if job.reason else None,
        "runner_id": job.runner_id,
        "step": job.step,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


# --- text rendering ---------------------------------------------------------------------


def ago(ts: float, now: float) -> str:
    s = max(0, int(now - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def short(runner_id: str | None) -> str:
    return (runner_id or "-")[:8]


def render_jobs(rows: list[dict], now: float) -> str:
    if not rows:
        return "no jobs"
    header = ("JOB", "REPO", "STATE", "REASON", "RUNNER", "STEP", "UPDATED")
    table = [header] + [
        (
            r["id"],
            r["repo"],
            r["state"],
            r["reason"] or "-",
            short(r["runner_id"]),
            r["step"] or "-",
            ago(r["updated_at"], now),
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in table
    )


def render_explain(data: dict, now: float) -> str:
    state = data["state"] + (f" ({data['reason']})" if data["reason"] else "")
    out = [
        f"job      {data['id']}  {data['repo']}",
        f"state    {state}, updated {ago(data['updated_at'], now)}",
    ]
    if data["runner_id"] or data["step"]:
        out.append(f"runner   {short(data['runner_id'])}  step: {data['step'] or '-'}")
    failure = data.get("failure")
    if failure:
        out.append(f"why      {failure['reason']}: {failure['detail']}")
        if failure["hint"]:
            out.append(f"         {failure['hint']}")
    out += ["", "timeline"]
    start = data["timeline"][0]["ts"] if data["timeline"] else data["created_at"]
    width = max((len(e["state"]) for e in data["timeline"]), default=0)
    for e in data["timeline"]:
        detail = e["detail"] + (f" [{e['reason']}]" if e["reason"] else "")
        out.append(
            f"  {clock(e['ts'])}  +{e['ts'] - start:>6.1f}s  {e['state']:<{width}}  "
            f"{e['actor']}{': ' + detail if detail else ''}"
        )
    log = data.get("log")
    out.append("")
    if log is None:
        out.append("log      none received")
        return "\n".join(out)
    status = "complete" if log["complete"] else "incomplete"
    if log["gaps"]:
        status += ", missing chunks " + ", ".join(
            f"{a}" if a == b else f"{a}-{b}" for a, b in log["gaps"]
        )
    elif not log["complete"]:
        status += ", still streaming or cut off"
    if log["truncated"]:
        status += ", truncated"
    attempts = f"; {log['attempts']} attempts, showing the last" if log["attempts"] > 1 else ""
    out.append(f"log      runner {short(log['runner_id'])}, {status}{attempts}")
    out += [f"  | {line}" for line in log["tail"]]
    return "\n".join(out)
