"""Coordinator side of log streaming: receive chunks, store them, ack them, serve them.

`LogCollector` listens on a job's encrypted log channel from the moment the job is offered
to a runner. Every chunk goes into `LogStore` (a repeat is ignored) and is answered with a
`LogAck` carrying the highest seq up to which the stream has no gap, which is where the
runner resumes after a loss. A stream is keyed by (job, runner): a job retried on another
runner gets a fresh stream, and readers get the newest one unless they ask for a runner.

Memory stays bounded: chunks are written straight to SQLite and read back in batches.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import PairCipher, decode_public_key, derive_channel_key, node_id
from ..proto import MAX_LOG_CHUNK_BYTES, LogAck, LogChunk, Presence, log_channel_id, logs_topic
from ..transport import ChannelTransport

log = logging.getLogger(__name__)

DEFAULT_RETENTION_S = 30 * 24 * 3600
DEFAULT_MAX_STREAM_BYTES = 100 * 1024 * 1024
# A finished stream's channel stays open this long, so a runner whose final ack was lost
# gets it again when it resends.
DEFAULT_LINGER_S = 120.0
MAX_SEQ = 10_000_000
_BATCH = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_streams (
  job_id TEXT NOT NULL, runner_id TEXT NOT NULL, started_at REAL NOT NULL,
  updated_at REAL NOT NULL, contiguous INTEGER NOT NULL DEFAULT -1, final_seq INTEGER,
  bytes INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, runner_id));
CREATE TABLE IF NOT EXISTS log_chunks (
  job_id TEXT NOT NULL, runner_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (job_id, runner_id, seq)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS log_streams_updated ON log_streams(updated_at);
"""


@dataclass(frozen=True)
class LogState:
    job_id: str
    runner_id: str
    contiguous: int  # every chunk up to this seq is stored; -1: none
    final_seq: int | None
    bytes: int
    truncated: bool
    started_at: float
    updated_at: float

    @property
    def complete(self) -> bool:
        return self.final_seq is not None and self.contiguous >= self.final_seq


def _state(row: sqlite3.Row) -> LogState:
    return LogState(
        job_id=row["job_id"],
        runner_id=row["runner_id"],
        contiguous=row["contiguous"],
        final_seq=row["final_seq"],
        bytes=row["bytes"],
        truncated=bool(row["truncated"]),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
    )


class LogStore:
    """Log chunks per (job, runner) in SQLite, with a size cap per stream and retention."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], float] = time.time,
        retention_s: float = DEFAULT_RETENTION_S,
        max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES,
    ) -> None:
        self._clock = clock
        self._max_bytes = max_stream_bytes
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

    def put(self, runner_id: str, chunk: LogChunk) -> LogState:
        """Store a chunk; a seq already stored is ignored. Returns the stream's state.

        Past the size cap a chunk is stored empty (the first one says so), so the stream
        still completes and the runner stops resending.
        """
        if not 0 <= chunk.seq <= MAX_SEQ:
            raise ValueError(f"log chunk seq out of range: {chunk.seq}")
        now = self._clock()
        key = (chunk.job_id, runner_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO log_streams(job_id, runner_id, started_at, updated_at) "
                "VALUES (?,?,?,?)",
                (*key, now, now),
            )
            st = self._require(*key)
            if st.final_seq is not None and chunk.seq > st.final_seq:
                return st  # nothing comes after the final chunk
            exists = self._db.execute(
                "SELECT 1 FROM log_chunks WHERE job_id=? AND runner_id=? AND seq=?",
                (*key, chunk.seq),
            ).fetchone()
            if exists and not (chunk.final and st.final_seq is None):
                return st
            data, size, truncated = chunk.data, len(chunk.data.encode()), st.truncated
            if not exists and st.bytes + size > self._max_bytes:
                data = (
                    "" if truncated else f"[octopus] log truncated: over {self._max_bytes} bytes\n"
                )
                size, truncated = len(data.encode()), True
            if not exists:
                self._db.execute(
                    "INSERT INTO log_chunks(job_id, runner_id, seq, data) VALUES (?,?,?,?)",
                    (*key, chunk.seq, data),
                )
            contiguous = st.contiguous
            while self._db.execute(
                "SELECT 1 FROM log_chunks WHERE job_id=? AND runner_id=? AND seq=?",
                (*key, contiguous + 1),
            ).fetchone():
                contiguous += 1
            final_seq = chunk.seq if chunk.final and st.final_seq is None else st.final_seq
            self._db.execute(
                "UPDATE log_streams SET contiguous=?, final_seq=?, bytes=bytes+?, truncated=?, "
                "updated_at=? WHERE job_id=? AND runner_id=?",
                (contiguous, final_seq, 0 if exists else size, int(truncated), now, *key),
            )
            return self._require(*key)

    def state(self, job_id: str, runner_id: str | None = None) -> LogState | None:
        """The stream of `runner_id`, or the job's newest stream."""
        with self._lock:
            if runner_id is not None:
                row = self._db.execute(
                    "SELECT * FROM log_streams WHERE job_id=? AND runner_id=?", (job_id, runner_id)
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM log_streams WHERE job_id=? ORDER BY started_at DESC, rowid DESC "
                    "LIMIT 1",
                    (job_id,),
                ).fetchone()
        return _state(row) if row else None

    def streams(self, job_id: str) -> list[LogState]:
        """Every attempt's stream for the job, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM log_streams WHERE job_id=? ORDER BY started_at, rowid", (job_id,)
            ).fetchall()
        return [_state(r) for r in rows]

    def gaps(self, job_id: str, runner_id: str | None = None) -> list[tuple[int, int]]:
        """Missing seq ranges (inclusive) below the highest stored seq, or the final seq."""
        st = self.state(job_id, runner_id)
        if st is None:
            return []
        with self._lock:
            seqs = [
                r[0]
                for r in self._db.execute(
                    "SELECT seq FROM log_chunks WHERE job_id=? AND runner_id=? AND seq>? "
                    "ORDER BY seq",
                    (job_id, st.runner_id, st.contiguous),
                )
            ]
        gaps, expected = [], st.contiguous + 1
        for seq in seqs:
            if seq > expected:
                gaps.append((expected, seq - 1))
            expected = seq + 1
        if st.final_seq is not None and expected <= st.final_seq:
            gaps.append((expected, st.final_seq))
        return gaps

    def read(
        self, job_id: str, start: int = 0, end: int | None = None, *, runner_id: str | None = None
    ) -> Iterator[LogChunk]:
        """Stored chunks with `start <= seq <= end`, in order, fetched in small batches."""
        st = self.state(job_id, runner_id)
        if st is None:
            return
        last = MAX_SEQ if end is None else end
        seq = start
        while seq <= last:
            with self._lock:
                rows = self._db.execute(
                    "SELECT seq, data FROM log_chunks WHERE job_id=? AND runner_id=? "
                    "AND seq>=? AND seq<=? ORDER BY seq LIMIT ?",
                    (job_id, st.runner_id, seq, last, _BATCH),
                ).fetchall()
            if not rows:
                return
            for r in rows:
                yield LogChunk(
                    job_id=job_id, seq=r["seq"], data=r["data"], final=r["seq"] == st.final_seq
                )
            seq = rows[-1]["seq"] + 1

    def tail(self, job_id: str, lines: int = 20, *, runner_id: str | None = None) -> list[str]:
        """The last `lines` lines of the job's log (for `octopus explain`)."""
        st = self.state(job_id, runner_id)
        if st is None or lines <= 0:
            return []
        parts: list[str] = []
        newlines, before = 0, MAX_SEQ + 1
        while newlines <= lines:
            with self._lock:
                rows = self._db.execute(
                    "SELECT seq, data FROM log_chunks WHERE job_id=? AND runner_id=? AND seq<? "
                    "ORDER BY seq DESC LIMIT ?",
                    (job_id, st.runner_id, before, _BATCH),
                ).fetchall()
            if not rows:
                break
            for r in rows:
                parts.append(r["data"])
                newlines += r["data"].count("\n")
            before = rows[-1]["seq"]
        text = "".join(reversed(parts))
        return text.splitlines()[-lines:]

    def purge(self, older_than_s: float) -> int:
        """Delete streams not updated for `older_than_s`. Returns how many."""
        cutoff = self._clock() - older_than_s
        with self._lock, self._db:
            keys = self._db.execute(
                "SELECT job_id, runner_id FROM log_streams WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            for job_id, runner_id in keys:
                self._delete(job_id, runner_id)
        return len(keys)

    def delete(self, job_id: str) -> None:
        with self._lock, self._db:
            for st in self.streams(job_id):
                self._delete(job_id, st.runner_id)

    def _delete(self, job_id: str, runner_id: str) -> None:
        self._db.execute(
            "DELETE FROM log_chunks WHERE job_id=? AND runner_id=?", (job_id, runner_id)
        )
        self._db.execute(
            "DELETE FROM log_streams WHERE job_id=? AND runner_id=?", (job_id, runner_id)
        )

    def _require(self, job_id: str, runner_id: str) -> LogState:
        st = self.state(job_id, runner_id)
        assert st is not None
        return st


@dataclass
class _Watch:
    job_id: str
    runner_id: str
    channel_id: str
    completed_at: float | None = None


class LogCollector:
    """Receives job logs from runners over their log channels, stores and acks them.

    `watch(job_id, runner)` before the offer goes out (the scheduler does), `unwatch` when
    the runner refused or was lost. A completed stream's channel closes on its own after
    `linger_s` (`tick()`, or the background thread from `start()`).
    """

    def __init__(
        self,
        transport: ChannelTransport,
        private_key: X25519PrivateKey,
        store: LogStore,
        *,
        clock: Clock | None = None,
        linger_s: float = DEFAULT_LINGER_S,
    ) -> None:
        self._transport = transport
        self._key = private_key
        self._store = store
        self._clock = clock or SystemClock()
        self._linger_s = linger_s
        self.coordinator_id = node_id(private_key.public_key())
        self._lock = threading.Lock()
        self._watches: dict[tuple[str, str], _Watch] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, tick_s: float = 5.0) -> None:
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(tick_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("log collector tick failed")

        self._thread = threading.Thread(target=loop, name="octopus-log-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            keys = list(self._watches)
        for job_id, runner_id in keys:
            self.unwatch(job_id, runner_id)

    def watch(self, job_id: str, runner: Presence) -> None:
        """Open the job's log channel with `runner`. Raises ValueError without a valid key."""
        if not runner.pubkey:
            raise ValueError(f"could not watch logs: runner has no key: {runner.runner_id}")
        peer = decode_public_key(runner.pubkey)
        if node_id(peer) != runner.runner_id:
            raise ValueError(f"could not watch logs: id does not match key: {runner.runner_id}")
        key = (job_id, runner.runner_id)
        channel_id = log_channel_id(self.coordinator_id, runner.runner_id, job_id)
        with self._lock:
            if key in self._watches:
                return
            self._watches[key] = _Watch(job_id, runner.runner_id, channel_id)
        cipher = PairCipher(derive_channel_key(self._key, peer, channel_id))
        try:
            self._transport.open_channel(
                channel_id,
                logs_topic(job_id),
                self.coordinator_id,
                cipher,
                lambda _sender, payload, k=key: self._on_message(k, payload),
            )
        except Exception:
            with self._lock:
                self._watches.pop(key, None)
            raise
        state = self._store.state(job_id, runner.runner_id)
        if state is not None:  # after a coordinator restart: tell the runner where to resume
            self._ack(key, state)

    def unwatch(self, job_id: str, runner_id: str) -> None:
        with self._lock:
            watch = self._watches.pop((job_id, runner_id), None)
        if watch is not None:
            try:
                self._transport.close_channel(watch.channel_id)
            except RuntimeError:
                pass

    def watching(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._watches)

    def tick(self) -> None:
        """Close the channels of streams that completed more than `linger_s` ago."""
        now = self._clock.monotonic()
        with self._lock:
            done = [
                k
                for k, w in self._watches.items()
                if w.completed_at is not None and now - w.completed_at >= self._linger_s
            ]
        for job_id, runner_id in done:
            self.unwatch(job_id, runner_id)

    def _on_message(self, key: tuple[str, str], payload: bytes) -> None:
        job_id, runner_id = key
        try:
            chunk = LogChunk.model_validate_json(payload)
        except ValidationError:
            return
        if chunk.job_id != job_id:
            log.warning("dropped log chunk for another job on %s's channel", job_id)
            return
        if len(chunk.data.encode()) > MAX_LOG_CHUNK_BYTES:
            log.warning("dropped oversized log chunk %d of job %s", chunk.seq, job_id)
            return
        try:
            state = self._store.put(runner_id, chunk)
        except ValueError as e:
            log.warning("dropped log chunk of job %s: %s", job_id, e)
            return
        if state.complete:
            with self._lock:
                watch = self._watches.get(key)
                if watch is not None and watch.completed_at is None:
                    watch.completed_at = self._clock.monotonic()
        self._ack(key, state)

    def _ack(self, key: tuple[str, str], state: LogState) -> None:
        with self._lock:
            watch = self._watches.get(key)
        if watch is None:
            return
        ack = LogAck(job_id=state.job_id, seq=state.contiguous, complete=state.complete)
        try:
            self._transport.send_on_channel(watch.channel_id, ack.model_dump_json().encode())
        except RuntimeError as e:
            log.debug("could not ack log of job %s: %s", state.job_id, e)
