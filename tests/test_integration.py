"""End-to-end integration tests for the arbscanner pipeline.

These tests exercise the full scan -> engine -> database pipeline without
touching any real exchange, network, or on-disk state outside of temporary
directories. The goal is to complement the unit tests in test_engine.py,
test_matcher.py, etc. by validating that the pieces compose correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from arbscanner.config import settings
from arbscanner.db import get_connection, log_opportunities
from arbscanner.engine import scan_all_pairs
from arbscanner.matcher import load_cache, save_cache
from arbscanner.models import MatchedPair, MatchedPairsCache


# ---------------------------------------------------------------------------
# Mock order book primitives (mirrors the pattern used in tests/test_engine.py)
# ---------------------------------------------------------------------------


@dataclass
class MockOrderLevel:
    """A single price level in a mock order book."""

    price: float
    amount: float


@dataclass
class MockOrderBook:
    """A mock order book with bids and asks lists of MockOrderLevel."""

    bids: list
    asks: list


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pair(index: int) -> MatchedPair:
    """Build a MatchedPair with distinct outcome IDs for the given index."""
    return MatchedPair(
        poly_market_id=f"poly_{index}",
        poly_title=f"Will event {index} happen?",
        kalshi_market_id=f"kalshi_{index}",
        kalshi_title=f"KX-EVENT-{index}",
        confidence=0.95,
        source="embedding",
        matched_at="2026-04-10T00:00:00Z",
        poly_yes_outcome_id=f"py{index}",
        poly_no_outcome_id=f"pn{index}",
        kalshi_yes_outcome_id=f"ky{index}",
        kalshi_no_outcome_id=f"kn{index}",
    )


@pytest.fixture
def three_pairs() -> list[MatchedPair]:
    """Three MatchedPair objects with disjoint outcome IDs."""
    return [_make_pair(i) for i in range(3)]


@pytest.fixture
def two_arb_books() -> dict:
    """Order books that yield exactly 2 arbs across the 3 pairs.

    * Pair 0: Poly YES ask 0.40 + Kalshi NO ask 0.45 -> big positive net edge.
    * Pair 1: Poly YES ask 0.42 + Kalshi NO ask 0.50 -> smaller positive edge.
    * Pair 2: Poly YES ask 0.55 + Kalshi NO ask 0.55 -> no edge (>= 1.00 cost).
    """
    empty = MockOrderBook(bids=[], asks=[])
    return {
        # Pair 0 — strong arb (~0.15 gross)
        "py0": MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)]),
        "pn0": empty,
        "ky0": empty,
        "kn0": MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 200)]),
        # Pair 1 — smaller but positive arb (~0.08 gross)
        "py1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.42, 50)]),
        "pn1": empty,
        "ky1": empty,
        "kn1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.50, 50)]),
        # Pair 2 — no arb
        "py2": MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)]),
        "pn2": empty,
        "ky2": empty,
        "kn2": MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)]),
    }


def _lookup_factory(books: dict):
    """Build a fetch_order_book_safe side effect that reads from a books dict."""

    def _side_effect(exchange, outcome_id):
        return books.get(outcome_id)

    return _side_effect


# ---------------------------------------------------------------------------
# 1. Full pipeline with mocked pmxt
# ---------------------------------------------------------------------------


def test_full_scan_pipeline_with_mocked_pmxt(three_pairs, two_arb_books):
    """Scanning 3 pairs with mocked fetches yields exactly 2 arbs sorted by profit."""
    side_effect = _lookup_factory(two_arb_books)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        results = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            three_pairs,
            threshold=0.01,
            max_workers=4,
        )

    assert len(results) == 2, f"expected 2 opportunities, got {len(results)}"

    # Both detected arbs should be on the Poly-YES / Kalshi-NO direction.
    directions = {opp.direction for opp in results}
    assert directions == {"poly_yes_kalshi_no"}

    # And exactly on pair_0 and pair_1 (pair_2 has no edge).
    market_ids = {opp.poly_market_id for opp in results}
    assert market_ids == {"poly_0", "poly_1"}
    assert "poly_2" not in market_ids

    # Must be sorted by expected_profit descending.
    profits = [opp.expected_profit for opp in results]
    assert profits == sorted(profits, reverse=True)
    assert results[0].expected_profit >= results[1].expected_profit


# ---------------------------------------------------------------------------
# 2. Scan results logged to SQLite
# ---------------------------------------------------------------------------


def test_scan_results_logged_to_database(three_pairs, two_arb_books):
    """Scanned opportunities can be persisted and queried back from SQLite."""
    side_effect = _lookup_factory(two_arb_books)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        results = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            three_pairs,
            threshold=0.01,
            max_workers=4,
        )

    assert len(results) == 2

    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "integration.db"
        conn = get_connection(db_path)
        try:
            log_opportunities(conn, results)

            rows = conn.execute(
                "SELECT poly_market_id, kalshi_market_id, direction, net_edge, "
                "expected_profit FROM opportunities ORDER BY expected_profit DESC"
            ).fetchall()
        finally:
            conn.close()

    assert len(rows) == 2
    row_pids = {r[0] for r in rows}
    assert row_pids == {"poly_0", "poly_1"}
    assert all(r[2] == "poly_yes_kalshi_no" for r in rows)
    assert all(r[3] > 0 for r in rows)  # net_edge
    # Descending sort preserved in DB dump.
    assert rows[0][4] >= rows[1][4]


# ---------------------------------------------------------------------------
# 3. Scanner gracefully handles flaky order book fetches
# ---------------------------------------------------------------------------


def test_scan_handles_flaky_order_books(three_pairs):
    """Exhausted-retry Nones for some outcomes do not break scanning of others."""
    empty = MockOrderBook(bids=[], asks=[])
    good_books = {
        # Pair 0 is fully alive -> should produce an arb.
        "py0": MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)]),
        "pn0": empty,
        "ky0": empty,
        "kn0": MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 100)]),
        # Pair 1 has a None on the Kalshi NO side -> direction 1 cannot fire.
        "py1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)]),
        "pn1": empty,
        "ky1": empty,
        "kn1": None,
        # Pair 2 has every side None -> completely dead.
        "py2": None,
        "pn2": None,
        "ky2": None,
        "kn2": None,
    }

    side_effect = _lookup_factory(good_books)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        results = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            three_pairs,
            threshold=0.01,
            max_workers=4,
        )

    # Only pair 0 should survive the flake — but the call must not raise.
    assert len(results) == 1
    assert results[0].poly_market_id == "poly_0"
    assert results[0].net_edge > 0


# ---------------------------------------------------------------------------
# 4. Matched-pairs cache roundtrip drives the scanner
# ---------------------------------------------------------------------------


def test_matched_pairs_cache_roundtrip_used_by_scanner(two_arb_books):
    """Pairs persisted via save_cache/load_cache still drive the scanner unchanged."""
    original_pairs = [_make_pair(i) for i in range(3)]
    original_cache = MatchedPairsCache(pairs=original_pairs)

    with TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "matched_pairs.json"
        save_cache(original_cache, path=cache_path)
        assert cache_path.exists(), "save_cache must materialize the file"

        reloaded = load_cache(path=cache_path)

    # Identity of the pair set must be preserved across the roundtrip.
    assert len(reloaded.pairs) == len(original_pairs)
    for original, loaded in zip(original_pairs, reloaded.pairs):
        assert loaded.poly_market_id == original.poly_market_id
        assert loaded.kalshi_market_id == original.kalshi_market_id
        assert loaded.poly_yes_outcome_id == original.poly_yes_outcome_id
        assert loaded.poly_no_outcome_id == original.poly_no_outcome_id
        assert loaded.kalshi_yes_outcome_id == original.kalshi_yes_outcome_id
        assert loaded.kalshi_no_outcome_id == original.kalshi_no_outcome_id

    # Now feed the reloaded pairs into the scanner and confirm we still get
    # the same 2-arb result as test_full_scan_pipeline_with_mocked_pmxt.
    side_effect = _lookup_factory(two_arb_books)
    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        results = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            reloaded.pairs,
            threshold=0.01,
            max_workers=4,
        )

    assert len(results) == 2
    assert {opp.poly_market_id for opp in results} == {"poly_0", "poly_1"}


# ---------------------------------------------------------------------------
# 5. Parallel scan matches serial scan bit-for-bit (on the fields we care about)
# ---------------------------------------------------------------------------


def test_parallel_scan_produces_same_results_as_serial(three_pairs, two_arb_books):
    """max_workers=1 and max_workers=8 yield identical opportunities."""
    side_effect = _lookup_factory(two_arb_books)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        serial = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            three_pairs,
            threshold=0.01,
            max_workers=1,
        )
        parallel = scan_all_pairs(
            MagicMock(),
            MagicMock(),
            three_pairs,
            threshold=0.01,
            max_workers=8,
        )

    def _key(opp):
        return (opp.poly_market_id, opp.direction)

    serial_sorted = sorted(serial, key=_key)
    parallel_sorted = sorted(parallel, key=_key)

    assert len(serial_sorted) == len(parallel_sorted) == 2

    for s_opp, p_opp in zip(serial_sorted, parallel_sorted):
        assert s_opp.poly_market_id == p_opp.poly_market_id
        assert s_opp.kalshi_market_id == p_opp.kalshi_market_id
        assert s_opp.direction == p_opp.direction
        assert s_opp.poly_price == p_opp.poly_price
        assert s_opp.kalshi_price == p_opp.kalshi_price
        assert s_opp.gross_edge == p_opp.gross_edge
        assert s_opp.net_edge == p_opp.net_edge
        assert s_opp.available_size == p_opp.available_size
        assert s_opp.expected_profit == p_opp.expected_profit


# ---------------------------------------------------------------------------
# 6. Alert deduper suppresses repeat alerts inside the TTL window
# ---------------------------------------------------------------------------


def test_dedup_filter_preserves_first_alert_and_suppresses_repeat():
    """First alert fires; an identical repeat inside the TTL is suppressed."""
    alerts_dedup = pytest.importorskip("arbscanner.alerts_dedup")

    from datetime import datetime

    from arbscanner.models import ArbOpportunity

    opp = ArbOpportunity(
        poly_title="Will X happen?",
        kalshi_title="KX-EVENT",
        poly_market_id="poly_dedup",
        kalshi_market_id="kalshi_dedup",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.12,
        available_size=100.0,
        expected_profit=12.0,
        timestamp=datetime.now(),
    )
    identical = ArbOpportunity(**{**opp.__dict__})

    # Long TTL so the second call is definitely inside the window.
    deduper = alerts_dedup.AlertDeduper(
        ttl_seconds=300.0, max_entries=100, edge_delta=0.005
    )

    first = deduper.should_alert(opp)
    second = deduper.should_alert(identical)

    assert first is True, "first alert must fire"
    assert second is False, "identical repeat must be suppressed inside TTL"

    # And the filter() helper must agree with should_alert().
    fresh_deduper = alerts_dedup.AlertDeduper(
        ttl_seconds=300.0, max_entries=100, edge_delta=0.005
    )
    fired = fresh_deduper.filter([opp, identical])
    assert len(fired) == 1
    assert fired[0] is opp


# ---------------------------------------------------------------------------
# 7. The exchanges module's shared rate limiter is the right type and rate
# ---------------------------------------------------------------------------


def test_rate_limiter_protects_exchanges_module():
    """The exchanges._rate_limiter singleton is a RateLimiter at the configured rate."""
    from arbscanner import exchanges
    from arbscanner.utils import RateLimiter

    limiter = exchanges._rate_limiter
    assert isinstance(limiter, RateLimiter), (
        f"expected RateLimiter, got {type(limiter).__name__}"
    )

    # RateLimiter stores _interval = 1 / calls_per_sec; derive calls_per_sec
    # and compare back to the settings value it was constructed from.
    assert limiter._interval > 0
    derived_calls_per_sec = 1.0 / limiter._interval
    assert derived_calls_per_sec == pytest.approx(settings.rate_limit_per_sec)
