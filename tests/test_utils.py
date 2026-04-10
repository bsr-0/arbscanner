"""Tests for retry decorator and rate limiter."""

import threading
import time

import pytest

from arbscanner.utils import RateLimiter, retry_with_backoff


def test_retry_succeeds_first_try():
    calls = []

    @retry_with_backoff(max_attempts=3)
    def ok():
        calls.append(1)
        return "success"

    assert ok() == "success"
    assert len(calls) == 1


def test_retry_succeeds_after_failures():
    calls = []

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("transient")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_retry_exhausted():
    calls = []

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def always_fails():
        calls.append(1)
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
        always_fails()
    assert len(calls) == 3


def test_retry_exponential_backoff():
    """Backoff delays should grow exponentially: 0.01, 0.02 ~= 0.03s total."""
    calls = []

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def fails_twice():
        calls.append(time.monotonic())
        if len(calls) < 3:
            raise ValueError()
        return "ok"

    start = time.monotonic()
    fails_twice()
    elapsed = time.monotonic() - start
    # Total sleep should be ~0.01 + ~0.02 = 0.03s (plus overhead)
    assert elapsed >= 0.025
    assert elapsed < 0.5


def test_retry_only_catches_specified_exceptions():
    @retry_with_backoff(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
    def raises_type():
        raise TypeError("not retryable")

    with pytest.raises(TypeError):
        raises_type()


def test_rate_limiter_invalid():
    with pytest.raises(ValueError):
        RateLimiter(calls_per_sec=0)
    with pytest.raises(ValueError):
        RateLimiter(calls_per_sec=-1.0)


def test_rate_limiter_throttles_sequential_calls():
    """10 calls at 20/sec should take at least ~0.45s."""
    limiter = RateLimiter(calls_per_sec=20.0)

    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.monotonic() - start

    # 10 calls at 20/sec = ~0.5s. Allow slack for CI variance.
    assert elapsed >= 0.4
    assert elapsed < 2.0


def test_rate_limiter_threadsafe():
    """Multiple threads sharing a limiter should be throttled collectively."""
    limiter = RateLimiter(calls_per_sec=50.0)
    call_times: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            limiter.acquire()
            with lock:
                call_times.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    # 20 total calls at 50/sec = ~0.4s minimum
    assert len(call_times) == 20
    assert elapsed >= 0.3
