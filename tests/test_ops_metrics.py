import pytest

from octopus.coordinator.ops import MetricsRegistry


def test_counter_renders_labels_and_escapes():
    r = MetricsRegistry()
    c = r.counter("octopus_x_total", "X things.", ["reason"])
    c.inc(reason='a"b\\c\nd')
    c.inc(2, reason="plain")
    out = r.render()
    assert "# HELP octopus_x_total X things." in out
    assert "# TYPE octopus_x_total counter" in out
    assert 'octopus_x_total{reason="plain"} 2' in out
    assert 'octopus_x_total{reason="a\\"b\\\\c\\nd"} 1' in out
    assert out.endswith("\n")


def test_counter_rejects_decrease_and_wrong_labels():
    c = MetricsRegistry().counter("octopus_c_total", "C.", ["a"])
    with pytest.raises(ValueError, match="only go up"):
        c.inc(-1, a="x")
    with pytest.raises(ValueError, match="wrong labels"):
        c.inc(b="x")
    with pytest.raises(ValueError, match="wrong labels"):
        c.inc()


def test_gauge_set_and_function():
    r = MetricsRegistry()
    g = r.gauge("octopus_g", "G.", ["k"])
    g.set(3, k="a")
    assert 'octopus_g{k="a"} 3' in r.render()
    g.set_function(lambda: {("b",): 1.5})
    out = r.render()
    assert 'octopus_g{k="b"} 1.5' in out
    assert 'k="a"' not in out

    scalar = MetricsRegistry()
    scalar.gauge("octopus_s", "S.").set_function(lambda: 4)
    assert "octopus_s 4\n" in scalar.render()


def test_histogram_buckets_are_cumulative():
    r = MetricsRegistry()
    h = r.histogram("octopus_lat_seconds", "Latency.", buckets=(1, 5))
    for v in (0.5, 2, 2, 9):
        h.observe(v)
    out = r.render()
    assert 'octopus_lat_seconds_bucket{le="1"} 1' in out
    assert 'octopus_lat_seconds_bucket{le="5"} 3' in out
    assert 'octopus_lat_seconds_bucket{le="+Inf"} 4' in out
    assert "octopus_lat_seconds_sum 13.5" in out
    assert "octopus_lat_seconds_count 4" in out
    assert h.count() == 4


def test_duplicate_registration_rejected():
    r = MetricsRegistry()
    r.counter("octopus_dup_total", "D.")
    with pytest.raises(ValueError, match="already registered"):
        r.gauge("octopus_dup_total", "D.")
