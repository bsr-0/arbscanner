"""Tests for the arb calculation engine."""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from arbscanner.config import kalshi_fee, poly_fee
from arbscanner.engine import calculate_arb, scan_all_pairs
from arbscanner.models import ArbOpportunity, MatchedPair


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


def test_kalshi_fee_brackets():
    """Test Kalshi fee schedule returns correct bracket fees."""
    assert kalshi_fee(0.05) == 0.015  # 0-10c bracket
    assert kalshi_fee(0.15) == 0.025  # 10-25c bracket
    assert kalshi_fee(0.30) == 0.035  # 25-50c bracket
    assert kalshi_fee(0.60) == 0.035  # 50-75c bracket
    assert kalshi_fee(0.80) == 0.025  # 75-90c bracket
    assert kalshi_fee(0.95) == 0.015  # 90-100c bracket


def test_poly_fee():
    """Test Polymarket fee calculation."""
    assert abs(poly_fee(0.50) - 0.0005) < 1e-10
    assert abs(poly_fee(1.0) - 0.001) < 1e-10


def test_arb_positive_edge():
    """Test arb detection when a real opportunity exists."""
    pair = _make_pair()

    # Poly YES ask = 0.40, Kalshi NO ask = 0.45 → cost = 0.85 → gross edge = 0.15
    poly_yes_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)])
    kalshi_no_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 50)])

    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        def side_effect(exchange, outcome_id):
            return {
                "py1": poly_yes_book,
                "pn1": MockOrderBook(bids=[], asks=[]),
                "ky1": MockOrderBook(bids=[], asks=[]),
                "kn1": kalshi_no_book,
            }.get(outcome_id)

        mock_fetch.side_effect = side_effect
        opps = calculate_arb(MagicMock(), MagicMock(), pair)

    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction == "poly_yes_kalshi_no"
    assert opp.poly_price == 0.40
    assert opp.kalshi_price == 0.45
    assert opp.gross_edge > 0
    assert opp.net_edge > 0
    assert opp.available_size == 50  # min of 100 and 50


def test_arb_no_edge():
    """Test no arb when cost >= $1."""
    pair = _make_pair()

    # Poly YES ask = 0.55, Kalshi NO ask = 0.50 → cost = 1.05 → no arb
    poly_yes_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)])
    kalshi_no_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.50, 100)])

    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        def side_effect(exchange, outcome_id):
            return {
                "py1": poly_yes_book,
                "pn1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.55, 100)]),
                "ky1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.50, 100)]),
                "kn1": kalshi_no_book,
            }.get(outcome_id)

        mock_fetch.side_effect = side_effect
        opps = calculate_arb(MagicMock(), MagicMock(), pair)

    assert len(opps) == 0


def test_arb_empty_order_book():
    """Test graceful handling of empty order books."""
    pair = _make_pair()

    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        mock_fetch.return_value = MockOrderBook(bids=[], asks=[])
        opps = calculate_arb(MagicMock(), MagicMock(), pair)

    assert len(opps) == 0


def test_arb_null_order_book():
    """Test graceful handling when order book fetch returns None."""
    pair = _make_pair()

    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        mock_fetch.return_value = None
        opps = calculate_arb(MagicMock(), MagicMock(), pair)

    assert len(opps) == 0


def test_scan_all_pairs_filters_by_threshold():
    """Test that scan_all_pairs filters by edge threshold."""
    pair = _make_pair()

    # Create an arb with gross edge = 0.15 but check threshold filtering
    poly_yes_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.40, 100)])
    kalshi_no_book = MockOrderBook(bids=[], asks=[MockOrderLevel(0.45, 50)])

    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        def side_effect(exchange, outcome_id):
            return {
                "py1": poly_yes_book,
                "pn1": MockOrderBook(bids=[], asks=[]),
                "ky1": MockOrderBook(bids=[], asks=[]),
                "kn1": kalshi_no_book,
            }.get(outcome_id)

        mock_fetch.side_effect = side_effect

        # With low threshold, should find opportunity
        opps = scan_all_pairs(MagicMock(), MagicMock(), [pair], threshold=0.01)
        assert len(opps) >= 1

        # With impossibly high threshold, should find nothing
        opps = scan_all_pairs(MagicMock(), MagicMock(), [pair], threshold=0.99)
        assert len(opps) == 0


def test_both_directions():
    """Test that both arb directions are checked."""
    pair = _make_pair()

    # Both directions have arb
    with patch("arbscanner.engine.fetch_order_book_safe") as mock_fetch:
        def side_effect(exchange, outcome_id):
            return {
                "py1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 100)]),
                "pn1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 100)]),
                "ky1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 50)]),
                "kn1": MockOrderBook(bids=[], asks=[MockOrderLevel(0.30, 50)]),
            }.get(outcome_id)

        mock_fetch.side_effect = side_effect
        opps = calculate_arb(MagicMock(), MagicMock(), pair)

    assert len(opps) == 2
    directions = {o.direction for o in opps}
    assert "poly_yes_kalshi_no" in directions
    assert "poly_no_kalshi_yes" in directions
