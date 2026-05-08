"""Crypto fair value via spot prices and binary option pricing.

Fetches real-time crypto prices from CoinGecko (free, no auth) and
computes fair value for threshold markets ("Will BTC be above $X by
date Y?") using Black-Scholes binary option math.

Fully optional — if no crypto pairs exist or CoinGecko is unreachable,
the rest of the system proceeds unchanged.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from arbscanner.models import MatchedPair
from arbscanner.utils import RateLimiter, retry_with_backoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kalshi ticker parsing
# ---------------------------------------------------------------------------

# Kalshi crypto threshold tickers follow patterns like:
#   KXBTCMAX150-25-26APR30-149999.99
#   KXBTCW-26MAY02-T99500   (weekly BTC above)
#   KXBTCD-26MAY07-T97000   (daily BTC above)
#   KXETHD-26MAY07-T2500    (daily ETH above)
#
# We extract: asset (BTC/ETH/SOL), strike price, and expiry date.

# Maps Kalshi asset prefixes to CoinGecko IDs
ASSET_PREFIX_TO_COINGECKO: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
    "DOT": "polkadot",
    "LINK": "chainlink",
}

# Regex patterns for Kalshi crypto tickers
# Pattern 1: KXBTCMAX150-25-26APR30-149999.99 (threshold with explicit strike)
_PATTERN_MAX = re.compile(
    r"^KX([A-Z]+?)MAX\d+-\d+-(\d{2}[A-Z]{3}\d{2})-(\d+(?:\.\d+)?)$"
)
# Pattern 2: KXBTCW-26MAY02-T99500 or KXBTCD-26MAY07-T97000 (weekly/daily with T-prefix strike)
_PATTERN_PERIODIC = re.compile(
    r"^KX([A-Z]+?)[WD]-(\d{2}[A-Z]{3}\d{2})-T(\d+(?:\.\d+)?)$"
)
# Pattern 3: KXBTCY-26-T100000 (yearly)
_PATTERN_YEARLY = re.compile(
    r"^KX([A-Z]+?)Y-(\d{2})-T(\d+(?:\.\d+)?)$"
)

# Month abbreviation to number
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class CryptoThreshold:
    """Parsed components of a Kalshi crypto threshold market."""

    asset: str  # "BTC", "ETH", etc.
    coingecko_id: str  # "bitcoin", "ethereum", etc.
    strike: float  # strike price in USD
    expiry: datetime | None  # expiry date (UTC)


def parse_crypto_ticker(kalshi_market_id: str) -> CryptoThreshold | None:
    """Parse a Kalshi crypto threshold ticker into components.

    Returns None if the ticker is not a recognized crypto threshold format.
    """
    # Try each pattern
    for pattern in (_PATTERN_MAX, _PATTERN_PERIODIC):
        m = pattern.match(kalshi_market_id)
        if m:
            asset = m.group(1)
            date_str = m.group(2)  # e.g. "26APR30"
            strike = float(m.group(3))
            cg_id = ASSET_PREFIX_TO_COINGECKO.get(asset)
            if cg_id is None:
                return None
            expiry = _parse_kalshi_date(date_str)
            return CryptoThreshold(
                asset=asset, coingecko_id=cg_id, strike=strike, expiry=expiry,
            )

    # Yearly pattern
    m = _PATTERN_YEARLY.match(kalshi_market_id)
    if m:
        asset = m.group(1)
        year_str = m.group(2)  # e.g. "26"
        strike = float(m.group(3))
        cg_id = ASSET_PREFIX_TO_COINGECKO.get(asset)
        if cg_id is None:
            return None
        try:
            year = 2000 + int(year_str)
            expiry = datetime(year, 12, 31, tzinfo=timezone.utc)
        except ValueError:
            expiry = None
        return CryptoThreshold(
            asset=asset, coingecko_id=cg_id, strike=strike, expiry=expiry,
        )

    return None


def _parse_kalshi_date(date_str: str) -> datetime | None:
    """Parse '26APR30' -> datetime(2026, 4, 30, UTC)."""
    if len(date_str) < 5:
        return None
    try:
        year = 2000 + int(date_str[:2])
        month_abbr = date_str[2:5]
        day = int(date_str[5:])
        month = _MONTHS.get(month_abbr)
        if month is None:
            return None
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Black-Scholes binary option pricing
# ---------------------------------------------------------------------------

# Default annualized volatilities (can be refined with historical data)
DEFAULT_VOLATILITY: dict[str, float] = {
    "bitcoin": 0.60,
    "ethereum": 0.70,
    "solana": 0.80,
    "dogecoin": 0.90,
    "ripple": 0.75,
    "cardano": 0.80,
    "avalanche-2": 0.80,
    "matic-network": 0.80,
    "polkadot": 0.80,
    "chainlink": 0.80,
}


def binary_call_fair_value(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float = 0.045,
) -> float:
    """Fair value of a binary call option (pays $1 if spot > strike at expiry).

    Uses the Black-Scholes formula: P = N(d2)
    where d2 = (ln(S/K) + (r - σ²/2) * T) / (σ * √T)

    Returns probability in [0, 1].
    """
    if spot <= 0 or strike <= 0:
        return 0.0
    if time_to_expiry_years <= 0:
        # Already expired: worth 1 if spot > strike, else 0
        return 1.0 if spot > strike else 0.0
    if volatility <= 0:
        # Zero vol: deterministic
        return 1.0 if spot > strike else 0.0

    d2 = (
        math.log(spot / strike) + (risk_free_rate - 0.5 * volatility**2) * time_to_expiry_years
    ) / (volatility * math.sqrt(time_to_expiry_years))

    return _norm_cdf(d2)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# CoinGecko client
# ---------------------------------------------------------------------------

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"


@dataclass
class CryptoFairValue:
    """Fair value for a crypto threshold market."""

    implied_prob: float  # Black-Scholes fair probability
    spot_price: float  # current spot price
    strike: float
    asset: str  # "BTC", "ETH"
    volatility: float  # annualized vol used
    days_to_expiry: float
    fetched_at: datetime

    def to_dict(self) -> dict:
        """Serialize for embedding in the calibration dict."""
        return {
            "implied_prob": round(self.implied_prob, 4),
            "spot_price": round(self.spot_price, 2),
            "strike": self.strike,
            "asset": self.asset,
            "source": "coingecko_bs",
        }


class CryptoPriceCache:
    """Thread-safe TTL cache for crypto spot prices."""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, float]] = {}  # cg_id -> (timestamp, price)

    def get(self, coingecko_id: str) -> float | None:
        with self._lock:
            entry = self._data.get(coingecko_id)
            if entry is None:
                return None
            ts, price = entry
            if time.monotonic() - ts > self._ttl:
                del self._data[coingecko_id]
                return None
            return price

    def put(self, coingecko_id: str, price: float) -> None:
        with self._lock:
            self._data[coingecko_id] = (time.monotonic(), price)


class CryptoClient:
    """CoinGecko client for crypto spot prices with caching."""

    def __init__(self, cache_ttl: int = 60):
        self._cache = CryptoPriceCache(ttl_seconds=cache_ttl)
        # CoinGecko free tier: 10-30 calls/min
        self._limiter = RateLimiter(calls_per_sec=0.5)
        self._failed = False

    @retry_with_backoff(max_attempts=2, base_delay=1.0)
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        self._limiter.acquire()
        resp = httpx.get(f"{_COINGECKO_BASE}{path}", params=params or {}, timeout=10)
        resp.raise_for_status()
        return resp

    def get_spot_price(self, coingecko_id: str) -> float | None:
        """Get current USD spot price for a crypto asset."""
        cached = self._cache.get(coingecko_id)
        if cached is not None:
            return cached

        if self._failed:
            return None

        try:
            resp = self._get(
                "/simple/price",
                params={"ids": coingecko_id, "vs_currencies": "usd"},
            )
            data = resp.json()
            price = data.get(coingecko_id, {}).get("usd")
            if price is not None:
                self._cache.put(coingecko_id, float(price))
                return float(price)
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("CoinGecko rate limited (429)")
                self._failed = True
            else:
                logger.debug("CoinGecko error: %s", exc)
            return None
        except Exception:
            logger.debug("Failed to fetch price for %s", coingecko_id, exc_info=True)
            return None

    def get_batch_prices(self, coingecko_ids: list[str]) -> dict[str, float]:
        """Get USD spot prices for multiple assets in one call."""
        # Check cache first, collect misses
        result: dict[str, float] = {}
        misses: list[str] = []
        for cg_id in coingecko_ids:
            cached = self._cache.get(cg_id)
            if cached is not None:
                result[cg_id] = cached
            else:
                misses.append(cg_id)

        if not misses or self._failed:
            return result

        try:
            resp = self._get(
                "/simple/price",
                params={"ids": ",".join(misses), "vs_currencies": "usd"},
            )
            data = resp.json()
            for cg_id in misses:
                price = data.get(cg_id, {}).get("usd")
                if price is not None:
                    self._cache.put(cg_id, float(price))
                    result[cg_id] = float(price)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("CoinGecko rate limited")
                self._failed = True
            else:
                logger.debug("CoinGecko batch error: %s", exc)
        except Exception:
            logger.debug("Failed batch price fetch", exc_info=True)

        return result

    def get_fair_value(self, pair: MatchedPair) -> CryptoFairValue | None:
        """Compute fair value for a crypto threshold market.

        Returns None if: not a crypto threshold market, price unavailable,
        or market already expired.
        """
        threshold = parse_crypto_ticker(pair.kalshi_market_id)
        if threshold is None:
            return None

        spot = self.get_spot_price(threshold.coingecko_id)
        if spot is None:
            return None

        # Compute time to expiry
        now = datetime.now(timezone.utc)
        if threshold.expiry is not None:
            days = (threshold.expiry - now).total_seconds() / 86400
        elif pair.resolution_date:
            try:
                res_dt = datetime.fromisoformat(
                    pair.resolution_date.replace("Z", "+00:00")
                )
                days = (res_dt - now).total_seconds() / 86400
            except ValueError:
                days = 30.0  # fallback
        else:
            days = 30.0

        years = max(days, 0.0) / 365.25
        vol = DEFAULT_VOLATILITY.get(threshold.coingecko_id, 0.70)

        prob = binary_call_fair_value(
            spot=spot,
            strike=threshold.strike,
            time_to_expiry_years=years,
            volatility=vol,
        )

        return CryptoFairValue(
            implied_prob=prob,
            spot_price=spot,
            strike=threshold.strike,
            asset=threshold.asset,
            volatility=vol,
            days_to_expiry=days,
            fetched_at=now,
        )


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_client: CryptoClient | None = None
_client_lock = threading.Lock()


def get_crypto_client() -> CryptoClient:
    """Return a shared CryptoClient (always available — no API key needed)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = CryptoClient(cache_ttl=60)
        return _client
