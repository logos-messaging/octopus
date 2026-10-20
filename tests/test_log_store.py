import pytest

from octopus.coordinator.logs import LogStore
from octopus.proto import LogChunk

R1, R2 = "runner-one", "runner-two"


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return LogStore(clock=clock)


def chunk(seq, data=None, final=False, job="j1"):
    return LogChunk(
        job_id=job, seq=seq, data=f"line {seq}\n" if data is None else data, final=final
    )


def test_in_order_stream_completes(store):
    for seq in range(3):
        st = store.put(R1, chunk(seq, final=seq == 2))
    assert st.contiguous == 2 and st.final_seq == 2 and st.complete
    assert [c.seq for c in store.read("j1")] == [0, 1, 2]
    assert [c.final for c in store.read("j1")] == [False, False, True]


def test_duplicates_are_ignored(store):
    store.put(R1, chunk(0, "first\n"))
    st = store.put(R1, chunk(0, "second\n"))
    assert [c.data for c in store.read("j1")] == ["first\n"]
    assert st.bytes == len("first\n")


def test_out_of_order_and_gaps(store):
    for seq in (0, 3, 1, 5):
        st = store.put(R1, chunk(seq))
    assert st.contiguous == 1
    assert store.gaps("j1") == [(2, 2), (4, 4)]
    store.put(R1, chunk(6, final=True))
    assert store.gaps("j1") == [(2, 2), (4, 4)]
    store.put(R1, chunk(2))
    st = store.put(R1, chunk(4))
    assert st.contiguous == 6 and st.complete and store.gaps("j1") == []


def test_gap_up_to_a_known_final(store):
    store.put(R1, chunk(0))
    store.put(R1, chunk(4, final=True))
    assert store.gaps("j1") == [(1, 3)]


def test_nothing_after_the_final_chunk(store):
    store.put(R1, chunk(0))
    store.put(R1, chunk(1, final=True))
    st = store.put(R1, chunk(2))
    assert st.contiguous == 1 and [c.seq for c in store.read("j1")] == [0, 1]


@pytest.mark.parametrize("seq", [-1, 10_000_001])
def test_seq_out_of_range(store, seq):
    with pytest.raises(ValueError):
        store.put(R1, chunk(seq))


def test_read_ranges(store):
    for seq in range(200):
        store.put(R1, chunk(seq))
    assert [c.seq for c in store.read("j1", 10, 12)] == [10, 11, 12]
    assert len(list(store.read("j1"))) == 200  # across several batches
    assert list(store.read("unknown")) == []


def test_tail(store):
    store.put(R1, chunk(0, "a\nb\n"))
    store.put(R1, chunk(1, "c\nd\ne\n"))
    assert store.tail("j1", 2) == ["d", "e"]
    assert store.tail("j1", 10) == ["a", "b", "c", "d", "e"]
    assert store.tail("j1", 0) == [] and store.tail("unknown") == []


def test_tail_spans_many_chunks(store):
    for seq in range(300):
        store.put(R1, chunk(seq))
    assert store.tail("j1", 150) == [f"line {s}" for s in range(150, 300)]


def test_streams_per_runner_newest_by_default(store, clock):
    store.put(R1, chunk(0, "attempt one\n"))
    clock.t += 10
    store.put(R2, chunk(0, "attempt two\n"))
    assert [c.data for c in store.read("j1")] == ["attempt two\n"]
    assert [c.data for c in store.read("j1", runner_id=R1)] == ["attempt one\n"]
    assert [s.runner_id for s in store.streams("j1")] == [R1, R2]


def test_size_cap_truncates_and_still_completes(clock):
    store = LogStore(clock=clock, max_stream_bytes=25)
    for seq in range(4):
        st = store.put(R1, chunk(seq, "0123456789\n", final=seq == 3))
    data = [c.data for c in store.read("j1")]
    assert data[:2] == ["0123456789\n", "0123456789\n"]
    assert data[2] == "[octopus] log truncated: over 25 bytes\n" and data[3] == ""
    assert st.complete and st.truncated


def test_retention(store, clock):
    store.put(R1, chunk(0))
    clock.t += 10
    store.put(R1, chunk(0, job="j2"))
    assert store.purge(older_than_s=5) == 1
    assert store.state("j1") is None and store.state("j2") is not None


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "logs.db")
    first = LogStore(path)
    first.put(R1, chunk(0))
    first.put(R1, chunk(2))
    first.close()
    again = LogStore(path)
    st = again.put(R1, chunk(1))
    assert st.contiguous == 2


def test_delete(store):
    store.put(R1, chunk(0))
    store.put(R2, chunk(0))
    store.delete("j1")
    assert store.streams("j1") == [] and list(store.read("j1")) == []
