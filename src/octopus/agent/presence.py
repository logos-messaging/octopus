from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..proto import PRESENCE_TOPIC, Presence
from ..transport import Transport

DEFAULT_INTERVAL_S = 10.0


@dataclass(frozen=True)
class RunnerInfo:
    runner_id: str
    os: str
    arch: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    capacity: int = 1


class PresencePublisher:
    """Announces this runner on PRESENCE_TOPIC: at start, periodically and on state change."""

    def __init__(
        self, transport: Transport, info: RunnerInfo, interval_s: float = DEFAULT_INTERVAL_S
    ) -> None:
        self._transport = transport
        self._info = info
        self._interval_s = interval_s
        self._busy = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.tick()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="octopus-presence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._publish(offline=True)

    def set_busy(self, busy: int) -> None:
        with self._lock:
            if busy == self._busy:
                return
            self._busy = busy
        self.tick()

    def tick(self) -> None:
        self._publish(offline=False)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.tick()

    def _publish(self, *, offline: bool) -> None:
        with self._lock:
            busy = self._busy
        msg = Presence(
            runner_id=self._info.runner_id,
            os=self._info.os,
            arch=self._info.arch,
            labels=list(self._info.labels),
            capacity=self._info.capacity,
            busy=busy,
            sent_at=time.time(),
            offline=offline,
        )
        self._transport.publish(PRESENCE_TOPIC, msg.model_dump_json().encode())
