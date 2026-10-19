from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

# (content_topic, payload)
Handler = Callable[[str, bytes], None]


class Transport(ABC):
    """Moves opaque payloads between Octopus nodes, addressed by content topic."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None: ...

    @abstractmethod
    def unsubscribe(self, topic: str) -> None: ...

    @abstractmethod
    def publish(self, topic: str, payload: bytes) -> None: ...
