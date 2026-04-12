"""Performance benchmark tests for the scanner.

These tests measure and enforce timing characteristics of the parallel engine
and the shared rate limiter. They are marked ``slow`` so CI can skip them via
``pytest -m "not slow"`` when only running the fast suite.
"""

import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from arbscanner.engine import scan_all_pairs
from arbscanner.models import MatchedPair
from arbscanner.utils import RateLimiter, retry_with_backoff

pytestmark = pytest.mark.slow


# --- Local mock patterns (mirroring test_engine.py) ---


@dataclass
class MockOrderLevel:
    price: float
    amount: float


@dataclass
class MockOrderBook:
    bids: list
    asks: list


def _make_pair(i: int) -> MatchedPair:
    """Build a MatchedPair with unique per-index outcome IDs."""
    return MatchedPair(
        poly_market_id=f"poly_{i}",
        poly_title=f"Will event {i} happen?",
        kalshi_market_id=f"kalshi_{i}",
        kalshi_title=f"KX-EVENT-{i}",
        confidence=0.95,
        source="embedding",
        matched_at="2026-04-10T00:00:00Z",
        poly_yes_outcome_id=f"py{i}",
        poly_no_outcome_id=f"pn{i}",
        kalshi_yes_outcome_id=f"ky{i}",
        kalshi_no_outcome_id=f"kn{i}",
    )


def _make_pairs(n: int) -> list[MatchedPair]:
    return [_make_pair(i) for i in range(n)]


def _slow_fetch(sleep_seconds: float = 0.05):
    """Return a fake fetch_order_book_safe that sleeps deterministically per call."""

    def fetcher(exchange, outcome_id):
        time.sleep(sleep_seconds)
        return MockOrderBook(bids=[], asks=[])

    return fetcher


# --- Tests ---


def test_parallel_scan_beats_sequential_for_many_pairs():
    """Parallel scanning with 10 workers should be at least 3x faster than 1 worker."""
    pairs = _make_pairs(20)
    fetcher = _slow_fetch(0.05)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=fetcher):
        start = time.monotonic()
        scan_all_pairs(
            MagicMock(), MagicMock(), pairs, threshold=0.01, max_workers=1
        )
        sequential_elapsed = time.monotonic() - start

        start = time.monotonic()
        scan_all_pairs(
            MagicMock(), MagicMock(), pairs, threshold=0.01, max_workers=10
        )
        parallel_elapsed = time.monotonic() - start

    assert parallel_elapsed > 0
    speedup = sequential_elapsed / parallel_elapsed
    assert speedup >= 3.0, (
        f"Expected parallel scan to be >=3x faster than sequential, "
        f"got sequential={sequential_elapsed:.3f}s, parallel={parallel_elapsed:.3f}s, "
        f"speedup={speedup:.2f}x"
    )


def test_scan_scales_sub_linearly():
    """With a worker pool large enough to saturate the workload, 6x more pairs
    should take roughly the same wall time (good scaling).

    Each pair triggers 2 fetches (poly + kalshi), so ``max_workers=150`` is
    enough that every fetch in the 60-pair scan (120 fetches) can run in a
    single round. At that point both scans are bounded by the fetch latency
    (``0.05s``), not by the worker count, so the ratio should be near 1×
    plus CI overhead.
    """
    fetcher = _slow_fetch(0.05)

    timings: dict[int, float] = {}
    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=fetcher):
        for n in (10, 30, 60):
            pairs = _make_pairs(n)
            start = time.monotonic()
            scan_all_pairs(
                MagicMock(), MagicMock(), pairs, threshold=0.01, max_workers=150
            )
            timings[n] = time.monotonic() - start

    assert timings[10] > 0
    ratio = timings[60] / timings[10]
    assert ratio < 3.0, (
        f"Expected 60-pair scan to be <3x the 10-pair scan, "
        f"got {timings[10]:.3f}s vs {timings[60]:.3f}s (ratio={ratio:.2f}x)"
    )


def test_rate_limiter_throughput_matches_config():
    """A 20 calls/sec limiter across 8 threads x 25 calls should take ~10s total."""
    limiter = RateLimiter(calls_per_sec=20)
    num_threads = 8
    calls_per_thread = 25
    total_calls = num_threads * calls_per_thread  # 200

    def worker():
        for _ in range(calls_per_thread):
            limiter.acquire()

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]

    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    expected = total_calls / 20  # 10 seconds
    assert 9.0 <= elapsed <= 13.0, (
        f"Expected rate limiter elapsed in [9, 13]s for {total_calls} calls at 20/s "
        f"(ideal {expected:.1f}s), got {elapsed:.3f}s"
    )


def test_retry_backoff_respects_delays():
    """retry_with_backoff should sleep 0.05+0.10+0.20 = 0.35s across 4 attempts."""
    call_count = {"n": 0}

    @retry_with_backoff(max_attempts=4, base_delay=0.05)
    def always_fails():
        call_count["n"] += 1
        raise ValueError("boom")

    start = time.monotonic()
    with pytest.raises(ValueError):
        always_fails()
    elapsed = time.monotonic() - start

    assert call_count["n"] == 4
    assert 0.3 <= elapsed <= 0.6, (
        f"Expected retry elapsed in [0.3, 0.6]s (ideal ~0.35s), got {elapsed:.3f}s"
    )


def test_empty_scan_is_fast():
    """Scanning an empty pair list should short-circuit and return in <50ms."""
    start = time.monotonic()
    result = scan_all_pairs(MagicMock(), MagicMock(), [], max_workers=8)
    elapsed = time.monotonic() - start

    assert result == []
    assert elapsed < 0.05, f"Expected empty scan <50ms, got {elapsed * 1000:.2f}ms"
