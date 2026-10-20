from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from ..clock import Clock, SystemClock
from ..crypto import KeyMaterialError, decode_public_key, node_id
from ..proto import PRESENCE_TOPIC, Presence
from ..transport import Transport

DEFAULT_INTERVAL_S = 10.0
DEFAULT_MISSED = 3


@dataclass(frozen=True)
class Entry:
    presence: Presence
    last_seen: float  # local monotonic receive time; sent_at is never trusted


class RegistryStore(Protocol):
    """Where entries live. In-memory by default; a persistent store can replace it."""

    def put(self, entry: Entry) -> None: ...

    def remove(self, runner_id: str) -> None: ...

    def get(self, runner_id: str) -> Entry | None: ...

    def all(self) -> list[Entry]: ...


class MemoryStore:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def put(self, entry: Entry) -> None:
        self._entries[entry.presence.runner_id] = entry

    def remove(self, runner_id: str) -> None:
        self._entries.pop(runner_id, None)

    def get(self, runner_id: str) -> Entry | None:
        return self._entries.get(runner_id)

    def all(self) -> list[Entry]:
        return list(self._entries.values())


class Registry:
    """Tracks which runners are online and idle, from their presence announcements."""

    def __init__(
        self,
        transport: Transport,
        clock: Clock | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        missed: int = DEFAULT_MISSED,
        store: RegistryStore | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock or SystemClock()
        self._timeout_s = interval_s * missed
        self._store = store or MemoryStore()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._transport.subscribe(PRESENCE_TOPIC, self._on_presence)

    def stop(self) -> None:
        self._transport.unsubscribe(PRESENCE_TOPIC)

    def online(self) -> list[Presence]:
        now = self._clock.monotonic()
        with self._lock:
            return [e.presence for e in self._store.all() if self._is_online(e, now)]

    def get(self, runner_id: str) -> Presence | None:
        now = self._clock.monotonic()
        with self._lock:
            entry = self._store.get(runner_id)
        return entry.presence if entry and self._is_online(entry, now) else None

    def idle_runners(
        self,
        labels: Iterable[str] = (),
        os: str | None = None,
        arch: str | None = None,
    ) -> list[Presence]:
        wanted = set(labels)
        return [
            p
            for p in self.online()
            if p.busy < p.capacity
            and wanted <= set(p.labels)
            and (os is None or p.os == os)
            and (arch is None or p.arch == arch)
        ]

    def _is_online(self, entry: Entry, now: float) -> bool:
        return now - entry.last_seen <= self._timeout_s

    def _on_presence(self, _topic: str, payload: bytes) -> None:
        try:
            presence = Presence.model_validate_json(payload)
        except ValidationError:
            return
        if presence.pubkey is not None and not _id_matches_key(presence):
            return
        with self._lock:
            if presence.offline:
                self._store.remove(presence.runner_id)
            else:
                self._store.put(Entry(presence, self._clock.monotonic()))


def _id_matches_key(presence: Presence) -> bool:
    """A runner id is derived from its key, so a forged presence cannot claim another's id."""
    try:
        return node_id(decode_public_key(presence.pubkey or "")) == presence.runner_id
    except KeyMaterialError:
        return False
