from octopus.transport import MemoryBus, MemoryTransport


def test_pubsub_between_nodes():
    bus = MemoryBus()
    a, b = MemoryTransport(bus), MemoryTransport(bus)
    got = []
    b.subscribe("/t", lambda t, p: got.append((t, p)))
    a.publish("/t", b"x")
    assert got == [("/t", b"x")]
    b.stop()
    a.publish("/t", b"y")
    assert len(got) == 1
