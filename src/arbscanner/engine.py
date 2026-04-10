"""Arb calculation engine — detect cross-platform arbitrage opportunities."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from arbscanner.config import kalshi_fee, poly_fee, settings
from arbscanner.exchanges import fetch_order_book_safe
from arbscanner.models import ArbOpportunity, MatchedPair

logger = logging.getLogger(__name__)


def calculate_arb(
    pair: MatchedPair,
    books: dict[str, object | None],
) -> list[ArbOpportunity]:
    """Calculate arb opportunities for a single matched pair from pre-fetched books.

    Args:
        pair: The matched market pair.
        books: Dict mapping outcome_id -> OrderBook (or None if fetch failed).

    Checks both directions:
      1. Buy YES on Poly + Buy NO on Kalshi
      2. Buy NO on Poly + Buy YES on Kalshi

    Returns opportunities with positive net edge.
    """
    poly_yes_book = books.get(pair.poly_yes_outcome_id)
    poly_no_book = books.get(pair.poly_no_outcome_id)
    kalshi_yes_book = books.get(pair.kalshi_yes_outcome_id)
    kalshi_no_book = books.get(pair.kalshi_no_outcome_id)

    opportunities: list[ArbOpportunity] = []
    now = datetime.now(timezone.utc)

    # Direction 1: Buy YES on Poly + Buy NO on Kalshi
    if poly_yes_book and kalshi_no_book:
        poly_ask = _best_ask(poly_yes_book)
        kalshi_ask = _best_ask(kalshi_no_book)

        if poly_ask and kalshi_ask:
            poly_price = poly_ask["price"]
            kalshi_price = kalshi_ask["price"]
            total_cost = poly_price + kalshi_price
            gross = 1.0 - total_cost
            net = gross - poly_fee(poly_price) - kalshi_fee(kalshi_price)
            size = min(poly_ask["amount"], kalshi_ask["amount"])

            if net > 0:
                opportunities.append(
                    ArbOpportunity(
                        poly_title=pair.poly_title,
                        kalshi_title=pair.kalshi_title,
                        poly_market_id=pair.poly_market_id,
                        kalshi_market_id=pair.kalshi_market_id,
                        direction="poly_yes_kalshi_no",
                        poly_price=poly_price,
                        kalshi_price=kalshi_price,
                        gross_edge=gross,
                        net_edge=net,
                        available_size=size,
                        expected_profit=net * size,
                        timestamp=now,
                    )
                )

    # Direction 2: Buy NO on Poly + Buy YES on Kalshi
    if poly_no_book and kalshi_yes_book:
        poly_ask = _best_ask(poly_no_book)
        kalshi_ask = _best_ask(kalshi_yes_book)

        if poly_ask and kalshi_ask:
            poly_price = poly_ask["price"]
            kalshi_price = kalshi_ask["price"]
            total_cost = poly_price + kalshi_price
            gross = 1.0 - total_cost
            net = gross - poly_fee(poly_price) - kalshi_fee(kalshi_price)
            size = min(poly_ask["amount"], kalshi_ask["amount"])

            if net > 0:
                opportunities.append(
                    ArbOpportunity(
                        poly_title=pair.poly_title,
                        kalshi_title=pair.kalshi_title,
                        poly_market_id=pair.poly_market_id,
                        kalshi_market_id=pair.kalshi_market_id,
                        direction="poly_no_kalshi_yes",
                        poly_price=poly_price,
                        kalshi_price=kalshi_price,
                        gross_edge=gross,
                        net_edge=net,
                        available_size=size,
                        expected_profit=net * size,
                        timestamp=now,
                    )
                )

    return opportunities


def _best_ask(order_book) -> dict | None:
    """Extract the best (lowest) ask from an order book."""
    if not order_book.asks:
        return None
    best = order_book.asks[0]
    return {"price": best.price, "amount": best.amount}


def _fetch_all_books(
    poly_exchange,
    kalshi_exchange,
    pairs: list[MatchedPair],
    max_workers: int,
) -> dict[str, object | None]:
    """Fetch order books for every outcome across all pairs in parallel.

    Returns a dict keyed by outcome_id. Failed fetches map to None.
    """
    # Build flat list of (exchange, outcome_id) to fetch
    tasks: list[tuple[object, str]] = []
    for pair in pairs:
        tasks.append((poly_exchange, pair.poly_yes_outcome_id))
        tasks.append((poly_exchange, pair.poly_no_outcome_id))
        tasks.append((kalshi_exchange, pair.kalshi_yes_outcome_id))
        tasks.append((kalshi_exchange, pair.kalshi_no_outcome_id))

    books: dict[str, object | None] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_outcome = {
            executor.submit(fetch_order_book_safe, exchange, outcome_id): outcome_id
            for exchange, outcome_id in tasks
            if outcome_id  # skip empty IDs
        }
        for future in as_completed(future_to_outcome):
            outcome_id = future_to_outcome[future]
            try:
                books[outcome_id] = future.result()
            except Exception:
                logger.exception("Unexpected error fetching book for %s", outcome_id)
                books[outcome_id] = None

    return books


def scan_all_pairs(
    poly_exchange,
    kalshi_exchange,
    pairs: list[MatchedPair],
    threshold: float | None = None,
    max_workers: int | None = None,
) -> list[ArbOpportunity]:
    """Scan all matched pairs and return sorted arb opportunities.

    Fetches all order books in parallel using a ThreadPoolExecutor, then
    computes arbs locally from the pre-fetched books. Rate limiting is handled
    by the shared limiter in arbscanner.exchanges.

    Returns opportunities with net_edge above threshold, sorted by expected_profit descending.
    """
    if threshold is None:
        threshold = settings.edge_threshold
    if max_workers is None:
        max_workers = settings.max_workers

    if not pairs:
        return []

    # Phase 1: parallel fetch of all order books
    books = _fetch_all_books(poly_exchange, kalshi_exchange, pairs, max_workers)

    # Phase 2: compute arbs locally (fast, no I/O)
    all_opps: list[ArbOpportunity] = []
    for pair in pairs:
        try:
            opps = calculate_arb(pair, books)
            all_opps.extend(opps)
        except Exception:
            logger.exception("Error scanning pair: %s <-> %s", pair.poly_title, pair.kalshi_title)

    # Filter by threshold and sort by expected profit
    filtered = [o for o in all_opps if o.net_edge >= threshold]
    filtered.sort(key=lambda o: o.expected_profit, reverse=True)

    logger.info("Found %d opportunities above %.1f%% threshold", len(filtered), threshold * 100)
    return filtered
