"""Tests for the arb calculation engine."""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from arbscanner.config import kalshi_fee, poly_fee
from arbscanner.engine import calculate_arb, scan_all_pairs
from arbscanner.models import MatchedPair


@dataclass
class MockOrderLevel:
    price: float
    amount: float


@dataclass
class MockOrderBook:
    bids: list
    asks: list


def _make_pair(**kwargs) -> MatchedPair:
    defaults = dict(
        poly_market_id="poly_1",
        poly_title="Will X happen?",
        kalshi_market_id="kalshi_1",
        kalshi_title="KX-EVENT",
        confidence=0.95,
        source="embedding",
        matched_at="2026-04-10T00:00:00Z",
        poly_yes_outcome_id="py1",
        poly_no_outcome_id="pn1",
        kalshi_yes_outcome_id="ky1",
        kalshi_no_outcome_id="kn1",
    )
    defaults.update(kwargs)
    return MatchedPair(**defaults)


def _books(**kwargs) -> dict:
    """Build a books dict with defaults for any missing outcome."""
    empty = MockOrderBook(bids=[], asks=[])
    defaults = {"py1": empty, "pn1": empty, "ky1": empty, "kn1": empty}
    defaults.update(kwargs)
    return defaults


def test_kalshi_fee_brackets():
    """Test Kalshi fee schedule returns correct bracket fees."""
    assert kalshi_fee(0.05) == 0.015
    assert kalshi_fee(0.15) == 0.025
    assert kalshi_fee(0.30) == 0.035
    assert kalshi_fee(0.60) == 0.035
    assert kalshi_fee(0.80) == 0.025
    assert kalshi_fee(0.95) == 0.015


def test_poly_fee():
    """Test Polymarket fee calculation."""
    assert abs(poly_fee(0.50) - 0.0005) < 1e-10
    assert abs(poly_fee(1.0) - 0.001) < 1e-10


def test_arb_positive_edge():
    """Test arb detection when a real opportunity exists."""
    pair = _make_pair()

    # Poly YES ask = 0.40, Kalshi NO ask = 0.45 → cost = 0.85 → gross edge = 0.15
    books = _books(
        py1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)]),
        kn1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 50)]),
    )

    opps = calculate_arb(pair, books)

    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction == "poly_yes_kalshi_no"
    assert opp.poly_price == 0.40
    assert opp.kalshi_price == 0.45
    assert opp.gross_edge > 0
    assert opp.net_edge > 0
    assert opp.available_size == 50


def test_arb_no_edge():
    """Test no arb when cost >= $1."""
    pair = _make_pair()

    books = _books(
        py1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)]),
        pn1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)]),
        ky1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.50, 100)]),
        kn1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.50, 100)]),
    )

    opps = calculate_arb(pair, books)
    assert len(opps) == 0


def test_arb_empty_order_book():
    """Test graceful handling of empty order books."""
    pair = _make_pair()
    opps = calculate_arb(pair, _books())
    assert len(opps) == 0


def test_arb_null_order_book():
    """Test graceful handling when order book is None."""
    pair = _make_pair()
    books = {"py1": None, "pn1": None, "ky1": None, "kn1": None}
    opps = calculate_arb(pair, books)
    assert len(opps) == 0


def test_scan_all_pairs_filters_by_threshold():
    """Test that scan_all_pairs filters by edge threshold."""
    pair = _make_pair()

    poly_yes_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)])
    kalshi_no_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 50)])
    empty = MockOrderBook(bids=[], asks=[])

    def side_effect(exchange, outcome_id):
        return {
            "py1": poly_yes_book,
            "pn1": empty,
            "ky1": empty,
            "kn1": kalshi_no_book,
        }.get(outcome_id)

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        opps = scan_all_pairs(MagicMock(), MagicMock(), [pair], threshold=0.01, max_workers=2)
        assert len(opps) >= 1

        opps = scan_all_pairs(MagicMock(), MagicMock(), [pair], threshold=0.99, max_workers=2)
        assert len(opps) == 0


def test_scan_all_pairs_parallel_equals_sequential():
    """Parallel scanning should produce identical results regardless of worker count."""
    pairs = [
        _make_pair(
            poly_market_id=f"p{i}",
            poly_yes_outcome_id=f"py{i}",
            poly_no_outcome_id=f"pn{i}",
            kalshi_yes_outcome_id=f"ky{i}",
            kalshi_no_outcome_id=f"kn{i}",
        )
        for i in range(5)
    ]

    def side_effect(exchange, outcome_id):
        # Every py{i} has a 0.40 ask, every kn{i} has a 0.45 ask → arb exists
        if outcome_id.startswith("py"):
            return MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)])
        if outcome_id.startswith("kn"):
            return MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 50)])
        return MockOrderBook(bids=[], asks=[])

    with patch("arbscanner.engine.fetch_order_book_safe", side_effect=side_effect):
        result_1 = scan_all_pairs(MagicMock(), MagicMock(), pairs, threshold=0.01, max_workers=1)
        result_8 = scan_all_pairs(MagicMock(), MagicMock(), pairs, threshold=0.01, max_workers=8)

    # Both runs should find the same number of opportunities with the same edges
    assert len(result_1) == len(result_8) == 5
    edges_1 = sorted(o.net_edge for o in result_1)
    edges_8 = sorted(o.net_edge for o in result_8)
    assert edges_1 == edges_8


def test_scan_empty_pairs():
    """scan_all_pairs should handle an empty pair list."""
    opps = scan_all_pairs(MagicMock(), MagicMock(), [], max_workers=4)
    assert opps == []


def test_both_directions():
    """Test that both arb directions are detected."""
    pair = _make_pair()

    books = _books(
        py1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 100)]),
        pn1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 100)]),
        ky1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 50)]),
        kn1=MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 50)]),
    )

    opps = calculate_arb(pair, books)
    assert len(opps) == 2
    directions = {o.direction for o in opps}
    assert "poly_yes_kalshi_no" in directions
    assert "poly_no_kalshi_yes" in directions
