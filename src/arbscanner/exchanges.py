"""Exchange data layer — market fetching and order book retrieval via pmxt."""

import logging
from typing import Any

import pmxt

from arbscanner.config import settings
from arbscanner.utils import RateLimiter, retry_with_backoff

logger = logging.getLogger(__name__)

# Shared rate limiter across all threads. Protects both exchanges from a burst
# of concurrent requests when the ThreadPoolExecutor spins up in engine.scan_all_pairs.
_rate_limiter = RateLimiter(calls_per_sec=settings.rate_limit_per_sec)


def create_exchanges() -> tuple[Any, Any]:
    """Create Polymarket and Kalshi exchange instances (read-only, no auth)."""
    poly = pmxt.Polymarket()
    kalshi = pmxt.Kalshi()
    return poly, kalshi


@retry_with_backoff(
    max_attempts=settings.retry_attempts,
    base_delay=settings.retry_base_delay,
)
def _fetch_markets_page(exchange: Any, params: dict[str, Any]) -> Any:
    """Single paginated fetch call with retry."""
    _rate_limiter.acquire()
    return exchange.fetch_markets_paginated(**params)


def fetch_all_markets(exchange: Any, exchange_name: str) -> list:
    """Paginate through all active binary markets on an exchange.

    Filters to markets that have both .yes and .no outcomes.
    """
    all_markets = []
    cursor = None

    while True:
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            result = _fetch_markets_page(exchange, params)
        except Exception:
            logger.exception("Error fetching markets from %s", exchange_name)
            break

        for market in result.data:
            if market.yes and market.no:
                all_markets.append(market)

        logger.info(
            "Fetched %d markets from %s (total binary so far: %d)",
            len(result.data),
            exchange_name,
            len(all_markets),
        )

        if not result.next_cursor:
            break
        cursor = result.next_cursor

    return all_markets


@retry_with_backoff(
    max_attempts=settings.retry_attempts,
    base_delay=settings.retry_base_delay,
)
def _fetch_order_book(exchange: Any, outcome_id: str) -> Any:
    """Single order book fetch with retry and rate limiting."""
    _rate_limiter.acquire()
    return exchange.fetch_order_book(outcome_id)


def fetch_order_book_safe(exchange: Any, outcome_id: str) -> Any | None:
    """Fetch an order book, returning None on error after retries exhausted.

    Preserves the None-on-failure contract that engine.py expects, but adds
    retry/backoff and rate limiting under the hood.
    """
    try:
        return _fetch_order_book(exchange, outcome_id)
    except Exception:
        logger.debug("Failed to fetch order book for %s after retries", outcome_id)
        return None
