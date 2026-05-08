"""Tests for arbscanner.polling — approval rating fair value."""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from arbscanner.polling import (
    ApprovalCache,
    ApprovalThreshold,
    PollingClient,
    PollingFairValue,
    approval_fair_value_ever_above,
    approval_fair_value_ever_below,
    approval_fair_value_point,
    parse_approval_ticker,
    _parse_kalshi_date,
)


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------


class TestParseApprovalTicker:
    def test_aprpotus(self):
        result = parse_approval_ticker("KXAPRPOTUS-26APR17-41.1")
        assert result is not None
        assert result.threshold == 41.1
        assert result.direction == "above"
        assert result.expiry == datetime(2026, 4, 17, tzinfo=timezone.utc)

    def test_approval_below(self):
        result = parse_approval_ticker("KXTRUMPAPPROVALBELOW-26DEC31-41")
        assert result is not None
        assert result.threshold == 41.0
        assert result.direction == "below"
        assert result.expiry == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_approval_year(self):
        result = parse_approval_ticker("KXTRUMPAPPROVALYEAR-26DEC31-43")
        assert result is not None
        assert result.threshold == 43.0
        assert result.direction == "above"

    def test_non_approval_ticker(self):
        assert parse_approval_ticker("KXBTCMAX150-25-26APR30-149999.99") is None
        assert parse_approval_ticker("KXFEDCUT-26JUN") is None


class TestParseKalshiDate:
    def test_standard(self):
        dt = _parse_kalshi_date("26APR17")
        assert dt == datetime(2026, 4, 17, tzinfo=timezone.utc)

    def test_invalid(self):
        assert _parse_kalshi_date("") is None
        assert _parse_kalshi_date("26XXX01") is None


# ---------------------------------------------------------------------------
# Fair value math — point-in-time
# ---------------------------------------------------------------------------


class TestApprovalFairValuePoint:
    def test_at_threshold(self):
        # Current = threshold -> ~50%
        prob = approval_fair_value_point(42.0, 42.0, days_to_expiry=30)
        assert 0.45 < prob < 0.55

    def test_well_above(self):
        # Current 45%, threshold 40%, 7 days -> very likely above
        prob = approval_fair_value_point(45.0, 40.0, days_to_expiry=7)
        assert prob > 0.90

    def test_well_below(self):
        # Current 38%, threshold 42%, 7 days -> unlikely above
        prob = approval_fair_value_point(38.0, 42.0, days_to_expiry=7)
        assert prob < 0.10

    def test_expired_above(self):
        prob = approval_fair_value_point(43.0, 42.0, days_to_expiry=0)
        assert prob == 1.0

    def test_expired_below(self):
        prob = approval_fair_value_point(41.0, 42.0, days_to_expiry=0)
        assert prob == 0.0

    def test_more_time_more_uncertainty(self):
        # More time -> probability reverts toward 50%
        short = approval_fair_value_point(45.0, 42.0, days_to_expiry=7)
        long = approval_fair_value_point(45.0, 42.0, days_to_expiry=365)
        assert short > long  # less time = more confident it stays above


# ---------------------------------------------------------------------------
# Fair value math — barrier (ever below)
# ---------------------------------------------------------------------------


class TestApprovalFairValueEverBelow:
    def test_already_below(self):
        prob = approval_fair_value_ever_below(40.0, 42.0, days_to_expiry=30)
        assert prob == 1.0

    def test_far_above(self):
        # Current 50%, threshold 30%, 30 days -> very unlikely to touch 30
        prob = approval_fair_value_ever_below(50.0, 30.0, days_to_expiry=30)
        assert prob < 0.01

    def test_close_above(self):
        # Current 42%, threshold 41%, 180 days -> decent chance
        prob = approval_fair_value_ever_below(42.0, 41.0, days_to_expiry=180)
        assert 0.3 < prob < 0.99

    def test_more_time_more_likely(self):
        short = approval_fair_value_ever_below(44.0, 40.0, days_to_expiry=7)
        long = approval_fair_value_ever_below(44.0, 40.0, days_to_expiry=365)
        assert long > short

    def test_expired(self):
        prob = approval_fair_value_ever_below(44.0, 40.0, days_to_expiry=0)
        assert prob == 0.0


# ---------------------------------------------------------------------------
# Fair value math — barrier (ever above)
# ---------------------------------------------------------------------------


class TestApprovalFairValueEverAbove:
    def test_already_above(self):
        prob = approval_fair_value_ever_above(45.0, 42.0, days_to_expiry=30)
        assert prob == 1.0

    def test_far_below(self):
        prob = approval_fair_value_ever_above(30.0, 50.0, days_to_expiry=30)
        assert prob < 0.01

    def test_close_below(self):
        prob = approval_fair_value_ever_above(42.0, 43.0, days_to_expiry=180)
        assert 0.3 < prob < 0.99

    def test_more_time_more_likely(self):
        short = approval_fair_value_ever_above(40.0, 44.0, days_to_expiry=7)
        long = approval_fair_value_ever_above(40.0, 44.0, days_to_expiry=365)
        assert long > short


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestApprovalCache:
    def test_put_and_get(self):
        cache = ApprovalCache(ttl_seconds=60)
        cache.put(42.3)
        assert cache.get() == 42.3

    def test_miss(self):
        cache = ApprovalCache(ttl_seconds=60)
        assert cache.get() is None

    def test_expiry(self):
        cache = ApprovalCache(ttl_seconds=0)
        cache.put(42.3)
        time.sleep(0.01)
        assert cache.get() is None


# ---------------------------------------------------------------------------
# FairValue model
# ---------------------------------------------------------------------------


class TestPollingFairValue:
    def test_to_dict(self):
        fv = PollingFairValue(
            implied_prob=0.65,
            current_approval=42.3,
            threshold=41.0,
            direction="above",
            days_to_expiry=30.0,
            daily_vol=0.20,
            fetched_at=datetime.now(timezone.utc),
        )
        d = fv.to_dict()
        assert d["implied_prob"] == 0.65
        assert d["current_approval"] == 42.3
        assert d["threshold"] == 41.0
        assert d["source"] == "538_approval"


# ---------------------------------------------------------------------------
# Client (unit tests with manual approval)
# ---------------------------------------------------------------------------


@dataclass
class MockMatchedPair:
    poly_market_id: str = "123"
    poly_title: str = ""
    kalshi_market_id: str = ""
    kalshi_title: str = ""
    confidence: float = 0.95
    source: str = "embedding"
    matched_at: str = ""
    poly_yes_outcome_id: str = ""
    poly_no_outcome_id: str = ""
    kalshi_yes_outcome_id: str = ""
    kalshi_no_outcome_id: str = ""
    category: str = "Politics"
    resolution_date: str = ""


class TestPollingClientGetFairValue:
    def test_non_approval_ticker(self):
        client = PollingClient(cache_ttl=60)
        client.set_approval(42.0)
        pair = MockMatchedPair(kalshi_market_id="KXFEDCUT-26JUN")
        assert client.get_fair_value(pair) is None

    def test_aprpotus_with_cached_approval(self):
        client = PollingClient(cache_ttl=60)
        client.set_approval(42.0)
        pair = MockMatchedPair(
            kalshi_market_id="KXAPRPOTUS-26DEC31-41.0",
            resolution_date="2026-12-31T00:00:00+00:00",
        )
        fv = client.get_fair_value(pair)
        assert fv is not None
        assert fv.current_approval == 42.0
        assert fv.threshold == 41.0
        assert fv.implied_prob > 0.5  # above threshold, should be >50%

    def test_approval_below_market(self):
        client = PollingClient(cache_ttl=60)
        client.set_approval(42.0)
        pair = MockMatchedPair(
            kalshi_market_id="KXTRUMPAPPROVALBELOW-26DEC31-40",
            resolution_date="2026-12-31T00:00:00+00:00",
        )
        fv = client.get_fair_value(pair)
        assert fv is not None
        assert fv.direction == "below"
        assert 0.0 < fv.implied_prob < 1.0

    def test_approval_year_market(self):
        client = PollingClient(cache_ttl=60)
        client.set_approval(42.0)
        pair = MockMatchedPair(
            kalshi_market_id="KXTRUMPAPPROVALYEAR-26DEC31-50",
            resolution_date="2026-12-31T00:00:00+00:00",
        )
        fv = client.get_fair_value(pair)
        assert fv is not None
        assert fv.direction == "above"
        # 42% current, 50% target — hard to reach
        assert fv.implied_prob < 0.5
