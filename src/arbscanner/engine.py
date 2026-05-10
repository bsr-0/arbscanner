"""Arb calculation engine — detect cross-platform arbitrage opportunities."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from arbscanner.calibration import CalibrationContext, get_calibration_context
from arbscanner.config import kalshi_fee, poly_fee, settings
from arbscanner.exchanges import fetch_order_book_safe
from arbscanner.metrics import (
    opportunities_found_total,
    order_book_fetch_failures_total,
    order_book_fetches_total,
    scan_cycle_seconds,
    scan_cycles_total,
    timing_block,
)
from arbscanner.models import ArbOpportunity, MatchedPair

logger = logging.getLogger(__name__)


def _calibration_for(
    pair: MatchedPair,
    net_edge: float,
    fair_value: object | None = None,
) -> dict | None:
    """Compute calibration context for an opportunity on this pair.

    Returns a dict (as emitted by CalibrationContext.__dict__) or None if the
    pair lacks the metadata needed to score it. Errors in the calibration
    lookup are swallowed and logged — we never want a bad calibration lookup
    to block arb detection.

    If *fair_value* (an ``odds.FairValue`` instance) is provided, its
    serialized form is attached under the ``fair_value`` key.
    """
    if not pair.category and not pair.resolution_date:
        return None
    resolution_date: datetime | None = None
    if pair.resolution_date:
        try:
            resolution_date = datetime.fromisoformat(
                pair.resolution_date.replace("Z", "+00:00")
            )
        except ValueError:
            logger.debug(
                "Unable to parse resolution_date %r for pair %s",
                pair.resolution_date,
                pair.poly_market_id,
            )
    try:
        ctx: CalibrationContext = get_calibration_context(
            pair.category or None, resolution_date, net_edge
        )
    except Exception:
        logger.exception("Calibration lookup failed for pair %s", pair.poly_market_id)
        return None
    result = {
        "category": ctx.category,
        "days_to_resolution": ctx.days_to_resolution,
        "time_bucket": ctx.time_bucket,
        "avg_mispricing": ctx.avg_mispricing,
        "edge_likely_real": ctx.edge_likely_real,
        "confidence_note": ctx.confidence_note,
    }
    if fair_value is not None:
        result["fair_value"] = fair_value.to_dict()
    return result


def calculate_arb(
    pair: MatchedPair,
    books: dict[str, object | None],
    fair_value: object | None = None,
) -> list[ArbOpportunity]:
    """Calculate arb opportunities for a single matched pair from pre-fetched books.

    Args:
        pair: The matched market pair.
        books: Dict mapping outcome_id -> OrderBook (or None if fetch failed).
        fair_value: Optional ``odds.FairValue`` for sportsbook consensus enrichment.

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
                        category=pair.category,
                        resolution_date=pair.resolution_date,
                        calibration=_calibration_for(pair, net, fair_value),
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
                        category=pair.category,
                        resolution_date=pair.resolution_date,
                        calibration=_calibration_for(pair, net, fair_value),
                    )
                )

    return opportunities


def _best_ask(order_book) -> dict | None:
    """Extract the best (lowest) ask from an order book."""
    if order_book is None or not getattr(order_book, "asks", None):
        return None
    best = order_book.asks[0]
    return {"price": best.price, "amount": best.size}


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


def _scan_chunked(
    poly_exchange,
    kalshi_exchange,
    pairs: list[MatchedPair],
    threshold: float,
    max_workers: int,
    chunk_size: int,
    on_opportunities,
) -> list[ArbOpportunity]:
    """Process pairs in chunks, calling on_opportunities after each batch."""
    all_opps: list[ArbOpportunity] = []
    total = len(pairs)
    for i in range(0, total, chunk_size):
        chunk = pairs[i : i + chunk_size]
        logger.info(
            "Scanning chunk %d-%d / %d", i + 1, min(i + chunk_size, total), total
        )
        chunk_opps = scan_all_pairs(
            poly_exchange, kalshi_exchange, chunk,
            threshold=threshold, max_workers=max_workers, chunk_size=0,
        )
        all_opps.extend(chunk_opps)
        if on_opportunities and chunk_opps:
            on_opportunities(chunk_opps)
    return all_opps


def scan_all_pairs(
    poly_exchange,
    kalshi_exchange,
    pairs: list[MatchedPair],
    threshold: float | None = None,
    max_workers: int | None = None,
    chunk_size: int = 0,
    on_opportunities=None,
) -> list[ArbOpportunity]:
    """Scan all matched pairs and return sorted arb opportunities.

    Fetches all order books in parallel using a ThreadPoolExecutor, then
    computes arbs locally from the pre-fetched books. Rate limiting is handled
    by the shared limiter in arbscanner.exchanges.

    Args:
        chunk_size: If > 0, process pairs in batches of this size and call
            ``on_opportunities`` after each batch. Useful for CI where partial
            results should be persisted before a timeout kills the process.
        on_opportunities: Callback invoked with each batch of found opportunities
            when ``chunk_size > 0``. Called as ``on_opportunities(opps)``.

    Returns opportunities with net_edge above threshold, sorted by expected_profit descending.
    """
    if threshold is None:
        threshold = settings.edge_threshold
    if max_workers is None:
        max_workers = settings.max_workers

    if not pairs:
        return []

    if chunk_size > 0:
        return _scan_chunked(
            poly_exchange, kalshi_exchange, pairs, threshold, max_workers,
            chunk_size, on_opportunities,
        )

    with timing_block(scan_cycle_seconds):
        # Phase 1: parallel fetch of all order books
        books = _fetch_all_books(poly_exchange, kalshi_exchange, pairs, max_workers)

        # Record fetch metrics from the books dict
        for outcome_id, book in books.items():
            order_book_fetches_total.inc()
            if book is None:
                order_book_fetch_failures_total.inc()

        # Phase 1.5: pre-fetch sportsbook fair values (optional, non-blocking)
        fair_values: dict[str, object] = {}
        try:
            from arbscanner.odds import get_odds_client

            odds_client = get_odds_client()
            if odds_client is not None:
                for pair in pairs:
                    if pair.category and pair.category.lower() in (
                        "sports", "sport",
                    ):
                        key = f"{pair.poly_market_id}::{pair.kalshi_market_id}"
                        fv = odds_client.get_fair_value(pair)
                        if fv is not None:
                            fair_values[key] = fv
                if fair_values:
                    logger.info(
                        "Enriched %d pairs with sportsbook fair values",
                        len(fair_values),
                    )
        except Exception:
            logger.debug("Odds API enrichment unavailable", exc_info=True)

        # Phase 1.5b: crypto fair values via CoinGecko + Black-Scholes
        try:
            from arbscanner.crypto import get_crypto_client

            crypto_client = get_crypto_client()
            for pair in pairs:
                if pair.category and pair.category.lower() in (
                    "crypto", "bitcoin", "ethereum",
                ):
                    key = f"{pair.poly_market_id}::{pair.kalshi_market_id}"
                    if key not in fair_values:
                        fv = crypto_client.get_fair_value(pair)
                        if fv is not None:
                            fair_values[key] = fv
            crypto_count = sum(
                1 for k, v in fair_values.items()
                if hasattr(v, "asset")  # CryptoFairValue has .asset
            )
            if crypto_count:
                logger.info(
                    "Enriched %d pairs with crypto fair values",
                    crypto_count,
                )
        except Exception:
            logger.debug("Crypto fair value enrichment unavailable", exc_info=True)

        # Phase 1.5c: polling fair values for approval rating markets
        try:
            from arbscanner.polling import get_polling_client

            polling_client = get_polling_client()
            polling_count = 0
            for pair in pairs:
                key = f"{pair.poly_market_id}::{pair.kalshi_market_id}"
                if key not in fair_values:
                    kid = pair.kalshi_market_id
                    if kid.startswith(("KXAPRPOTUS", "KXTRUMPAPPROVAL")):
                        fv = polling_client.get_fair_value(pair)
                        if fv is not None:
                            fair_values[key] = fv
                            polling_count += 1
            if polling_count:
                logger.info(
                    "Enriched %d pairs with polling fair values",
                    polling_count,
                )
        except Exception:
            logger.debug("Polling fair value enrichment unavailable", exc_info=True)

        # Phase 2: compute arbs locally (fast, no I/O)
        all_opps: list[ArbOpportunity] = []
        for pair in pairs:
            try:
                key = f"{pair.poly_market_id}::{pair.kalshi_market_id}"
                fv = fair_values.get(key)
                opps = calculate_arb(pair, books, fair_value=fv)
                all_opps.extend(opps)
            except Exception:
                logger.exception(
                    "Error scanning pair: %s <-> %s", pair.poly_title, pair.kalshi_title
                )

        # Filter by threshold and sort by expected profit
        filtered = [o for o in all_opps if o.net_edge >= threshold]
        filtered.sort(key=lambda o: o.expected_profit, reverse=True)

        # Metrics
        scan_cycles_total.inc()
        for opp in filtered:
            opportunities_found_total.inc(direction=opp.direction)

        logger.info(
            "Found %d opportunities above %.1f%% threshold",
            len(filtered),
            threshold * 100,
        )
        return filtered
