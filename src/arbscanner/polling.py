"""Polling fair value via FiveThirtyEight approval rating aggregates.

Fetches presidential approval polling averages and computes fair value
for approval-threshold markets ("Will Trump's approval be above/below
X% by date Y?") using historical volatility of approval ratings.

No API key required — FiveThirtyEight publishes public JSON.
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
# FiveThirtyEight data source
# ---------------------------------------------------------------------------

# FiveThirtyEight publishes approval data at this URL.
# The format returns a JSON object with polling averages.
_538_APPROVAL_URL = (
    "https://projects.fivethirtyeight.com/polls/approval/donald-trump/polls.json"
)

# Fallback: RealClearPolitics-style average (we'll scrape 538 first)
# Historical daily volatility of presidential approval ratings is ~0.15-0.25
# percentage points per day (annualized ~2.5-4 points).
DEFAULT_APPROVAL_DAILY_VOL = 0.20  # percentage points per day


# ---------------------------------------------------------------------------
# Kalshi ticker parsing
# ---------------------------------------------------------------------------

# KXAPRPOTUS-26APR17-41.1 — approval on specific date, threshold
_PATTERN_APRPOTUS = re.compile(
    r"^KXAPRPOTUS-(\d{2}[A-Z]{3}\d{2})-([\d.]+)$"
)

# KXTRUMPAPPROVALBELOW-26DEC31-41 — will approval drop below X before date
_PATTERN_APPROVAL_BELOW = re.compile(
    r"^KXTRUMPAPPROVALBELOW-(\d{2}[A-Z]{3}\d{2})-(\d+)$"
)

# KXTRUMPAPPROVALYEAR-26DEC31-43 — will approval reach X before date
_PATTERN_APPROVAL_YEAR = re.compile(
    r"^KXTRUMPAPPROVALYEAR-(\d{2}[A-Z]{3}\d{2})-(\d+)$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class ApprovalThreshold:
    """Parsed components of a Kalshi approval rating market."""

    threshold: float  # the approval % level
    expiry: datetime | None  # date by which approval is measured
    direction: str  # "above", "below", or "point" (exact date snapshot)


def parse_approval_ticker(kalshi_market_id: str) -> ApprovalThreshold | None:
    """Parse a Kalshi approval rating ticker.

    Returns None if not a recognized approval pattern.
    """
    # KXAPRPOTUS — snapshot on a specific date
    m = _PATTERN_APRPOTUS.match(kalshi_market_id)
    if m:
        expiry = _parse_kalshi_date(m.group(1))
        threshold = float(m.group(2))
        return ApprovalThreshold(
            threshold=threshold, expiry=expiry, direction="above",
        )

    # KXTRUMPAPPROVALBELOW — will it drop below threshold before date
    m = _PATTERN_APPROVAL_BELOW.match(kalshi_market_id)
    if m:
        expiry = _parse_kalshi_date(m.group(1))
        threshold = float(m.group(2))
        return ApprovalThreshold(
            threshold=threshold, expiry=expiry, direction="below",
        )

    # KXTRUMPAPPROVALYEAR — will it reach threshold before date
    m = _PATTERN_APPROVAL_YEAR.match(kalshi_market_id)
    if m:
        expiry = _parse_kalshi_date(m.group(1))
        threshold = float(m.group(2))
        return ApprovalThreshold(
            threshold=threshold, expiry=expiry, direction="above",
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
# Fair value math
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def approval_fair_value_point(
    current_approval: float,
    threshold: float,
    days_to_expiry: float,
    daily_vol: float = DEFAULT_APPROVAL_DAILY_VOL,
) -> float:
    """Fair value for "approval above threshold on date X".

    Models approval as a random walk with daily volatility.
    P(approval > threshold) = N((current - threshold) / (vol * sqrt(days)))
    """
    if days_to_expiry <= 0:
        return 1.0 if current_approval > threshold else 0.0
    if daily_vol <= 0:
        return 1.0 if current_approval > threshold else 0.0

    total_vol = daily_vol * math.sqrt(days_to_expiry)
    d = (current_approval - threshold) / total_vol
    return _norm_cdf(d)


def approval_fair_value_ever_below(
    current_approval: float,
    threshold: float,
    days_to_expiry: float,
    daily_vol: float = DEFAULT_APPROVAL_DAILY_VOL,
) -> float:
    """Fair value for "approval drops below threshold at any point before date".

    Uses the reflection principle for Brownian motion: the probability
    that a random walk starting at `current` ever touches `threshold`
    (below) within `days` is approximated by:

    P = 2 * N(-|current - threshold| / (vol * sqrt(days)))

    This is exact for pure Brownian motion and a reasonable approximation
    for approval ratings which mean-revert slowly.
    """
    if current_approval <= threshold:
        return 1.0  # already below
    if days_to_expiry <= 0:
        return 0.0
    if daily_vol <= 0:
        return 0.0

    total_vol = daily_vol * math.sqrt(days_to_expiry)
    distance = current_approval - threshold
    # Reflection principle
    prob = 2.0 * _norm_cdf(-distance / total_vol)
    return min(prob, 1.0)


def approval_fair_value_ever_above(
    current_approval: float,
    threshold: float,
    days_to_expiry: float,
    daily_vol: float = DEFAULT_APPROVAL_DAILY_VOL,
) -> float:
    """Fair value for "approval reaches threshold at any point before date"."""
    if current_approval >= threshold:
        return 1.0  # already above
    if days_to_expiry <= 0:
        return 0.0
    if daily_vol <= 0:
        return 0.0

    total_vol = daily_vol * math.sqrt(days_to_expiry)
    distance = threshold - current_approval
    prob = 2.0 * _norm_cdf(-distance / total_vol)
    return min(prob, 1.0)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PollingFairValue:
    """Fair value for an approval rating market."""

    implied_prob: float
    current_approval: float
    threshold: float
    direction: str  # "above", "below"
    days_to_expiry: float
    daily_vol: float
    fetched_at: datetime

    def to_dict(self) -> dict:
        """Serialize for embedding in the calibration dict."""
        return {
            "implied_prob": round(self.implied_prob, 4),
            "current_approval": round(self.current_approval, 1),
            "threshold": self.threshold,
            "source": "538_approval",
        }


# ---------------------------------------------------------------------------
# Approval data client
# ---------------------------------------------------------------------------


class ApprovalCache:
    """Thread-safe TTL cache for approval rating data."""

    def __init__(self, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._approval: float | None = None
        self._fetched_at: float = 0

    def get(self) -> float | None:
        with self._lock:
            if self._approval is None:
                return None
            if time.monotonic() - self._fetched_at > self._ttl:
                self._approval = None
                return None
            return self._approval

    def put(self, approval: float) -> None:
        with self._lock:
            self._approval = approval
            self._fetched_at = time.monotonic()


class PollingClient:
    """Client for fetching presidential approval polling data."""

    def __init__(self, cache_ttl: int = 3600):
        self._cache = ApprovalCache(ttl_seconds=cache_ttl)
        self._limiter = RateLimiter(calls_per_sec=0.2)  # very conservative
        self._failed = False

    @retry_with_backoff(max_attempts=2, base_delay=2.0)
    def _get(self, url: str) -> httpx.Response:
        self._limiter.acquire()
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp

    def get_current_approval(self) -> float | None:
        """Fetch the latest Trump approval rating average.

        Tries FiveThirtyEight's polling JSON first. Returns the approve
        percentage as a float (e.g., 42.3), or None on failure.
        """
        cached = self._cache.get()
        if cached is not None:
            return cached

        if self._failed:
            return None

        try:
            resp = self._get(_538_APPROVAL_URL)
            polls = resp.json()

            # 538 returns an array of poll objects. We want the most recent
            # "approve" value from the aggregate/average. The format varies
            # but typically has "pct" or "yes" fields.
            # Strategy: take the average of the most recent batch of polls.
            if isinstance(polls, list) and polls:
                # Sort by date descending, take the most recent polls
                recent = polls[:20]  # most recent entries
                approvals = []
                for poll in recent:
                    # Try different field names
                    pct = poll.get("pct_estimate") or poll.get("pct") or poll.get("yes")
                    if pct is not None:
                        try:
                            approvals.append(float(pct))
                        except (ValueError, TypeError):
                            pass
                if approvals:
                    avg = sum(approvals) / len(approvals)
                    self._cache.put(avg)
                    logger.info("Fetched approval rating: %.1f%% (avg of %d polls)", avg, len(approvals))
                    return avg

            logger.debug("Could not parse approval data from 538")
            return None
        except Exception:
            logger.debug("Failed to fetch approval data", exc_info=True)
            self._failed = True
            return None

    def set_approval(self, approval: float) -> None:
        """Manually set the current approval (for testing or manual override)."""
        self._cache.put(approval)

    def get_fair_value(self, pair: MatchedPair) -> PollingFairValue | None:
        """Compute fair value for an approval rating market.

        Returns None if: not an approval market, data unavailable.
        """
        parsed = parse_approval_ticker(pair.kalshi_market_id)
        if parsed is None:
            return None

        approval = self.get_current_approval()
        if approval is None:
            return None

        now = datetime.now(timezone.utc)
        if parsed.expiry is not None:
            days = (parsed.expiry - now).total_seconds() / 86400
        elif pair.resolution_date:
            try:
                res_dt = datetime.fromisoformat(
                    pair.resolution_date.replace("Z", "+00:00")
                )
                days = (res_dt - now).total_seconds() / 86400
            except ValueError:
                days = 30.0
        else:
            days = 30.0

        days = max(days, 0.0)

        # Choose the right pricing model based on market type
        ticker = pair.kalshi_market_id
        if ticker.startswith("KXTRUMPAPPROVALBELOW"):
            # "Will approval drop below X at any point" — barrier option
            prob = approval_fair_value_ever_below(
                approval, parsed.threshold, days,
            )
        elif ticker.startswith("KXTRUMPAPPROVALYEAR"):
            # "Will approval reach X at any point" — barrier option (upside)
            prob = approval_fair_value_ever_above(
                approval, parsed.threshold, days,
            )
        else:
            # Point-in-time: "What is approval on date X?"
            prob = approval_fair_value_point(
                approval, parsed.threshold, days,
            )

        return PollingFairValue(
            implied_prob=prob,
            current_approval=approval,
            threshold=parsed.threshold,
            direction=parsed.direction,
            days_to_expiry=days,
            daily_vol=DEFAULT_APPROVAL_DAILY_VOL,
            fetched_at=now,
        )


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_client: PollingClient | None = None
_client_lock = threading.Lock()


def get_polling_client() -> PollingClient:
    """Return a shared PollingClient (always available — no API key needed)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = PollingClient(cache_ttl=3600)
        return _client
