"""Lightweight in-process metrics for arbscanner observability.

Design
------
This module provides Counter, Gauge, and Histogram primitives that work with
only the Python standard library. The goal is to instrument the hot paths in
``arbscanner.engine`` and ``arbscanner.exchanges`` (scan cycles, order-book
fetches, rate-limit waits, alert deliveries) without pulling in any new
dependency.

Why not just use ``prometheus_client``?
    * It's an extra dependency we don't currently take.
    * We want metrics to be cheap to enable in tests and in the CLI, where
      spinning up a Prometheus HTTP server is overkill.
    * We still want Prometheus-compatible text output so that if a user runs
      the FastAPI backend behind a ``/metrics`` endpoint, scraping Just Works.

Compatibility
-------------
If ``prometheus_client`` happens to be installed, the module sets
``_HAS_PROMETHEUS = True``. The registry is designed so that a future proxy
layer can mirror our primitives into ``prometheus_client``'s own registry and
hand off ``export_text`` to its ``generate_latest``. That proxy is deliberately
left as future work --- the stdlib implementation is self-sufficient and
emits the same text exposition format described by the Prometheus project
(https://prometheus.io/docs/instrumenting/exposition_formats/).

Thread safety
-------------
Every primitive takes a ``threading.Lock`` on any mutation or read of
structured state. Label sets are normalized to sorted tuples of ``(key, value)``
pairs so they can be used as dict keys, and so that two call sites passing the
same labels in different keyword orders land in the same bucket.

Usage
-----
    from arbscanner.metrics import (
        scan_cycles_total,
        scan_cycle_seconds,
        timing_block,
    )

    with timing_block(scan_cycle_seconds):
        run_scan()
    scan_cycles_total.inc()
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Optional prometheus_client integration
# --------------------------------------------------------------------------- #
try:
    import prometheus_client as _prometheus_client  # noqa: F401

    _HAS_PROMETHEUS: bool = True
except ImportError:  # pragma: no cover - depends on environment
    _HAS_PROMETHEUS = False


# --------------------------------------------------------------------------- #
# Label helpers
# --------------------------------------------------------------------------- #
LabelKey = tuple[tuple[str, str], ...]


def _label_key(labels: dict[str, Any]) -> LabelKey:
    """Normalize a labels mapping into a hashable sorted tuple key.

    Values are coerced to strings so that ``exchange="poly"`` and
    ``exchange=Poly(..)`` produce stable keys.
    """
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _format_labels(key: LabelKey) -> str:
    """Render a label key as a Prometheus-style ``{k="v",...}`` string."""
    if not key:
        return ""
    parts = []
    for k, v in key:
        # Escape backslashes and double quotes per Prom text format.
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{k}="{escaped}"')
    return "{" + ",".join(parts) + "}"


# --------------------------------------------------------------------------- #
# Base metric
# --------------------------------------------------------------------------- #
class _Metric:
    """Shared base: name, description, and automatic registry binding."""

    kind: str = "untyped"

    def __init__(self, name: str, description: str) -> None:
        self.name: str = name
        self.description: str = description
        self._lock: threading.Lock = threading.Lock()
        MetricsRegistry.instance().register(self)

    # Subclasses must implement ``_export_lines`` to emit Prom text.
    def _export_lines(self) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Counter
# --------------------------------------------------------------------------- #
class Counter(_Metric):
    """Monotonically increasing integer counter, optionally labeled."""

    kind = "counter"

    def __init__(self, name: str, description: str) -> None:
        super().__init__(name, description)
        self._values: dict[LabelKey, int] = defaultdict(int)

    def inc(self, n: int = 1, **labels: Any) -> None:
        """Increment the counter by ``n`` (must be non-negative)."""
        if n < 0:
            raise ValueError(f"Counter {self.name} cannot be decremented (n={n})")
        key = _label_key(labels)
        with self._lock:
            self._values[key] += n

    def value(self, **labels: Any) -> int:
        """Return the current value for the given label set (0 if unseen)."""
        key = _label_key(labels)
        with self._lock:
            return self._values.get(key, 0)

    def _export_lines(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            items = list(self._values.items())
        if not items:
            # Emit a zero sample so scrapers see the series exists.
            lines.append(f"{self.name} 0")
            return lines
        for key, val in items:
            lines.append(f"{self.name}{_format_labels(key)} {val}")
        return lines


# --------------------------------------------------------------------------- #
# Gauge
# --------------------------------------------------------------------------- #
class Gauge(_Metric):
    """Arbitrary float value that can go up or down."""

    kind = "gauge"

    def __init__(self, name: str, description: str) -> None:
        super().__init__(name, description)
        self._values: dict[LabelKey, float] = defaultdict(float)

    def set(self, value: float, **labels: Any) -> None:
        """Set the gauge to an absolute value."""
        key = _label_key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, n: float = 1.0, **labels: Any) -> None:
        """Increment the gauge by ``n`` (default 1)."""
        key = _label_key(labels)
        with self._lock:
            self._values[key] += float(n)

    def dec(self, n: float = 1.0, **labels: Any) -> None:
        """Decrement the gauge by ``n`` (default 1)."""
        key = _label_key(labels)
        with self._lock:
            self._values[key] -= float(n)

    def value(self, **labels: Any) -> float:
        """Return the current value for the given label set (0.0 if unseen)."""
        key = _label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def _export_lines(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} gauge",
        ]
        with self._lock:
            items = list(self._values.items())
        if not items:
            lines.append(f"{self.name} 0")
            return lines
        for key, val in items:
            lines.append(f"{self.name}{_format_labels(key)} {val}")
        return lines


# --------------------------------------------------------------------------- #
# Histogram
# --------------------------------------------------------------------------- #
# Default buckets suited to sub-second-to-minute latencies (seconds).
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


class _HistogramState:
    """Per-label-set state for a Histogram."""

    __slots__ = ("count", "sum", "bucket_counts", "samples")

    def __init__(self, n_buckets: int) -> None:
        self.count: int = 0
        self.sum: float = 0.0
        # Cumulative counts aligned with the bucket upper bounds. The last
        # slot is the +Inf bucket.
        self.bucket_counts: list[int] = [0] * (n_buckets + 1)
        # Reservoir of raw samples for quantile estimation. Bounded so we
        # don't grow without limit in long-running processes.
        self.samples: list[float] = []


class Histogram(_Metric):
    """Bucketed histogram with count, sum, and p50/p95/p99 quantile estimates."""

    kind = "histogram"
    _MAX_SAMPLES: int = 1024

    def __init__(
        self,
        name: str,
        description: str,
        buckets: tuple[float, ...] | list[float] | None = None,
    ) -> None:
        super().__init__(name, description)
        if buckets is None:
            self.buckets: tuple[float, ...] = DEFAULT_BUCKETS
        else:
            cleaned = tuple(sorted(float(b) for b in buckets))
            if not cleaned:
                raise ValueError(f"Histogram {name}: buckets must be non-empty")
            self.buckets = cleaned
        self._states: dict[LabelKey, _HistogramState] = {}

    def _get_state(self, key: LabelKey) -> _HistogramState:
        state = self._states.get(key)
        if state is None:
            state = _HistogramState(len(self.buckets))
            self._states[key] = state
        return state

    def observe(self, value: float, **labels: Any) -> None:
        """Record a single observation into the histogram."""
        v = float(value)
        key = _label_key(labels)
        with self._lock:
            state = self._get_state(key)
            state.count += 1
            state.sum += v
            # Cumulative bucket increment: every bucket whose upper bound
            # is >= v gets +1. The +Inf bucket always increments.
            placed = False
            for i, bound in enumerate(self.buckets):
                if v <= bound:
                    state.bucket_counts[i] += 1
                    placed = True
                    break
            # If not placed in any finite bucket, it still must be counted in
            # all buckets above it; since buckets are cumulative we propagate.
            if placed:
                # Propagate up: any bucket after the first matching one also
                # includes this observation.
                start = next(
                    i for i, b in enumerate(self.buckets) if v <= b
                )
                for i in range(start + 1, len(self.buckets)):
                    state.bucket_counts[i] += 1
            # +Inf bucket
            state.bucket_counts[-1] += 1
            # Bounded reservoir for quantile estimation.
            if len(state.samples) < self._MAX_SAMPLES:
                state.samples.append(v)
            else:
                # Simple overwrite based on count modulo size — a cheap
                # approximation of reservoir sampling for long-running procs.
                state.samples[state.count % self._MAX_SAMPLES] = v

    def summary(self, **labels: Any) -> dict[str, Any]:
        """Return a summary dict for the given label set.

        Keys: ``count``, ``sum``, ``p50``, ``p95``, ``p99``, ``buckets``.
        ``buckets`` is a list of ``(upper_bound, cumulative_count)`` tuples
        where the final entry has ``upper_bound = math.inf``.
        """
        key = _label_key(labels)
        with self._lock:
            state = self._states.get(key)
            if state is None or state.count == 0:
                empty_buckets: list[tuple[float, int]] = [
                    (b, 0) for b in self.buckets
                ]
                empty_buckets.append((math.inf, 0))
                return {
                    "count": 0,
                    "sum": 0.0,
                    "p50": 0.0,
                    "p95": 0.0,
                    "p99": 0.0,
                    "buckets": empty_buckets,
                }
            samples_sorted = sorted(state.samples)
            bucket_snapshot: list[tuple[float, int]] = [
                (b, state.bucket_counts[i]) for i, b in enumerate(self.buckets)
            ]
            bucket_snapshot.append((math.inf, state.bucket_counts[-1]))
            count = state.count
            total = state.sum

        def _quantile(sorted_samples: list[float], q: float) -> float:
            if not sorted_samples:
                return 0.0
            # Nearest-rank method, clamped to the sample bounds.
            idx = max(0, min(len(sorted_samples) - 1, int(q * len(sorted_samples))))
            return sorted_samples[idx]

        return {
            "count": count,
            "sum": total,
            "p50": _quantile(samples_sorted, 0.50),
            "p95": _quantile(samples_sorted, 0.95),
            "p99": _quantile(samples_sorted, 0.99),
            "buckets": bucket_snapshot,
        }

    def _export_lines(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            items = list(self._states.items())
            buckets = self.buckets
        if not items:
            for b in buckets:
                lines.append(f'{self.name}_bucket{{le="{b}"}} 0')
            lines.append(f'{self.name}_bucket{{le="+Inf"}} 0')
            lines.append(f"{self.name}_count 0")
            lines.append(f"{self.name}_sum 0")
            return lines
        for key, state in items:
            base_labels = dict(key)
            for i, bound in enumerate(buckets):
                label_map = {**base_labels, "le": str(bound)}
                lines.append(
                    f"{self.name}_bucket{_format_labels(_label_key(label_map))} "
                    f"{state.bucket_counts[i]}"
                )
            inf_labels = {**base_labels, "le": "+Inf"}
            lines.append(
                f"{self.name}_bucket{_format_labels(_label_key(inf_labels))} "
                f"{state.bucket_counts[-1]}"
            )
            lines.append(
                f"{self.name}_count{_format_labels(key)} {state.count}"
            )
            lines.append(
                f"{self.name}_sum{_format_labels(key)} {state.sum}"
            )
        return lines


# --------------------------------------------------------------------------- #
# Registry singleton
# --------------------------------------------------------------------------- #
class MetricsRegistry:
    """Process-wide singleton holding every registered metric.

    Future work: when ``_HAS_PROMETHEUS`` is True, a proxy can mirror
    registered primitives into ``prometheus_client.REGISTRY`` and delegate
    ``export_text`` to ``prometheus_client.generate_latest``. Today we emit
    Prometheus text format directly from our own state.
    """

    _singleton: "MetricsRegistry | None" = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    @classmethod
    def instance(cls) -> "MetricsRegistry":
        """Return the process-wide registry, creating it if necessary."""
        if cls._singleton is None:
            with cls._singleton_lock:
                if cls._singleton is None:
                    cls._singleton = cls()
        return cls._singleton

    def register(self, metric: _Metric) -> None:
        """Register a metric. Re-registering the same name is a no-op so the
        module-level metrics defined below stay idempotent across reloads."""
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None and existing is not metric:
                # Someone re-created a metric with the same name; keep the
                # first one to preserve accumulated state.
                return
            self._metrics[metric.name] = metric

    def get(self, name: str) -> _Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def all_metrics(self) -> list[_Metric]:
        with self._lock:
            return list(self._metrics.values())

    def export_text(self) -> str:
        """Render every registered metric in Prometheus text exposition format."""
        lines: list[str] = []
        for metric in self.all_metrics():
            lines.extend(metric._export_lines())
        # Prom text format requires a trailing newline.
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop every registered metric. Intended for tests only."""
        with self._lock:
            self._metrics.clear()


# --------------------------------------------------------------------------- #
# Timing context manager
# --------------------------------------------------------------------------- #
@contextmanager
def timing_block(
    histogram: Histogram, **labels: Any
) -> Iterator[None]:
    """Context manager that observes wall-clock seconds into a histogram.

    Example::

        with timing_block(scan_cycle_seconds):
            run_one_scan()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        histogram.observe(elapsed, **labels)


# --------------------------------------------------------------------------- #
# Pre-registered metrics for arbscanner hot paths
# --------------------------------------------------------------------------- #
# Scan lifecycle — instrumented around engine.scan_all_pairs.
scan_cycles_total: Counter = Counter(
    "arbscanner_scan_cycles_total",
    "Total number of full scan cycles completed by engine.scan_all_pairs.",
)
scan_cycle_seconds: Histogram = Histogram(
    "arbscanner_scan_cycle_seconds",
    "Wall-clock duration of a full scan cycle (fetch + compute).",
)

# Opportunity detection — labeled by arb direction so we can spot which
# leg dominates over time (poly_yes_kalshi_no vs poly_no_kalshi_yes).
opportunities_found_total: Counter = Counter(
    "arbscanner_opportunities_found_total",
    "Arbitrage opportunities detected above the configured edge threshold.",
)

# Exchange I/O — exchanges.fetch_order_book_safe is called concurrently
# from the ThreadPoolExecutor in engine._fetch_all_books.
order_book_fetches_total: Counter = Counter(
    "arbscanner_order_book_fetches_total",
    "Order book fetch attempts via pmxt, labeled by exchange and outcome.",
)
order_book_fetch_failures_total: Counter = Counter(
    "arbscanner_order_book_fetch_failures_total",
    "Order book fetches that exhausted retries and returned None.",
)

# Rate limiting — utils.RateLimiter.acquire() blocks when over budget.
rate_limit_waits_seconds: Histogram = Histogram(
    "arbscanner_rate_limit_waits_seconds",
    "Seconds spent blocked inside the shared RateLimiter before a call.",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Alerting — alerts.py delivers to Telegram / Discord / email.
alerts_sent_total: Counter = Counter(
    "arbscanner_alerts_sent_total",
    "Alerts successfully delivered to a downstream channel.",
)


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import random

    # Simulate a scan cycle.
    with timing_block(scan_cycle_seconds):
        time.sleep(0.03)
    scan_cycles_total.inc()

    # Simulate a few opportunities.
    for _ in range(3):
        opportunities_found_total.inc(direction="poly_yes_kalshi_no")
    opportunities_found_total.inc(direction="poly_no_kalshi_yes")

    # Simulate order-book fetches.
    for _ in range(20):
        ok = random.random() > 0.1
        order_book_fetches_total.inc(
            exchange="polymarket",
            outcome="success" if ok else "error",
        )
        if not ok:
            order_book_fetch_failures_total.inc()

    # Simulate rate-limiter waits.
    for _ in range(10):
        rate_limit_waits_seconds.observe(random.uniform(0.0, 0.2))

    # Simulate alerts.
    alerts_sent_total.inc(channel="telegram")
    alerts_sent_total.inc(channel="telegram")
    alerts_sent_total.inc(channel="discord")

    print(f"_HAS_PROMETHEUS = {_HAS_PROMETHEUS}")
    print()
    print(f"scan_cycles_total = {scan_cycles_total.value()}")
    print(
        f"opportunities_found_total(poly_yes_kalshi_no) = "
        f"{opportunities_found_total.value(direction='poly_yes_kalshi_no')}"
    )
    print(
        f"order_book_fetch_failures_total = "
        f"{order_book_fetch_failures_total.value()}"
    )
    print()
    print("scan_cycle_seconds summary:")
    for k, v in scan_cycle_seconds.summary().items():
        print(f"  {k}: {v}")
    print()
    print("rate_limit_waits_seconds summary:")
    for k, v in rate_limit_waits_seconds.summary().items():
        print(f"  {k}: {v}")
    print()
    print("--- Prometheus text export ---")
    print(MetricsRegistry.instance().export_text())
