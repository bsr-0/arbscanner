"""Exchange data layer — market fetching and order book retrieval via pmxt."""

import logging
from typing import Any

import pmxt

logger = logging.getLogger(__name__)


def create_exchanges() -> tuple[Any, Any]:
    """Create Polymarket and Kalshi exchange instances (read-only, no auth)."""
    poly = pmxt.Polymarket()
    kalshi = pmxt.Kalshi()
    return poly, kalshi


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
            result = exchange.fetch_markets_paginated(**params)
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


def fetch_order_book_safe(exchange: Any, outcome_id: str) -> Any | None:
    """Fetch an order book, returning None on error."""
    try:
        return exchange.fetch_order_book(outcome_id)
    except Exception:
        logger.debug("Failed to fetch order book for %s", outcome_id)
        return None
