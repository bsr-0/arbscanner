"""Shared test fixtures and factories for mocking pmxt exchanges and building test data.

This module centralizes the mock objects and factory helpers that were previously
duplicated across ``tests/test_engine.py``, ``tests/test_web.py``, and
``tests/test_alerts.py`` (e.g. ``MockOrderLevel``, ``MockOrderBook``, ``_make_pair``,
``_make_opp``). Tests should import from here instead of redefining their own copies.

Note: the sibling ``tests/fixtures/__init__.py`` file is created alongside this
module (by this same change) so that ``tests.fixtures`` is importable as a package.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from arbscanner.models import ArbOpportunity, MatchedPair


# ---------------------------------------------------------------------------
# Order book mocks
# ---------------------------------------------------------------------------


@dataclass
class MockOrderLevel:
    """A single level in a mock order book (price + size)."""

    price: float
    amount: float


@dataclass
class MockOrderBook:
    """A minimal order book stand-in compatible with the engine's expectations."""

    bids: list[MockOrderLevel] = field(default_factory=list)
    asks: list[MockOrderLevel] = field(default_factory=list)
    timestamp: int | None = None


def make_order_book(
    yes_price: float = 0.5,
    yes_size: float = 100,
    no_price: float = 0.5,
    no_size: float = 100,
) -> MockOrderBook:
    """Build a simple order book with a single ask level.

    The ``yes_*`` / ``no_*`` parameter names are kept for call-site clarity; this
    helper produces one book with one ask at ``yes_price`` / ``yes_size``. Tests
    that need both sides of a market should call this helper twice (once per
    outcome) and assemble a ``books`` dict themselves.
    """

    del no_price, no_size  # reserved for future two-sided helpers
    return MockOrderBook(
        bids=[],
        asks=[MockOrderLevel(price=yes_price, amount=yes_size)],
    )


def empty_order_book() -> MockOrderBook:
    """Return a ``MockOrderBook`` with empty bids and asks."""

    return MockOrderBook(bids=[], asks=[])


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------


def make_matched_pair(**overrides: Any) -> MatchedPair:
    """Factory returning a ``MatchedPair`` with sensible defaults.

    Pass ``**overrides`` to customize any field (e.g.
    ``make_matched_pair(poly_market_id="p5", confidence=0.7)``).
    """

    pair_id = overrides.pop("id", "1")
    defaults: dict[str, Any] = dict(
        poly_market_id=f"poly_{pair_id}",
        poly_title="Will X happen?",
        kalshi_market_id=f"kalshi_{pair_id}",
        kalshi_title="KX-EVENT",
        confidence=0.95,
        source="embedding",
        matched_at="2026-04-10T00:00:00Z",
        poly_yes_outcome_id="py1",
        poly_no_outcome_id="pn1",
        kalshi_yes_outcome_id="ky1",
        kalshi_no_outcome_id="kn1",
    )
    defaults.update(overrides)
    return MatchedPair(**defaults)


def make_arb_opportunity(**overrides: Any) -> ArbOpportunity:
    """Factory returning an ``ArbOpportunity`` with sensible defaults."""

    defaults: dict[str, Any] = dict(
        poly_title="Test Market",
        kalshi_title="KX-TEST",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=50,
        expected_profit=5.0,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return ArbOpportunity(**defaults)


# ---------------------------------------------------------------------------
# Exchange mocks
# ---------------------------------------------------------------------------


def make_mock_exchange() -> MagicMock:
    """Return a ``MagicMock`` simulating a pmxt exchange (Polymarket or Kalshi).

    The returned mock has ``fetch_markets_paginated`` and ``fetch_order_book``
    configured as attributes so tests can set ``.return_value`` or
    ``.side_effect`` on them without additional setup.
    """

    exchange = MagicMock(name="MockExchange")
    exchange.fetch_markets_paginated = MagicMock(return_value=[])
    exchange.fetch_order_book = MagicMock(return_value=empty_order_book())
    return exchange


class MockExchange:
    """Simple exchange stand-in backed by a dict of ``outcome_id -> MockOrderBook``.

    Unlike ``make_mock_exchange`` (which returns a ``MagicMock``), this class is a
    concrete object whose ``fetch_order_book`` method looks up the requested
    outcome and raises ``KeyError`` if it is not present.
    """

    def __init__(self, books: dict[str, MockOrderBook] | None = None) -> None:
        self.books: dict[str, MockOrderBook] = dict(books or {})

    def fetch_order_book(self, outcome_id: str) -> MockOrderBook:
        """Return the book for ``outcome_id`` or raise ``KeyError``."""

        return self.books[outcome_id]


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


@contextmanager
def patch_fetch_order_book(
    books_by_outcome: dict[str, MockOrderBook],
) -> Iterator[MagicMock]:
    """Patch ``arbscanner.engine.fetch_order_book_safe`` to read from a dict.

    For any outcome in ``books_by_outcome`` the patched function returns the
    corresponding ``MockOrderBook``; for any other outcome it returns ``None``,
    matching the real ``fetch_order_book_safe`` behavior on failure.

    Usage::

        with patch_fetch_order_book({"py1": book_a, "kn1": book_b}):
            opps = scan_all_pairs(...)
    """

    def _side_effect(exchange: Any, outcome_id: str) -> MockOrderBook | None:
        del exchange  # unused — lookup keyed by outcome only
        return books_by_outcome.get(outcome_id)

    with patch(
        "arbscanner.engine.fetch_order_book_safe",
        side_effect=_side_effect,
    ) as mocked:
        yield mocked
