from __future__ import annotations

from collections import defaultdict
from threading import Lock

from .base import Handler, Transport


class MemoryBus:
    """Shared in-process bus connecting several MemoryTransport instances."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[tuple[object, Handler]]] = defaultdict(list)

    def add(self, topic: str, owner: object, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append((owner, handler))

    def remove(self, topic: str, owner: object) -> None:
        with self._lock:
            self._subs[topic] = [s for s in self._subs[topic] if s[0] is not owner]

    def deliver(self, topic: str, payload: bytes) -> None:
        with self._lock:
            targets = list(self._subs.get(topic, ()))
        for _, handler in targets:
            handler(topic, payload)


class MemoryTransport(Transport):
    """In-memory fake used by unit and integration tests."""

    def __init__(self, bus: MemoryBus) -> None:
        self._bus = bus
        self._topics: set[str] = set()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        for topic in list(self._topics):
            self.unsubscribe(topic)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._topics.add(topic)
        self._bus.add(topic, self, handler)

    def unsubscribe(self, topic: str) -> None:
        self._topics.discard(topic)
        self._bus.remove(topic, self)

    def publish(self, topic: str, payload: bytes) -> None:
        self._bus.deliver(topic, payload)
