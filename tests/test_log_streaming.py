"""Runner LogStreamer -> coordinator LogCollector over MemoryTransport, with faults."""

import random
import tracemalloc
from collections import deque

import pytest

from octopus.agent.logs import LogStreamer, RunnerMarker, SecretMasker, _split_utf8, runner_marker
from octopus.coordinator.logs import LogCollector, LogStore
from octopus.crypto import (
    PairCipher,
    derive_channel_key,
    encode_public_key,
    generate_private_key,
    node_id,
)
from octopus.proto import MAX_LOG_CHUNK_BYTES, LogAck, LogChunk, Presence, log_channel_id
from octopus.transport import MemoryBus, MemoryTransport

JIT = "eyJmYWtlIjoiaml0LWNvbmZpZyJ9"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def monotonic(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FlakyTransport(MemoryTransport):
    """Drops, duplicates and delays (so reorders) outgoing channel sends, seeded."""

    def __init__(self, bus, *, seed=1, drop=0.0, dup=0.0, delay=0.0):
        super().__init__(bus)
        self.rng = random.Random(seed)
        self.drop, self.dup, self.delay = drop, dup, delay
        self.held: list[tuple[str, bytes]] = []
        self.down = False

    def send_on_channel(self, channel_id, payload, on_result=None):
        if self.down:
            raise RuntimeError("transport down")
        r = self.rng.random()
        if r < self.drop:
            return
        if r < self.drop + self.delay:
            self.held.append((channel_id, payload))
            return
        super().send_on_channel(channel_id, payload, on_result)
        if self.rng.random() < self.dup:
            super().send_on_channel(channel_id, payload, on_result)

    def release_held(self):
        held, self.held = self.held, []
        self.rng.shuffle(held)
        for channel_id, payload in held:
            super().send_on_channel(channel_id, payload)


class World:
    def __init__(self, tmp_path, *, runner_transport=None, coord_transport=None, store=None, **kw):
        self.bus = MemoryBus()
        self.clock = FakeClock()
        self.coord_key = generate_private_key()
        self.runner_key = generate_private_key()
        self.coordinator_id = node_id(self.coord_key.public_key())
        self.runner = Presence(
            runner_id=node_id(self.runner_key.public_key()),
            os="linux",
            arch="x64",
            sent_at=0,
            pubkey=encode_public_key(self.runner_key.public_key()),
        )
        self.store = store or LogStore(":memory:", clock=self.clock.now)
        self.coord_transport = coord_transport or MemoryTransport(self.bus)
        self.collector = LogCollector(
            self.coord_transport, self.coord_key, self.store, clock=self.clock
        )
        self.collector.watch("job-1", self.runner)
        # `runner_transport`: a factory taking the bus, e.g. a FlakyTransport.
        self.runner_transport = (runner_transport or MemoryTransport)(self.bus)
        kw.setdefault("secrets", [JIT])
        self.streamer = LogStreamer(
            self.runner_transport,
            self.runner_key,
            self.coordinator_id,
            encode_public_key(self.coord_key.public_key()),
            "job-1",
            tmp_path / "spool",
            clock=self.clock,
            **kw,
        )
        self.streamer.start(background=False)

    def pump(self, rounds=50, step_s=1.0):
        for _ in range(rounds):
            if self.streamer.complete:
                return
            self.streamer.tick()
            self.clock.advance(step_s)

    def text(self):
        return "".join(c.data for c in self.store.read("job-1"))


def lines(n, width=80):
    return [f"{i:06d} " + "x" * (width - 7) for i in range(n)]


def test_round_trip(tmp_path):
    w = World(tmp_path)
    for line in ["step 1", "step 2", "done"]:
        w.streamer.write(line)
    w.streamer.finish()
    w.pump()
    assert w.streamer.complete
    assert w.text() == "step 1\nstep 2\ndone\n"
    st = w.store.state("job-1")
    assert st.complete and st.runner_id == w.runner.runner_id
    assert w.streamer.close(timeout_s=0)


def test_chunks_are_bounded_and_ordered(tmp_path):
    w = World(tmp_path)
    expected = lines(2000)
    for line in expected:
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=200)
    chunks = list(w.store.read("job-1"))
    assert len(chunks) > 5
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert all(len(c.data.encode()) <= MAX_LOG_CHUNK_BYTES for c in chunks)
    assert w.text().splitlines() == expected


def test_stale_output_is_flushed_without_finish(tmp_path):
    w = World(tmp_path, flush_interval_s=1.0)
    w.streamer.write("still running")
    w.streamer.tick()
    assert w.text() == ""  # not yet a second old
    w.clock.advance(1.0)
    w.streamer.tick()
    assert w.text() == "still running\n"
    assert not w.store.state("job-1").complete


def test_secrets_are_masked_and_the_wire_is_encrypted(tmp_path):
    w = World(tmp_path)
    w.streamer.write(f"config: {JIT}")
    w.streamer.write("::add-mask::hunter22")
    w.streamer.write("password is hunter22")
    w.streamer.finish()
    w.pump()
    text = w.text()
    assert JIT not in text and "hunter22" not in text
    assert text == "config: ***\n::add-mask::***\npassword is ***\n"
    assert w.bus.wire and all(b"password is" not in frame for frame in w.bus.wire)


def test_another_node_cannot_read_the_log(tmp_path):
    w = World(tmp_path)
    eve_key = generate_private_key()
    channel = log_channel_id(w.coordinator_id, w.runner.runner_id, "job-1")
    stolen = []
    eve = MemoryTransport(w.bus)
    eve.open_channel(
        channel,
        "t",
        "eve",
        PairCipher(derive_channel_key(eve_key, w.runner_key.public_key(), channel)),
        lambda s, p: stolen.append(p),
    )
    w.streamer.write("private build output")
    w.streamer.finish()
    w.pump()
    assert stolen == [] and w.streamer.complete


def test_resumes_after_a_dropped_connection(tmp_path):
    w = World(tmp_path, window=4, resend_after_s=5)
    expected = lines(1500)
    for line in expected[:700]:
        w.streamer.write(line)
    w.pump(rounds=3)
    acked_before = w.streamer.acked
    assert acked_before >= 0
    w.bus.drop_channel_sends = True  # connection gone: chunks and acks lost
    for line in expected[700:]:
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=10)
    assert not w.streamer.complete and w.streamer.acked == acked_before
    w.bus.drop_channel_sends = False
    w.pump(rounds=200)
    assert w.streamer.complete
    assert w.text().splitlines() == expected
    assert w.store.gaps("job-1") == []


def test_transport_errors_while_sending_are_retried(tmp_path):
    w = World(tmp_path, runner_transport=FlakyTransport, window=2, resend_after_s=3)
    w.runner_transport.down = True
    for line in lines(400):
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=5)
    assert not w.streamer.complete and w.streamer.acked == -1
    w.runner_transport.down = False
    w.pump(rounds=100)
    assert w.streamer.complete and w.text().splitlines() == lines(400)


def test_lost_acks_cause_resends_that_are_deduplicated(tmp_path):
    bus_seed = random.Random(7)
    w = World(tmp_path, window=4, resend_after_s=3)
    real_send = w.coord_transport.send_on_channel

    def lossy_ack(channel_id, payload, on_result=None):
        if bus_seed.random() < 0.6:
            return
        real_send(channel_id, payload, on_result)

    w.coord_transport.send_on_channel = lossy_ack
    for line in lines(1200):
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=500)
    assert w.streamer.complete
    assert w.text().splitlines() == lines(1200)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_loss_duplication_and_reordering(tmp_path, seed):
    def flaky(bus):
        return FlakyTransport(bus, seed=seed, drop=0.2, dup=0.2, delay=0.2)

    w = World(tmp_path, runner_transport=flaky, window=8, resend_after_s=3)
    expected = lines(3000)
    for line in expected:
        w.streamer.write(line)
    w.streamer.finish()
    for _ in range(400):
        if w.streamer.complete:
            break
        w.streamer.tick()
        if w.runner_transport.held and w.runner_transport.rng.random() < 0.5:
            w.runner_transport.release_held()  # late, shuffled deliveries
        w.clock.advance(1.0)
    assert w.streamer.complete
    assert w.text().splitlines() == expected
    assert w.store.gaps("job-1") == []


def test_window_limits_unacked_chunks(tmp_path):
    w = World(tmp_path, window=3, resend_after_s=100)
    w.collector.unwatch("job-1", w.runner.runner_id)  # nobody acks
    sent = []
    real = w.runner_transport.send_on_channel
    w.runner_transport.send_on_channel = lambda c, p, r=None: (sent.append(p), real(c, p, r))
    for line in lines(3000):
        w.streamer.write(line)
    w.streamer.finish()
    for _ in range(10):
        w.streamer.tick()
        w.clock.advance(1)
    assert len(sent) == 3


def test_coordinator_restart_resumes_from_what_was_stored(tmp_path):
    db = str(tmp_path / "logs.db")
    w = World(tmp_path, store=LogStore(db), window=4, resend_after_s=5)
    expected = lines(1500)
    for line in expected[:800]:
        w.streamer.write(line)
    w.pump(rounds=3)
    stored = w.store.state("job-1").contiguous
    assert stored >= 0
    # The coordinator dies: its channel and store go away mid-stream.
    w.collector.stop()
    w.store.close()
    for line in expected[800:]:
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=5)
    assert not w.streamer.complete
    # It comes back with the same database and watches the job again.
    store = LogStore(db)
    collector = LogCollector(MemoryTransport(w.bus), w.coord_key, store, clock=w.clock)
    collector.watch("job-1", w.runner)
    assert w.streamer.acked >= stored  # the resume ack on watch
    w.pump(rounds=200)
    assert w.streamer.complete
    assert "".join(c.data for c in store.read("job-1")).splitlines() == expected


def test_spool_cap_truncates_but_completes(tmp_path):
    w = World(tmp_path, max_spool_bytes=50_000)
    for line in lines(2000):
        w.streamer.write(line)
    w.streamer.finish()
    w.pump(rounds=100)
    assert w.streamer.complete
    text = w.text()
    assert text.endswith("[octopus] log truncated: over 50000 bytes\n")
    assert len(text.encode()) < 60_000


def test_spool_is_removed_on_close(tmp_path):
    w = World(tmp_path)
    w.streamer.write("x")
    w.streamer.finish()
    w.pump()
    w.streamer.close(timeout_s=0)
    assert list((tmp_path / "spool").iterdir()) == []


def test_close_without_acks_reports_incomplete(tmp_path):
    w = World(tmp_path)
    w.collector.unwatch("job-1", w.runner.runner_id)
    w.streamer.write("x")
    assert w.streamer.close(timeout_s=0) is False


def test_streamer_with_a_background_thread(tmp_path):
    bus = MemoryBus()
    coord_key, runner_key = generate_private_key(), generate_private_key()
    store = LogStore()
    collector = LogCollector(MemoryTransport(bus), coord_key, store)
    runner = Presence(
        runner_id=node_id(runner_key.public_key()),
        os="linux",
        arch="x64",
        sent_at=0,
        pubkey=encode_public_key(runner_key.public_key()),
    )
    collector.watch("job-9", runner)
    streamer = LogStreamer(
        MemoryTransport(bus),
        runner_key,
        collector.coordinator_id,
        encode_public_key(coord_key.public_key()),
        "job-9",
        tmp_path,
    )
    streamer.start()
    for line in lines(500):
        streamer.write(line)
    assert streamer.close(timeout_s=10)
    assert "".join(c.data for c in store.read("job-9")).splitlines() == lines(500)


def test_runner_markers_are_reported(tmp_path):
    seen = []
    w = World(tmp_path, on_marker=seen.append)
    w.streamer.write("2026-10-20 10:00:00Z: Running job: build")
    w.streamer.write("compiling")
    w.streamer.write("2026-10-20 10:01:00Z: Job build completed with result: Failed")
    assert seen == [
        RunnerMarker("job-started", "build"),
        RunnerMarker("job-completed", "build", "Failed"),
    ]


def test_a_failing_marker_handler_does_not_lose_output(tmp_path):
    def boom(marker):
        raise RuntimeError("consumer bug")

    w = World(tmp_path, on_marker=boom)
    w.streamer.write("2026-10-20 10:00:00Z: Running job: build")
    w.streamer.finish()
    w.pump()
    assert "Running job: build" in w.text()


def test_streamer_rejects_a_key_that_does_not_match_the_id(tmp_path):
    bus = MemoryBus()
    with pytest.raises(ValueError):
        LogStreamer(
            MemoryTransport(bus),
            generate_private_key(),
            "not-the-id",
            encode_public_key(generate_private_key().public_key()),
            "j",
            tmp_path,
        )


# --- coordinator collector --------------------------------------------------------------


def test_chunk_for_another_job_is_dropped(tmp_path):
    w = World(tmp_path)
    channel = w.streamer.channel_id
    w.runner_transport.send_on_channel(
        channel, LogChunk(job_id="job-2", seq=0, data="x").model_dump_json().encode()
    )
    assert w.store.state("job-2") is None and w.store.state("job-1") is None


def test_oversized_chunk_is_dropped(tmp_path):
    w = World(tmp_path)
    big = LogChunk(job_id="job-1", seq=0, data="x" * (MAX_LOG_CHUNK_BYTES + 1))
    w.runner_transport.send_on_channel(w.streamer.channel_id, big.model_dump_json().encode())
    assert w.store.state("job-1") is None


def test_acks_carry_the_contiguous_seq(tmp_path):
    w = World(tmp_path)
    acks = []
    real = w.streamer._on_message
    w.streamer._on_message = lambda p: (acks.append(LogAck.model_validate_json(p)), real(p))
    channel = w.streamer.channel_id
    for seq in (0, 2, 1):
        w.runner_transport.send_on_channel(
            channel, LogChunk(job_id="job-1", seq=seq, data=f"{seq}\n").model_dump_json().encode()
        )
    assert [a.seq for a in acks] == [0, 0, 2]


def test_completed_channel_closes_after_linger(tmp_path):
    w = World(tmp_path)
    w.streamer.write("x")
    w.streamer.finish()
    w.pump()
    assert w.collector.watching() == [("job-1", w.runner.runner_id)]
    w.collector.tick()
    assert w.collector.watching()
    w.clock.advance(200)
    w.collector.tick()
    assert w.collector.watching() == []


def test_watch_rejects_a_mismatched_key(tmp_path):
    w = World(tmp_path)
    impostor = w.runner.model_copy(
        update={"pubkey": encode_public_key(generate_private_key().public_key())}
    )
    with pytest.raises(ValueError):
        w.collector.watch("job-2", impostor)
    with pytest.raises(ValueError):
        w.collector.watch("job-3", w.runner.model_copy(update={"pubkey": None}))


# --- the 50 MB acceptance test ----------------------------------------------------------


def test_a_50_mb_log_does_not_exhaust_memory(tmp_path):
    w = World(tmp_path, store=LogStore(str(tmp_path / "logs.db")), window=16)
    w.bus.wire = deque(maxlen=0)  # the fake bus records every frame; measure our code only
    line = "y" * 4000
    total = 50 * 1024 * 1024
    tracemalloc.start()
    try:
        written = 0
        while written < total:
            w.streamer.write(line)
            written += len(line) + 1
            if written % (1024 * 1024) < len(line) + 1:
                w.streamer.tick()  # send as we go, like the background thread
        w.streamer.finish()
        w.pump(rounds=10_000, step_s=0.0)
        assert w.streamer.complete
        size = 0
        for chunk in w.store.read("job-1"):
            size += len(chunk.data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert size == written
    assert peak < 4 * 1024 * 1024, f"peak {peak / 2**20:.1f} MiB"


# --- helpers ----------------------------------------------------------------------------


def test_masker_ignores_tiny_values_and_prefers_longest():
    m = SecretMasker(["ab", "secret", "secret-longer"])
    assert m.mask("ab secret-longer secret") == "ab *** ***"


def test_split_utf8_never_cuts_a_character():
    text = "é" * 10 + "😀" * 10
    pieces = _split_utf8(text, 7)
    assert "".join(pieces) == text
    assert all(len(p.encode()) <= 7 for p in pieces)


def test_runner_marker_ignores_other_lines():
    assert runner_marker("Running job: build") is None  # no timestamp prefix
    assert runner_marker("2026-10-20 10:00:00Z: Listening for Jobs") is None
