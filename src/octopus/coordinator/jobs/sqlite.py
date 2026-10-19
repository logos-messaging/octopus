"""SQLite implementation of JobStore.

Purpose: persist jobs and their append-only event timeline so the history survives a
coordinator restart; validates transitions and purges old finished jobs (retention).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable

from octopus.proto import FailureReason

from .model import ALLOWED, TERMINAL, Event, IllegalTransition, Job, State, UnknownJob
from .store import JobStore

DEFAULT_RETENTION_S = 30 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
  runner_id TEXT, step TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL NOT NULL,
  actor TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, reason TEXT);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at);
"""


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        repo=row["repo"],
        state=State(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reason=FailureReason(row["reason"]) if row["reason"] else None,
        runner_id=row["runner_id"],
        step=row["step"],
    )


class SqliteJobStore(JobStore):
    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], float] = time.time,
        retention_s: float = DEFAULT_RETENTION_S,
    ) -> None:
        self._clock = clock
        self._retention_s = retention_s
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        self.purge(retention_s)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create(self, job_id: str, repo: str, *, actor: str = "coordinator") -> Job:
        now = self._clock()
        with self._lock, self._db:
            try:
                self._db.execute(
                    "INSERT INTO jobs(id, repo, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (job_id, repo, State.QUEUED.value, now, now),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"could not create job, already exists: {job_id}") from e
            self._add_event(job_id, now, actor, State.QUEUED, "", None)
        return self._require(job_id)

    def transition(
        self,
        job_id: str,
        state: State,
        *,
        actor: str,
        detail: str = "",
        reason: FailureReason | None = None,
        runner_id: str | None = None,
        step: str | None = None,
    ) -> Job:
        if state == State.FAILED and reason is None:
            raise IllegalTransition(f"could not fail job without a reason: {job_id}")
        if state != State.FAILED and reason is not None:
            raise IllegalTransition(f"could not set a reason on non-failed state: {state.value}")
        now = self._clock()
        with self._lock, self._db:
            current = self._require(job_id)
            if state not in ALLOWED[current.state]:
                raise IllegalTransition(
                    f"illegal transition for job {job_id}: {current.state.value} -> {state.value}"
                )
            self._db.execute(
                "UPDATE jobs SET state=?, reason=?, runner_id=COALESCE(?, runner_id), "
                "step=COALESCE(?, step), updated_at=? WHERE id=?",
                (state.value, reason.value if reason else None, runner_id, step, now, job_id),
            )
            self._add_event(job_id, now, actor, state, detail, reason)
        return self._require(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list(
        self,
        *,
        state: State | None = None,
        failed: bool = False,
        running: bool = False,
        repo: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        where, args = [], []
        if state is not None:
            where.append("state=?")
            args.append(state.value)
        if failed:
            where.append("state=?")
            args.append(State.FAILED.value)
        if running:
            active = [s.value for s in State if s not in TERMINAL and s != State.QUEUED]
            where.append(f"state IN ({','.join('?' * len(active))})")
            args += active
        if repo is not None:
            where.append("repo=?")
            args.append(repo)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [_job(r) for r in rows]

    def timeline(self, job_id: str) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)
            ).fetchall()
        return [
            Event(
                job_id=r["job_id"],
                ts=r["ts"],
                actor=r["actor"],
                state=State(r["state"]),
                detail=r["detail"],
                reason=FailureReason(r["reason"]) if r["reason"] else None,
            )
            for r in rows
        ]

    def purge(self, older_than_s: float) -> int:
        cutoff = self._clock() - older_than_s
        terminal = [s.value for s in TERMINAL]
        marks = ",".join("?" * len(terminal))
        with self._lock, self._db:
            ids = [
                r["id"]
                for r in self._db.execute(
                    f"SELECT id FROM jobs WHERE state IN ({marks}) AND updated_at < ?",
                    (*terminal, cutoff),
                )
            ]
            for job_id in ids:
                self._db.execute("DELETE FROM events WHERE job_id=?", (job_id,))
                self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return len(ids)

    def _add_event(self, job_id, ts, actor, state, detail, reason) -> None:
        self._db.execute(
            "INSERT INTO events(job_id, ts, actor, state, detail, reason) VALUES (?,?,?,?,?,?)",
            (job_id, ts, actor, state.value, detail, reason.value if reason else None),
        )

    def _require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise UnknownJob(job_id)
        return job
