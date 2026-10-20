from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol

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


class Cipher(Protocol):
    """Bytes in, bytes out. Runs inline on the transport's event loop: fast, no I/O."""

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


# (sender_id, payload)
ChannelHandler = Callable[[str, bytes], None]
# (delivered, reason): delivered=False when the send finalised with a failure.
SendResult = Callable[[bool, str | None], None]


class ChannelTransport(Transport):
    """A Transport that also offers reliable, encrypted point-to-point channels.

    The cipher encrypts everything on the wire; a message that fails to decrypt is
    dropped and never reaches the handler.
    """

    @abstractmethod
    def open_channel(
        self,
        channel_id: str,
        topic: str,
        sender_id: str,
        cipher: Cipher,
        on_message: ChannelHandler,
    ) -> None: ...

    @abstractmethod
    def send_on_channel(
        self, channel_id: str, payload: bytes, on_result: SendResult | None = None
    ) -> None: ...

    @abstractmethod
    def close_channel(self, channel_id: str) -> None: ...
