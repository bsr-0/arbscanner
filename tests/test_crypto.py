"""Tests for arbscanner.crypto — crypto fair value via Black-Scholes."""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from arbscanner.crypto import (
    CryptoClient,
    CryptoFairValue,
    CryptoPriceCache,
    CryptoThreshold,
    binary_call_fair_value,
    parse_crypto_ticker,
    _norm_cdf,
    _parse_kalshi_date,
)


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------


class TestParseCryptoTicker:
    def test_btc_max_pattern(self):
        result = parse_crypto_ticker("KXBTCMAX150-25-26APR30-149999.99")
        assert result is not None
        assert result.asset == "BTC"
        assert result.coingecko_id == "bitcoin"
        assert result.strike == 149999.99
        assert result.expiry == datetime(2026, 4, 30, tzinfo=timezone.utc)

    def test_btc_weekly(self):
        result = parse_crypto_ticker("KXBTCW-26MAY02-T99500")
        assert result is not None
        assert result.asset == "BTC"
        assert result.strike == 99500.0
        assert result.expiry == datetime(2026, 5, 2, tzinfo=timezone.utc)

    def test_btc_daily(self):
        result = parse_crypto_ticker("KXBTCD-26MAY07-T97000")
        assert result is not None
        assert result.asset == "BTC"
        assert result.strike == 97000.0

    def test_eth_daily(self):
        result = parse_crypto_ticker("KXETHD-26MAY07-T2500")
        assert result is not None
        assert result.asset == "ETH"
        assert result.coingecko_id == "ethereum"
        assert result.strike == 2500.0

    def test_btc_yearly(self):
        result = parse_crypto_ticker("KXBTCY-26-T100000")
        assert result is not None
        assert result.asset == "BTC"
        assert result.strike == 100000.0
        assert result.expiry == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_non_crypto_ticker(self):
        assert parse_crypto_ticker("KXFEDCUT-26JUN") is None

    def test_btc_vs_gold_not_threshold(self):
        assert parse_crypto_ticker("KXBTCVSGOLD-26") is None

    def test_unknown_asset(self):
        assert parse_crypto_ticker("KXZZZ-26MAY02-T100") is None
        assert parse_crypto_ticker("KXZZZD-26MAY07-T100") is None

    def test_various_months(self):
        for month, num in [("JAN", 1), ("JUN", 6), ("DEC", 12)]:
            result = parse_crypto_ticker(f"KXBTCW-26{month}15-T50000")
            assert result is not None
            assert result.expiry.month == num
            assert result.expiry.day == 15


class TestParseKalshiDate:
    def test_standard(self):
        dt = _parse_kalshi_date("26APR30")
        assert dt == datetime(2026, 4, 30, tzinfo=timezone.utc)

    def test_january(self):
        dt = _parse_kalshi_date("26JAN15")
        assert dt == datetime(2026, 1, 15, tzinfo=timezone.utc)

    def test_invalid(self):
        assert _parse_kalshi_date("") is None
        assert _parse_kalshi_date("abc") is None
        assert _parse_kalshi_date("26XXX01") is None


# ---------------------------------------------------------------------------
# Black-Scholes pricing
# ---------------------------------------------------------------------------


class TestBinaryCallFairValue:
    def test_atm_50_percent(self):
        # At-the-money with reasonable vol should be close to 50%
        # (slightly below due to risk-free drift vs vol drag)
        prob = binary_call_fair_value(
            spot=100000, strike=100000, time_to_expiry_years=0.5, volatility=0.60
        )
        assert 0.35 < prob < 0.65

    def test_deep_itm(self):
        # Spot way above strike -> probability near 1
        prob = binary_call_fair_value(
            spot=150000, strike=50000, time_to_expiry_years=0.1, volatility=0.60
        )
        assert prob > 0.95

    def test_deep_otm(self):
        # Spot way below strike -> probability near 0
        prob = binary_call_fair_value(
            spot=50000, strike=150000, time_to_expiry_years=0.1, volatility=0.60
        )
        assert prob < 0.05

    def test_expired_itm(self):
        prob = binary_call_fair_value(
            spot=110000, strike=100000, time_to_expiry_years=0, volatility=0.60
        )
        assert prob == 1.0

    def test_expired_otm(self):
        prob = binary_call_fair_value(
            spot=90000, strike=100000, time_to_expiry_years=0, volatility=0.60
        )
        assert prob == 0.0

    def test_zero_vol_itm(self):
        prob = binary_call_fair_value(
            spot=110000, strike=100000, time_to_expiry_years=1, volatility=0
        )
        assert prob == 1.0

    def test_zero_vol_otm(self):
        prob = binary_call_fair_value(
            spot=90000, strike=100000, time_to_expiry_years=1, volatility=0
        )
        assert prob == 0.0

    def test_higher_vol_wider_distribution(self):
        # Higher vol pushes ATM prob further from 0.5 toward 0.5
        # but for OTM, higher vol increases probability
        otm_low_vol = binary_call_fair_value(
            spot=80000, strike=100000, time_to_expiry_years=0.5, volatility=0.30
        )
        otm_high_vol = binary_call_fair_value(
            spot=80000, strike=100000, time_to_expiry_years=0.5, volatility=0.80
        )
        assert otm_high_vol > otm_low_vol

    def test_longer_time_more_uncertainty(self):
        # More time increases OTM probability
        short = binary_call_fair_value(
            spot=80000, strike=100000, time_to_expiry_years=0.01, volatility=0.60
        )
        long = binary_call_fair_value(
            spot=80000, strike=100000, time_to_expiry_years=1.0, volatility=0.60
        )
        assert long > short

    def test_invalid_inputs(self):
        assert binary_call_fair_value(0, 100, 1, 0.5) == 0.0
        assert binary_call_fair_value(100, 0, 1, 0.5) == 0.0
        assert binary_call_fair_value(-1, 100, 1, 0.5) == 0.0


class TestNormCdf:
    def test_center(self):
        assert abs(_norm_cdf(0) - 0.5) < 1e-10

    def test_positive(self):
        assert _norm_cdf(2) > 0.97

    def test_negative(self):
        assert _norm_cdf(-2) < 0.03

    def test_symmetry(self):
        assert abs(_norm_cdf(1) + _norm_cdf(-1) - 1.0) < 1e-10


# ---------------------------------------------------------------------------
# Price cache
# ---------------------------------------------------------------------------


class TestCryptoPriceCache:
    def test_put_and_get(self):
        cache = CryptoPriceCache(ttl_seconds=60)
        cache.put("bitcoin", 97000.0)
        assert cache.get("bitcoin") == 97000.0

    def test_miss(self):
        cache = CryptoPriceCache(ttl_seconds=60)
        assert cache.get("nonexistent") is None

    def test_expiry(self):
        cache = CryptoPriceCache(ttl_seconds=0)
        cache.put("bitcoin", 97000.0)
        time.sleep(0.01)
        assert cache.get("bitcoin") is None


# ---------------------------------------------------------------------------
# CryptoFairValue
# ---------------------------------------------------------------------------


class TestCryptoFairValue:
    def test_to_dict(self):
        fv = CryptoFairValue(
            implied_prob=0.6543,
            spot_price=97500.0,
            strike=100000.0,
            asset="BTC",
            volatility=0.60,
            days_to_expiry=30.0,
            fetched_at=datetime.now(timezone.utc),
        )
        d = fv.to_dict()
        assert d["implied_prob"] == 0.6543
        assert d["spot_price"] == 97500.0
        assert d["strike"] == 100000.0
        assert d["asset"] == "BTC"
        assert d["source"] == "coingecko_bs"


# ---------------------------------------------------------------------------
# Client (mocked)
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
    category: str = "Crypto"
    resolution_date: str = ""


class TestCryptoClientGetFairValue:
    def test_non_crypto_ticker(self):
        client = CryptoClient.__new__(CryptoClient)
        client._cache = CryptoPriceCache(ttl_seconds=60)
        client._limiter = MagicMock()
        client._failed = False

        pair = MockMatchedPair(kalshi_market_id="KXFEDCUT-26JUN")
        assert client.get_fair_value(pair) is None

    def test_with_cached_price(self):
        client = CryptoClient.__new__(CryptoClient)
        client._cache = CryptoPriceCache(ttl_seconds=60)
        client._limiter = MagicMock()
        client._failed = False

        # Pre-populate cache
        client._cache.put("bitcoin", 97000.0)

        pair = MockMatchedPair(
            kalshi_market_id="KXBTCMAX150-25-26DEC31-149999.99",
            resolution_date="2026-12-31T00:00:00+00:00",
        )
        fv = client.get_fair_value(pair)
        assert fv is not None
        assert fv.asset == "BTC"
        assert fv.spot_price == 97000.0
        assert fv.strike == 149999.99
        assert 0.0 < fv.implied_prob < 1.0
