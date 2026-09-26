"""Metrics for SocketFlow.

Counters, gauges, and timing histograms with labels, plus a Prometheus text
exporter. Recording is cheap and always available; exporting is opt-in.

    registry = MetricsRegistry()
    registry.increment("messages_sent", path="echo")
    print(registry.prometheus())
"""

import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
)

_LabelKey = Tuple[Tuple[str, str], ...]


def _key(labels: Optional[Dict[str, Any]]) -> _LabelKey:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_text(labels: _LabelKey) -> str:
    """Stable, hashable, JSON-friendly key for a label set."""
    if not labels:
        return ""
    return ",".join(f"{key}={value}" for key, value in labels)


def _render_labels(labels: _LabelKey) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + inner + "}"


class Histogram:
    """Cumulative bucket histogram for timings and sizes."""

    def __init__(self, buckets: Iterable[float] = DEFAULT_BUCKETS):
        self.buckets = tuple(sorted(buckets))
        self.count = 0
        self.total = 0.0
        self._counts = {bucket: 0 for bucket in self.buckets}

    def observe(self, value: float):
        self.count += 1
        self.total += value
        for bucket in self.buckets:
            if value <= bucket:
                self._counts[bucket] += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "sum": self.total,
            "buckets": dict(self._counts),
            "avg": (self.total / self.count) if self.count else 0.0,
        }


class MetricsRegistry:
    """Thread-safe metric storage."""

    def __init__(self, namespace: str = "socketflow"):
        self.namespace = namespace
        self._lock = threading.RLock()
        self._counters: Dict[str, Dict[_LabelKey, float]] = {}
        self._gauges: Dict[str, Dict[_LabelKey, float]] = {}
        self._histograms: Dict[str, Dict[_LabelKey, Histogram]] = {}
        self._help: Dict[str, str] = {}

    # ---------------------------------------------------------------- recording

    def describe(self, name: str, help_text: str):
        """Attach documentation to a metric; shown in Prometheus output."""
        with self._lock:
            self._help[name] = help_text

    def increment(self, name: str, value: float = 1, **labels):
        """Add to a counter."""
        with self._lock:
            series = self._counters.setdefault(name, {})
            label_key = _key(labels)
            series[label_key] = series.get(label_key, 0) + value

    def gauge(self, name: str, value: float, **labels):
        """Set a gauge to an absolute value."""
        with self._lock:
            self._gauges.setdefault(name, {})[_key(labels)] = value

    def gauge_add(self, name: str, value: float, **labels):
        """Add to a gauge, e.g. for active connection counts."""
        with self._lock:
            series = self._gauges.setdefault(name, {})
            label_key = _key(labels)
            series[label_key] = series.get(label_key, 0) + value

    def observe(self, name: str, value: float, **labels):
        """Record a timing or size into a histogram."""
        with self._lock:
            series = self._histograms.setdefault(name, {})
            label_key = _key(labels)
            histogram = series.get(label_key)
            if histogram is None:
                histogram = Histogram()
                series[label_key] = histogram
            histogram.observe(value)

    class _Timer:
        def __init__(self, registry, name, labels):
            self._registry = registry
            self._name = name
            self._labels = labels
            self._start = 0.0
            self.elapsed = 0.0

        def __enter__(self):
            self._start = time.perf_counter()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.elapsed = time.perf_counter() - self._start
            self._registry.observe(self._name, self.elapsed, **self._labels)
            return False

    def timer(self, name: str, **labels) -> "MetricsRegistry._Timer":
        """Time a block: with registry.timer("x") as t: ..."""
        return MetricsRegistry._Timer(self, name, labels)

    # ----------------------------------------------------------------- reading

    def counter_value(self, name: str, **labels) -> float:
        with self._lock:
            return self._counters.get(name, {}).get(_key(labels), 0)

    def gauge_value(self, name: str, **labels) -> float:
        with self._lock:
            return self._gauges.get(name, {}).get(_key(labels), 0)

    def reset(self):
        """Clear all metrics."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def snapshot(self) -> Dict[str, Any]:
        """Plain nested dict of every metric.

        Series are keyed by a readable label string such as "path=echo";
        metrics without labels use the empty string.
        """
        with self._lock:
            data: Dict[str, Any] = {"counters": {}, "gauges": {}, "histograms": {}}
            for name, series in self._counters.items():
                data["counters"][name] = {
                    _label_text(labels): value for labels, value in series.items()
                }
            for name, series in self._gauges.items():
                data["gauges"][name] = {
                    _label_text(labels): value for labels, value in series.items()
                }
            for name, series in self._histograms.items():
                data["histograms"][name] = {
                    _label_text(labels): hist.snapshot()
                    for labels, hist in series.items()
                }
            return data

    # ---------------------------------------------------------------- exporting

    def _full_name(self, name: str) -> str:
        if self.namespace and not name.startswith(f"{self.namespace}_"):
            return f"{self.namespace}_{name}"
        return name

    def prometheus(self) -> str:
        """Render every metric in the Prometheus text exposition format."""
        lines: List[str] = []
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            gauges = {k: dict(v) for k, v in self._gauges.items()}
            histograms = {k: dict(v) for k, v in self._histograms.items()}
            help_text = dict(self._help)

        for name, series in sorted(counters.items()):
            full = self._full_name(name)
            if name in help_text:
                lines.append(f"# HELP {full} {help_text[name]}")
            lines.append(f"# TYPE {full} counter")
            for labels, value in sorted(series.items()):
                lines.append(f"{full}{_render_labels(labels)} {value}")

        for name, series in sorted(gauges.items()):
            full = self._full_name(name)
            if name in help_text:
                lines.append(f"# HELP {full} {help_text[name]}")
            lines.append(f"# TYPE {full} gauge")
            for labels, value in sorted(series.items()):
                lines.append(f"{full}{_render_labels(labels)} {value}")

        for name, series in sorted(histograms.items()):
            full = self._full_name(name)
            if name in help_text:
                lines.append(f"# HELP {full} {help_text[name]}")
            lines.append(f"# TYPE {full} histogram")
            for labels, histogram in sorted(series.items()):
                snapshot = histogram.snapshot()
                lines.append(f"{full}_sum{_render_labels(labels)} {snapshot['sum']}")
                lines.append(f"{full}_count{_render_labels(labels)} {snapshot['count']}")
                for bucket, count in snapshot["buckets"].items():
                    bucket_labels = labels + (("le", str(bucket)),)
                    lines.append(
                        f"{full}_bucket{_render_labels(bucket_labels)} {count}"
                    )
                lines.append(f'{full}_bucket{{le="+Inf"}} {snapshot["count"]}')

        return "\n".join(lines) + ("\n" if lines else "")

