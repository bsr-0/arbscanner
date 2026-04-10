"""Arb calculation engine — detect cross-platform arbitrage opportunities."""

import logging
from datetime import datetime, timezone

from arbscanner.config import kalshi_fee, poly_fee, settings
from arbscanner.exchanges import fetch_order_book_safe
from arbscanner.models import ArbOpportunity, MatchedPair

logger = logging.getLogger(__name__)


def calculate_arb(
    poly_exchange,
    kalshi_exchange,
    pair: MatchedPair,
) -> list[ArbOpportunity]:
    """Calculate arb opportunities for a single matched pair.

    Checks both directions:
      1. Buy YES on Poly + Buy NO on Kalshi
      2. Buy NO on Poly + Buy YES on Kalshi

    Returns opportunities with positive net edge.
    """
    # Fetch order books for all four outcomes
    poly_yes_book = fetch_order_book_safe(poly_exchange, pair.poly_yes_outcome_id)
    poly_no_book = fetch_order_book_safe(poly_exchange, pair.poly_no_outcome_id)
    kalshi_yes_book = fetch_order_book_safe(kalshi_exchange, pair.kalshi_yes_outcome_id)
    kalshi_no_book = fetch_order_book_safe(kalshi_exchange, pair.kalshi_no_outcome_id)

    opportunities: list[ArbOpportunity] = []
    now = datetime.now(timezone.utc)

    # Direction 1: Buy YES on Poly + Buy NO on Kalshi
    # If event happens: Poly YES pays $1, Kalshi NO pays $0 → net = $1 - cost
    # If event doesn't happen: Poly YES pays $0, Kalshi NO pays $1 → net = $1 - cost
    # Either way you get $1, so arb exists if total cost < $1
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


def scan_all_pairs(
    poly_exchange,
    kalshi_exchange,
    pairs: list[MatchedPair],
    threshold: float | None = None,
) -> list[ArbOpportunity]:
    """Scan all matched pairs and return sorted arb opportunities.

    Returns opportunities with net_edge above threshold, sorted by expected_profit descending.
    """
    if threshold is None:
        threshold = settings.edge_threshold

    all_opps: list[ArbOpportunity] = []

    for pair in pairs:
        try:
            opps = calculate_arb(poly_exchange, kalshi_exchange, pair)
            all_opps.extend(opps)
        except Exception:
            logger.exception("Error scanning pair: %s <-> %s", pair.poly_title, pair.kalshi_title)

    # Filter by threshold and sort by expected profit
    filtered = [o for o in all_opps if o.net_edge >= threshold]
    filtered.sort(key=lambda o: o.expected_profit, reverse=True)

    logger.info("Found %d opportunities above %.1f%% threshold", len(filtered), threshold * 100)
    return filtered
