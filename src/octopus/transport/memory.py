from __future__ import annotations

from collections import defaultdict
from threading import Lock

from .base import ChannelHandler, ChannelTransport, Cipher, Handler, SendResult


class MemoryBus:
    """Shared in-process bus connecting several MemoryTransport instances."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[tuple[object, Handler]]] = defaultdict(list)
        self._channels: dict[str, list[_Member]] = defaultdict(list)
        # Test hooks: ciphertext seen on the wire, and a switch to lose every channel send.
        self.wire: list[bytes] = []
        self.drop_channel_sends = False

    def add(self, topic: str, owner: object, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append((owner, handler))

    def remove(self, topic: str, owner: object) -> None:
        with self._lock:
            self._subs[topic] = [s for s in self._subs[topic] if s[0] is not owner]

    def add_member(self, channel_id: str, member: _Member) -> None:
        with self._lock:
            self._channels[channel_id].append(member)

    def remove_member(self, channel_id: str, owner: object) -> None:
        with self._lock:
            self._channels[channel_id] = [
                m for m in self._channels[channel_id] if m.owner is not owner
            ]

    def deliver_channel(self, channel_id: str, sender: object, wire: bytes) -> bool:
        """Deliver to every other member. Members whose cipher rejects the data drop it."""
        with self._lock:
            self.wire.append(wire)
            if self.drop_channel_sends:
                return False
            targets = [m for m in self._channels.get(channel_id, ()) if m.owner is not sender]
        for m in targets:
            try:
                plain = m.cipher.decrypt(wire)
            except ValueError:  # wrong key or tampered: dropped, as the real channel does
                continue
            m.on_message(self._sender_id_of(channel_id, sender), plain)
        return True

    def _sender_id_of(self, channel_id: str, sender: object) -> str:
        with self._lock:
            for m in self._channels.get(channel_id, ()):
                if m.owner is sender:
                    return m.sender_id
        return ""

    def deliver(self, topic: str, payload: bytes) -> None:
        with self._lock:
            targets = list(self._subs.get(topic, ()))
        for _, handler in targets:
            handler(topic, payload)


class _Member:
    def __init__(
        self, owner: object, sender_id: str, cipher: Cipher, on_message: ChannelHandler
    ) -> None:
        self.owner = owner
        self.sender_id = sender_id
        self.cipher = cipher
        self.on_message = on_message


class MemoryTransport(ChannelTransport):
    """In-memory fake used by unit and integration tests."""

    def __init__(self, bus: MemoryBus) -> None:
        self._bus = bus
        self._topics: set[str] = set()
        self._channel_ids: set[str] = set()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        for topic in list(self._topics):
            self.unsubscribe(topic)
        for channel_id in list(self._channel_ids):
            self.close_channel(channel_id)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._topics.add(topic)
        self._bus.add(topic, self, handler)

    def unsubscribe(self, topic: str) -> None:
        self._topics.discard(topic)
        self._bus.remove(topic, self)

    def publish(self, topic: str, payload: bytes) -> None:
        self._bus.deliver(topic, payload)

    def open_channel(
        self,
        channel_id: str,
        topic: str,
        sender_id: str,
        cipher: Cipher,
        on_message: ChannelHandler,
    ) -> None:
        self._channel_ids.add(channel_id)
        self._bus.add_member(channel_id, _Member(self, sender_id, cipher, on_message))

    def send_on_channel(
        self, channel_id: str, payload: bytes, on_result: SendResult | None = None
    ) -> None:
        member = next((m for m in self._bus._channels.get(channel_id, ()) if m.owner is self), None)
        if member is None:
            raise RuntimeError(f"could not send on channel: not open: {channel_id}")
        delivered = self._bus.deliver_channel(channel_id, self, member.cipher.encrypt(payload))
        if on_result is not None:
            on_result(delivered, None if delivered else "dropped")

    def close_channel(self, channel_id: str) -> None:
        self._channel_ids.discard(channel_id)
        self._bus.remove_member(channel_id, self)
