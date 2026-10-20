"""Runner side of log streaming: mask, chunk, spool to disk, send with acks and resume.

`LogStreamer.write(line)` takes the runner's output (stdout and stderr, merged by
`run_runner`). Lines are masked, packed into `LogChunk`s of at most `MAX_LOG_CHUNK_BYTES`
and appended to a spool file, so memory stays bounded however long the log is and however
long the coordinator is unreachable. Chunks go out on the job's encrypted log channel, at
most `window` unacknowledged at a time. The coordinator acks the highest seq up to which it
has everything; when no ack advances for `resend_after_s`, sending restarts from there
(go-back-N). A dropped connection, a lost message and a coordinator restart all end the
same way: a resend from the last acked seq.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from array import array
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import PairCipher, decode_public_key, derive_channel_key, node_id
from ..proto import MAX_LOG_CHUNK_BYTES, LogAck, LogChunk, log_channel_id, logs_topic
from ..transport import ChannelTransport

log = logging.getLogger(__name__)

REDACTED = "***"
ADD_MASK = "::add-mask::"
# Shorter values would mangle the log more than they protect.
MIN_SECRET_LEN = 3
DEFAULT_WINDOW = 16
DEFAULT_RESEND_AFTER_S = 10.0
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_MAX_SPOOL_BYTES = 256 * 1024 * 1024
DEFAULT_DRAIN_S = 300.0
TICK_S = 0.2


class SecretMasker:
    """Replaces secrets with `***`: the values it is given, and any a workflow registers with
    `::add-mask::<value>` (that line is masked too). Later lines only, as on GitHub."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: list[str] = []
        for s in secrets:
            self.add(s)

    def add(self, value: str) -> None:
        value = value.strip()
        if len(value) < MIN_SECRET_LEN:
            return
        with self._lock:
            if value not in self._secrets:
                # Longest first, so a secret containing another is replaced whole.
                self._secrets = sorted([*self._secrets, value], key=len, reverse=True)

    def mask(self, line: str) -> str:
        at = line.find(ADD_MASK)
        if at != -1:
            self.add(line[at + len(ADD_MASK) :])
            line = line[: at + len(ADD_MASK)] + REDACTED
        with self._lock:
            secrets = self._secrets
        for s in secrets:
            if s in line:
                line = line.replace(s, REDACTED)
        return line


@dataclass(frozen=True)
class RunnerMarker:
    """A job boundary printed by `actions/runner` on its console."""

    kind: Literal["job-started", "job-completed"]
    name: str
    result: str | None = None


# Runner.Listener prints "<utc time>: Running job: <name>" and
# "<utc time>: Job <name> completed with result: <result>".
_STARTED = re.compile(r": Running job: (?P<name>.+)$")
_COMPLETED = re.compile(r": Job (?P<name>.+) completed with result: (?P<result>\w+)$")


def runner_marker(line: str) -> RunnerMarker | None:
    if m := _COMPLETED.search(line):
        return RunnerMarker("job-completed", m["name"], m["result"])
    if m := _STARTED.search(line):
        return RunnerMarker("job-started", m["name"])
    return None


class LogSpool:
    """Append-only chunk store in one file (mode 0600). Memory holds only the offsets."""

    def __init__(self, directory: Path, prefix: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".spool", dir=directory)
        self.path = Path(name)
        self._f = os.fdopen(fd, "w+b")
        self._lock = threading.Lock()
        self._ends = array("q", [0])  # chunk i is bytes [ends[i], ends[i + 1])

    def append(self, text: str) -> int:
        data = text.encode()
        with self._lock:
            self._f.seek(self._ends[-1])
            self._f.write(data)
            self._f.flush()
            self._ends.append(self._ends[-1] + len(data))
            return len(self._ends) - 2

    def read(self, seq: int) -> str:
        with self._lock:
            start, end = self._ends[seq], self._ends[seq + 1]
            self._f.seek(start)
            data = self._f.read(end - start)
        return data.decode()

    def __len__(self) -> int:
        return len(self._ends) - 1

    @property
    def size(self) -> int:
        return self._ends[-1]

    def close(self) -> None:
        with self._lock:
            self._f.close()
        self.path.unlink(missing_ok=True)


def _split_utf8(text: str, limit: int) -> list[str]:
    """`text` in pieces of at most `limit` UTF-8 bytes, never splitting a character."""
    data = text.encode()
    pieces = []
    while len(data) > limit:
        cut = limit
        while cut > 0 and (data[cut] & 0xC0) == 0x80:  # continuation byte
            cut -= 1
        pieces.append(data[:cut].decode())
        data = data[cut:]
    pieces.append(data.decode())
    return pieces


class LogStreamer:
    """Streams one job's log to the coordinator that offered it. Thread-safe.

    Call `start()`, feed `write(line)` (e.g. as `RunnerManager.run_job`'s `on_output`), then
    `close()` when the job is over: it sends what is left and waits for the coordinator to
    have all of it.
    """

    def __init__(
        self,
        transport: ChannelTransport,
        private_key: X25519PrivateKey,
        coordinator_id: str,
        coordinator_pubkey: str,
        job_id: str,
        spool_dir: Path,
        *,
        secrets: Iterable[str] = (),
        clock: Clock | None = None,
        window: int = DEFAULT_WINDOW,
        resend_after_s: float = DEFAULT_RESEND_AFTER_S,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_chunk_bytes: int = MAX_LOG_CHUNK_BYTES,
        max_spool_bytes: int = DEFAULT_MAX_SPOOL_BYTES,
        on_marker: Callable[[RunnerMarker], None] | None = None,
    ) -> None:
        peer = decode_public_key(coordinator_pubkey)
        if node_id(peer) != coordinator_id:
            raise ValueError(f"could not stream logs: id does not match key: {coordinator_id}")
        self.job_id = job_id
        self.runner_id = node_id(private_key.public_key())
        self.channel_id = log_channel_id(coordinator_id, self.runner_id, job_id)
        self._cipher = PairCipher(derive_channel_key(private_key, peer, self.channel_id))
        self._transport = transport
        self._clock = clock or SystemClock()
        self._window = window
        self._resend_after_s = resend_after_s
        self._flush_interval_s = flush_interval_s
        self._max_chunk = min(max_chunk_bytes, MAX_LOG_CHUNK_BYTES)
        self._max_spool = max_spool_bytes
        self._on_marker = on_marker
        self._masker = SecretMasker(secrets)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in job_id)[:40]
        self._spool = LogSpool(spool_dir, f"log-{safe}-")

        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._pending_bytes = 0
        self._pending_since = 0.0
        self._truncated = False
        self._final_seq: int | None = None
        self._acked = -1  # the coordinator has every chunk up to here
        self._next = 0  # next seq to send
        self._last_progress = self._clock.monotonic()
        self._done = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._open = False

    # lifecycle

    def start(self, *, background: bool = True) -> None:
        """Open the log channel; with `background`, a thread sends and resends on its own."""
        self._transport.open_channel(
            self.channel_id,
            logs_topic(self.job_id),
            self.runner_id,
            self._cipher,
            lambda _sender, payload: self._on_message(payload),
        )
        self._open = True
        if background:
            self._thread = threading.Thread(
                target=self._loop, name=f"octopus-logs-{self.job_id[:8]}", daemon=True
            )
            self._thread.start()

    def finish(self) -> None:
        """No more output: seal what is pending as the final chunk."""
        with self._lock:
            if self._final_seq is not None:
                return
            if self._pending:
                self._seal()
            else:
                self._spool.append("")  # an empty final chunk: the log ends here
            self._final_seq = len(self._spool) - 1
        self._wake.set()

    def close(self, timeout_s: float = DEFAULT_DRAIN_S) -> bool:
        """Finish, wait up to `timeout_s` for the coordinator to have everything, clean up.

        Returns whether the whole log was acknowledged.
        """
        self.finish()
        if self._thread is not None:
            self._done.wait(timeout_s)
        complete = self._done.is_set()
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._open:
            try:
                self._transport.close_channel(self.channel_id)
            except RuntimeError:
                pass
            self._open = False
        self._spool.close()
        if not complete:
            log.warning("log of job %s not fully delivered: acked %d", self.job_id, self._acked)
        return complete

    @property
    def complete(self) -> bool:
        return self._done.is_set()

    @property
    def acked(self) -> int:
        return self._acked

    # input

    def write(self, line: str) -> None:
        """One line of output, without its line ending."""
        masked = self._masker.mask(line)
        if self._on_marker is not None and (marker := runner_marker(masked)) is not None:
            try:
                self._on_marker(marker)
            except Exception:  # a failing consumer must not lose the log
                log.exception("marker handler failed")
        with self._lock:
            if self._final_seq is not None or self._truncated:
                return
            for piece in _split_utf8(masked + "\n", self._max_chunk):
                self._add(piece)

    def _add(self, piece: str) -> None:
        size = len(piece.encode())
        if self._spool.size + self._pending_bytes + size > self._max_spool:
            self._truncated = True
            if self._pending:
                self._seal()
            self._spool.append(f"[octopus] log truncated: over {self._max_spool} bytes\n")
            return
        if self._pending and self._pending_bytes + size > self._max_chunk:
            self._seal()
        if not self._pending:
            self._pending_since = self._clock.monotonic()
        self._pending.append(piece)
        self._pending_bytes += size

    def _seal(self) -> None:
        self._spool.append("".join(self._pending))
        self._pending.clear()
        self._pending_bytes = 0

    # sending

    def tick(self) -> None:
        """Flush stale output and send what the window allows. Called by the thread."""
        now = self._clock.monotonic()
        with self._lock:
            if self._pending and now - self._pending_since >= self._flush_interval_s:
                self._seal()
            in_flight = self._next > self._acked + 1
            if in_flight and now - self._last_progress >= self._resend_after_s:
                self._next = self._acked + 1  # nothing acked for a while: go back
                in_flight = False
            limit = min(len(self._spool), self._acked + 1 + self._window)
            batch = list(range(self._next, limit))
            if batch and not in_flight:
                self._last_progress = now
            self._next = max(self._next, limit)
            final = self._final_seq
        for seq in batch:
            chunk = LogChunk(
                job_id=self.job_id, seq=seq, data=self._spool.read(seq), final=seq == final
            )
            try:
                self._transport.send_on_channel(self.channel_id, chunk.model_dump_json().encode())
            except RuntimeError as e:  # transport down: the resend timer retries
                log.debug("could not send log chunk %d of %s: %s", seq, self.job_id, e)
                break

    def _on_message(self, payload: bytes) -> None:
        try:
            ack = LogAck.model_validate_json(payload)
        except ValidationError:
            return
        if ack.job_id != self.job_id:
            return
        with self._lock:
            seq = min(ack.seq, len(self._spool) - 1)  # never past what was sent
            if seq > self._acked:
                self._acked = seq
                self._last_progress = self._clock.monotonic()
            self._next = max(self._next, self._acked + 1)
            done = ack.complete and self._final_seq is not None and self._acked >= self._final_seq
        if done:
            self._done.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("log streaming failed: job %s", self.job_id)
            self._wake.wait(TICK_S)
            self._wake.clear()
