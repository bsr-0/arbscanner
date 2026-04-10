"""Shared utilities: retry decorator and thread-safe rate limiter."""

import functools
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that retries a function with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including the first).
        base_delay: Initial delay in seconds. Doubles each retry.
        exceptions: Exception types that trigger a retry.

    Raises the final exception if all attempts fail.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        break
                    delay = base_delay * (2**attempt)
                    logger.debug(
                        "Retry %d/%d for %s after %.2fs: %s",
                        attempt + 1,
                        max_attempts,
                        func.__name__,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


class RateLimiter:
    """Thread-safe token bucket rate limiter.

    Allows up to `calls_per_sec` operations per second across all threads.
    Callers block on `acquire()` until a slot is available.
    """

    def __init__(self, calls_per_sec: float):
        if calls_per_sec <= 0:
            raise ValueError("calls_per_sec must be positive")
        self._interval = 1.0 / calls_per_sec
        self._lock = threading.Lock()
        self._next_available = time.monotonic()

    def acquire(self) -> None:
        """Block until the next call slot is available."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_available - now
            if wait > 0:
                time.sleep(wait)
                self._next_available += self._interval
            else:
                # Align next slot to current time so idle periods don't accumulate burst
                self._next_available = now + self._interval
