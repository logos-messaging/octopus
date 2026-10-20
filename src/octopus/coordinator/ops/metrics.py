"""A small Prometheus client: counters, gauges and histograms in the text exposition format.

Written against the standard library so the coordinator gains no dependency for its
`/metrics` endpoint. Everything is thread-safe; label values are escaped as the format needs.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence

DEFAULT_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60)

LabelValues = tuple[str, ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value)) if value != int(value) else str(int(value))


def _labels(names: Sequence[str], values: LabelValues, extra: str = "") -> str:
    pairs = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        pairs.append(extra)
    return "{" + ",".join(pairs) + "}" if pairs else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(labels)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> LabelValues:
        if set(labels) != set(self.label_names):
            raise ValueError(
                f"wrong labels for metric: {self.name}: expected {list(self.label_names)}, "
                f"got {sorted(labels)}"
            )
        return tuple(str(labels[n]) for n in self.label_names)

    def _samples(self) -> list[str]:
        raise NotImplementedError

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        return lines + self._samples()


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[LabelValues, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"counters only go up: {self.name}")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def _samples(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [f"{self.name}{_labels(self.label_names, k)} {_fmt(v)}" for k, v in items]


class Gauge(_Metric):
    """A value that goes up and down. `set_function` makes it computed at scrape time."""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[LabelValues, float] = {}
        self._function: Callable[[], dict[LabelValues, float] | float] | None = None

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def set_function(self, function: Callable[[], dict[LabelValues, float] | float]) -> None:
        """Compute the value on every scrape: a number, or {label values: number}."""
        self._function = function

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def _samples(self) -> list[str]:
        values = self._function() if self._function is not None else None
        if values is None:
            with self._lock:
                items = sorted(self._values.items())
        elif isinstance(values, dict):
            items = sorted(values.items())
        else:
            items = [((), float(values))]
        return [f"{self.name}{_labels(self.label_names, k)} {_fmt(v)}" for k, v in items]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        # per label set: ([count per bucket], sum, count)
        self._series: dict[LabelValues, tuple[list[int], list[float]]] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts, totals = self._series.setdefault(key, ([0] * len(self.buckets), [0.0, 0.0]))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            totals[0] += value
            totals[1] += 1

    def count(self, **labels: str) -> int:
        with self._lock:
            series = self._series.get(self._key(labels))
            return int(series[1][1]) if series else 0

    def _samples(self) -> list[str]:
        with self._lock:
            snapshot = sorted((k, list(c), list(t)) for k, (c, t) in self._series.items())
        lines: list[str] = []
        for key, counts, (total, n) in snapshot:
            for bound, count in zip(self.buckets, counts, strict=True):
                le = f'le="{_fmt(bound)}"'
                lines.append(f"{self.name}_bucket{_labels(self.label_names, key, le)} {count}")
            inf = 'le="+Inf"'
            lines.append(f"{self.name}_bucket{_labels(self.label_names, key, inf)} {int(n)}")
            lines.append(f"{self.name}_sum{_labels(self.label_names, key)} {_fmt(total)}")
            lines.append(f"{self.name}_count{_labels(self.label_names, key)} {int(n)}")
        return lines


class MetricsRegistry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric):
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric already registered: {metric.name}")
            self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str, labels: Sequence[str] = ()) -> Counter:
        return self.register(Counter(name, help_text, labels))

    def gauge(self, name: str, help_text: str, labels: Sequence[str] = ()) -> Gauge:
        return self.register(Gauge(name, help_text, labels))

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        return self.register(Histogram(name, help_text, labels, buckets))

    def render(self) -> str:
        with self._lock:
            metrics = sorted(self._metrics.values(), key=lambda m: m.name)
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"
