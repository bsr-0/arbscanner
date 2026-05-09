"""Sportsbook fair value via multiple odds API backends.

Fetches consensus odds from multiple bookmakers for sports events and
matches them to arbscanner's MatchedPair objects. Supports three
backends with automatic fallback:

1. **Odds-API.io** (default) — 100 req/hr free, 265+ bookmakers
2. **The Odds API** — 500 req/month free, 40+ bookmakers
3. **OddsPapi** — no stated cap, 350+ bookmakers

Set ``ODDS_PROVIDER`` env var to choose: ``odds-api-io`` (default),
``the-odds-api``, or ``oddspapi``. Falls back through the list if the
primary fails.

Fully optional — if no API key is set, ``get_odds_client()`` returns
None and the rest of the system proceeds unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from arbscanner.config import settings
from arbscanner.models import MatchedPair
from arbscanner.utils import RateLimiter, retry_with_backoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kalshi market-ID prefix -> sport_key (shared across all backends)
# ---------------------------------------------------------------------------

KALSHI_PREFIX_TO_SPORT: dict[str, str] = {
    # Basketball
    "KXNBAGAME": "basketball_nba",
    "KXWBAGAME": "basketball_wnba",
    "KXCBAGAME": "basketball_cba",
    "KXEUROLEAGUEGAME": "basketball_euroleague",
    "KXJBLEAGUEGAME": "basketball_japan_b1_league",
    "KXKBLGAME": "basketball_kbl",
    "KXVTBGAME": "basketball_vtb",
    # American Football
    "KXNFLGAME": "americanfootball_nfl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    # Baseball
    "KXMLBGAME": "baseball_mlb",
    "KXKBOGAME": "baseball_kbo",
    "KXNPBGAME": "baseball_npb",
    # Ice Hockey
    "KXNHLGAME": "icehockey_nhl",
    "KXKHLGAME": "icehockey_khl",
    "KXAHLGAME": "icehockey_ahl",
    # Soccer
    "KXEPLGAME": "soccer_epl",
    "KXLALIGAGAME": "soccer_spain_la_liga",
    "KXSERIAGAME": "soccer_italy_serie_a",
    "KXBUNDESLIGAGAME": "soccer_germany_bundesliga",
    "KXLIGUE1GAME": "soccer_france_ligue_one",
    "KXMLSGAME": "soccer_usa_mls",
    # Cricket
    "KXIPLGAME": "cricket_ipl",
    "KXPSLGAME": "cricket_psl",
    # MMA
    "KXUFCFIGHT": "mma_mixed_martial_arts",
}


# ---------------------------------------------------------------------------
# Odds math (shared across all backends)
# ---------------------------------------------------------------------------


def american_to_implied_prob(odds: int | float) -> float:
    """Convert American odds to implied probability.

    +150 -> 100/(150+100) = 0.40
    -200 -> 200/(200+100) = 0.667
    """
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def decimal_to_implied_prob(odds: float) -> float:
    """Convert decimal odds to implied probability. 2.50 -> 0.40."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def remove_vig(probs: list[float]) -> list[float]:
    """Remove overround (vig) from a set of implied probabilities.

    Bookmaker probabilities sum to >1.0 (the overround). Normalizing
    to sum to 1.0 gives vig-free probabilities.
    """
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


def consensus_implied_prob(
    bookmakers: list[dict],
    team_index: int,
) -> tuple[float, int, float, float] | None:
    """Compute consensus fair probability for a team from bookmaker data.

    Expects the normalized format (see ``_normalize_event``).

    Args:
        bookmakers: List of bookmaker dicts with ``markets`` containing
            ``h2h`` outcomes with ``price`` (decimal odds).
        team_index: 0 for home team, 1 for away team.

    Returns:
        (median_prob, num_bookmakers, min_prob, max_prob) or None.
    """
    fair_probs: list[float] = []

    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            raw = [decimal_to_implied_prob(o.get("price", 0)) for o in outcomes]
            fair = remove_vig(raw)
            if team_index < len(fair):
                fair_probs.append(fair[team_index])
            break
    if not fair_probs:
        return None

    return (
        statistics.median(fair_probs),
        len(fair_probs),
        min(fair_probs),
        max(fair_probs),
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FairValue:
    """Sportsbook consensus fair value for a matched sports pair."""

    implied_prob: float
    num_bookmakers: int
    min_prob: float
    max_prob: float
    spread: float
    home_team: str
    away_team: str
    sport_key: str
    fetched_at: datetime

    def to_dict(self) -> dict:
        """Serialize for embedding in the calibration dict."""
        return {
            "implied_prob": round(self.implied_prob, 4),
            "num_bookmakers": self.num_bookmakers,
            "spread": round(self.spread, 4),
            "source": "odds_api",
        }


# ---------------------------------------------------------------------------
# Event matching (shared across all backends)
# ---------------------------------------------------------------------------

_TEAM_SUFFIXES = frozenset({
    "fc", "sc", "bc", "ac", "cf", "fk", "sk", "afc", "bk",
    "city", "united", "athletic", "sporting", "club",
})

_VS_PATTERN = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
_DASH_DUPLICATE = re.compile(r"\s*-\s*.+$")


class EventMatcher:
    """Match MatchedPair team names to odds API events."""

    @staticmethod
    def extract_teams(pair: MatchedPair) -> tuple[str, str] | None:
        """Parse two team names from the pair's titles."""
        for title in (pair.kalshi_title, pair.poly_title):
            cleaned = _DASH_DUPLICATE.sub("", title).strip()
            parts = _VS_PATTERN.split(cleaned, maxsplit=1)
            if len(parts) == 2:
                a, b = parts[0].strip(), parts[1].strip()
                if a and b:
                    return (a, b)
        return None

    @staticmethod
    def _tokenize(name: str) -> set[str]:
        tokens = set(name.lower().split())
        return tokens - _TEAM_SUFFIXES

    @staticmethod
    def score_match(
        pair_teams: tuple[str, str],
        event_home: str,
        event_away: str,
    ) -> float:
        """Jaccard similarity between pair teams and event teams."""
        pa_tok = EventMatcher._tokenize(pair_teams[0])
        pb_tok = EventMatcher._tokenize(pair_teams[1])
        eh_tok = EventMatcher._tokenize(event_home)
        ea_tok = EventMatcher._tokenize(event_away)

        def jaccard(a: set[str], b: set[str]) -> float:
            if not a and not b:
                return 0.0
            inter = len(a & b)
            union = len(a | b)
            return inter / union if union else 0.0

        score_1 = (jaccard(pa_tok, eh_tok) + jaccard(pb_tok, ea_tok)) / 2
        score_2 = (jaccard(pa_tok, ea_tok) + jaccard(pb_tok, eh_tok)) / 2
        return max(score_1, score_2)

    @staticmethod
    def find_event(
        pair: MatchedPair,
        events: list[dict],
        match_threshold: float = 0.3,
    ) -> tuple[dict, int] | None:
        """Find best matching event for a pair.

        Returns (event_dict, team_index) or None.
        """
        teams = EventMatcher.extract_teams(pair)
        if teams is None:
            return None

        filtered = events
        if pair.resolution_date:
            try:
                res_dt = datetime.fromisoformat(
                    pair.resolution_date.replace("Z", "+00:00")
                )
                filtered = [
                    e for e in events
                    if _event_within_window(e, res_dt, hours=48)
                ]
            except ValueError:
                pass

        best_event = None
        best_score = 0.0
        for event in filtered:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            score = EventMatcher.score_match(teams, home, away)
            if score > best_score:
                best_score = score
                best_event = event

        if best_event is None or best_score < match_threshold:
            return None

        home = best_event.get("home_team", "")
        away = best_event.get("away_team", "")
        pa_tok = EventMatcher._tokenize(teams[0])
        eh_tok = EventMatcher._tokenize(home)
        ea_tok = EventMatcher._tokenize(away)

        def jaccard(a: set, b: set) -> float:
            inter = len(a & b)
            union = len(a | b)
            return inter / union if union else 0.0

        team_index = 0 if jaccard(pa_tok, eh_tok) >= jaccard(pa_tok, ea_tok) else 1
        return (best_event, team_index)


def _event_within_window(event: dict, target: datetime, hours: int = 48) -> bool:
    commence = event.get("commence_time", "")
    if not commence:
        return True
    try:
        event_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        delta = abs((event_dt - target).total_seconds())
        return delta <= hours * 3600
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Cache (shared)
# ---------------------------------------------------------------------------


class OddsCache:
    """Thread-safe TTL cache for odds API responses, keyed by sport."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, list[dict]]] = {}

    def get(self, sport: str) -> list[dict] | None:
        with self._lock:
            entry = self._data.get(sport)
            if entry is None:
                return None
            ts, events = entry
            if time.monotonic() - ts > self._ttl:
                del self._data[sport]
                return None
            return events

    def put(self, sport: str, events: list[dict]) -> None:
        with self._lock:
            self._data[sport] = (time.monotonic(), events)

    def invalidate(self, sport: str | None = None) -> None:
        with self._lock:
            if sport:
                self._data.pop(sport, None)
            else:
                self._data.clear()


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------


def _extract_sport_prefix(kalshi_market_id: str) -> str | None:
    """Extract the sport prefix from a Kalshi market ID."""
    parts = kalshi_market_id.split("-", 1)
    if parts:
        return parts[0]
    return None


class OddsBackend:
    """Abstract interface for an odds data provider."""

    name: str = "base"

    def __init__(self, api_key: str, rate_limit: float = 1.0):
        self._api_key = api_key
        self._limiter = RateLimiter(calls_per_sec=rate_limit)
        self._failed = False

    @retry_with_backoff(max_attempts=2, base_delay=1.0)
    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        self._limiter.acquire()
        resp = httpx.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp

    def fetch_sports(self) -> list[str]:
        """Return available sport keys."""
        raise NotImplementedError

    def fetch_odds(self, sport_key: str) -> list[dict]:
        """Return normalized event list for a sport.

        Each event must have: home_team, away_team, commence_time,
        bookmakers[].markets[].key="h2h", outcomes[].price (decimal).
        """
        raise NotImplementedError


class TheOddsApiBackend(OddsBackend):
    """The Odds API (the-odds-api.com) — 500 req/month free."""

    name = "the-odds-api"
    _base = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str):
        super().__init__(api_key, rate_limit=1.0)
        self.requests_remaining: int | None = None

    @retry_with_backoff(max_attempts=2, base_delay=1.0)
    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        self._limiter.acquire()
        all_params = {"apiKey": self._api_key}
        if params:
            all_params.update(params)
        resp = httpx.get(url, params=all_params, timeout=15)
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            self.requests_remaining = int(remaining)
            if self.requests_remaining < 5:
                self._failed = True
                logger.warning("The Odds API budget exhausted (%d remaining)", self.requests_remaining)
        return resp

    def fetch_sports(self) -> list[str]:
        try:
            resp = self._get(f"{self._base}/sports")
            return [s["key"] for s in resp.json() if s.get("active", True)]
        except Exception:
            logger.debug("the-odds-api: failed to fetch sports")
            return []

    def fetch_odds(self, sport_key: str) -> list[dict]:
        if self._failed:
            return []
        try:
            resp = self._get(
                f"{self._base}/sports/{sport_key}/odds",
                params={"regions": "us,eu,uk", "markets": "h2h", "oddsFormat": "decimal"},
            )
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 429):
                self._failed = True
            return []
        except Exception:
            return []


class OddsApiIoBackend(OddsBackend):
    """Odds-API.io — 100 req/hr free, 265+ bookmakers."""

    name = "odds-api-io"
    _base = "https://api.odds-api.io/v1"

    def __init__(self, api_key: str):
        # 100 req/hr = ~1.7 req/min; stay safe at 1/sec
        super().__init__(api_key, rate_limit=1.0)

    def fetch_sports(self) -> list[str]:
        try:
            resp = self._get(
                f"{self._base}/sports",
                params={"apiKey": self._api_key},
            )
            data = resp.json()
            if isinstance(data, list):
                return [s.get("key", s.get("id", "")) for s in data if s.get("active", True)]
            return []
        except Exception:
            logger.debug("odds-api-io: failed to fetch sports")
            return []

    def fetch_odds(self, sport_key: str) -> list[dict]:
        if self._failed:
            return []
        try:
            resp = self._get(
                f"{self._base}/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us,eu,uk",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            data = resp.json()
            # Odds-API.io uses the same response format as The Odds API
            if isinstance(data, list):
                return data
            # If the response is wrapped in a data key
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                self._failed = True
                logger.warning("odds-api-io: %d error, disabling", exc.response.status_code)
            return []
        except Exception:
            return []


class OddsPapiBackend(OddsBackend):
    """OddsPapi (oddspapi.io) — 350+ bookmakers, free."""

    name = "oddspapi"
    _base = "https://api.oddspapi.io/v1"

    def __init__(self, api_key: str):
        super().__init__(api_key, rate_limit=0.5)

    def fetch_sports(self) -> list[str]:
        try:
            resp = self._get(
                f"{self._base}/sports",
                params={"apiKey": self._api_key},
            )
            data = resp.json()
            if isinstance(data, list):
                return [s.get("key", s.get("id", "")) for s in data if s.get("active", True)]
            return []
        except Exception:
            logger.debug("oddspapi: failed to fetch sports")
            return []

    def fetch_odds(self, sport_key: str) -> list[dict]:
        if self._failed:
            return []
        try:
            resp = self._get(
                f"{self._base}/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us,eu,uk",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                self._failed = True
            return []
        except Exception:
            return []


# Provider registry
PROVIDERS: dict[str, type[OddsBackend]] = {
    "odds-api-io": OddsApiIoBackend,
    "the-odds-api": TheOddsApiBackend,
    "oddspapi": OddsPapiBackend,
}

# Fallback order when the primary backend fails
FALLBACK_ORDER = ["odds-api-io", "the-odds-api", "oddspapi"]


def _resolve_provider() -> str:
    """Determine the configured provider from env."""
    return os.getenv("ODDS_PROVIDER", "the-odds-api").lower().strip()


# ---------------------------------------------------------------------------
# Client (uses backends with fallback)
# ---------------------------------------------------------------------------


class OddsClient:
    """Multi-backend odds client with caching and automatic fallback.

    Each backend can have its own API key. The client builds a chain
    from whichever keys are available, with the preferred provider first.
    """

    def __init__(
        self,
        *,
        cache_ttl: int = 300,
        provider: str | None = None,
        keys: dict[str, str] | None = None,
        api_key: str = "",
    ):
        """Create a multi-backend client.

        Args:
            cache_ttl: TTL for cached odds data in seconds.
            provider: Preferred provider name (falls back to ODDS_PROVIDER env).
            keys: Dict mapping provider name -> API key. Providers without
                a key are skipped entirely.
            api_key: Legacy single-key mode. If ``keys`` is not provided,
                this key is used for all backends.
        """
        self._cache = OddsCache(ttl_seconds=cache_ttl)
        self._matcher = EventMatcher()
        self._available_sports: set[str] | None = None

        if keys is None:
            keys = {name: api_key for name in PROVIDERS if api_key}

        preferred = provider or _resolve_provider()
        self._backends: list[OddsBackend] = []

        # Build ordered backend list: preferred first, then fallbacks
        ordered = [preferred] + [n for n in FALLBACK_ORDER if n != preferred]
        for name in ordered:
            key = keys.get(name, "")
            if key and name in PROVIDERS:
                self._backends.append(PROVIDERS[name](key))

        self._active_backend: OddsBackend | None = self._backends[0] if self._backends else None
        self.requests_remaining: int | None = None

    @property
    def provider_name(self) -> str:
        if self._active_backend:
            return self._active_backend.name
        return "none"

    def fetch_available_sports(self) -> list[str]:
        """Fetch available sports, trying backends in order."""
        if self._available_sports is not None:
            return sorted(self._available_sports)

        for backend in self._backends:
            if backend._failed:
                continue
            sports = backend.fetch_sports()
            if sports:
                self._available_sports = set(sports)
                self._active_backend = backend
                logger.info("Using %s for odds (found %d sports)", backend.name, len(sports))
                return sorted(self._available_sports)

        self._available_sports = set()
        return []

    def fetch_odds(self, sport_key: str) -> list[dict]:
        """Fetch odds, using cache and falling through backends on failure."""
        cached = self._cache.get(sport_key)
        if cached is not None:
            return cached

        if self._available_sports is not None and sport_key not in self._available_sports:
            return []

        for backend in self._backends:
            if backend._failed:
                continue
            events = backend.fetch_odds(sport_key)
            if events:
                self._cache.put(sport_key, events)
                self._active_backend = backend
                # Track remaining requests for The Odds API
                if isinstance(backend, TheOddsApiBackend):
                    self.requests_remaining = backend.requests_remaining
                logger.info(
                    "Fetched %d events for %s via %s",
                    len(events), sport_key, backend.name,
                )
                return events

        return []

    def get_fair_value(self, pair: MatchedPair) -> FairValue | None:
        """Get sportsbook fair value for a matched sports pair."""
        prefix = _extract_sport_prefix(pair.kalshi_market_id)
        if prefix is None:
            return None
        sport_key = KALSHI_PREFIX_TO_SPORT.get(prefix)
        if sport_key is None:
            return None

        events = self.fetch_odds(sport_key)
        if not events:
            return None

        result = self._matcher.find_event(pair, events)
        if result is None:
            return None

        event, team_index = result
        bookmakers = event.get("bookmakers", [])
        consensus = consensus_implied_prob(bookmakers, team_index)
        if consensus is None:
            return None

        median_prob, count, min_prob, max_prob = consensus
        return FairValue(
            implied_prob=median_prob,
            num_bookmakers=count,
            min_prob=min_prob,
            max_prob=max_prob,
            spread=max_prob - min_prob,
            home_team=event.get("home_team", ""),
            away_team=event.get("away_team", ""),
            sport_key=sport_key,
            fetched_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_client: OddsClient | None = None
_client_lock = threading.Lock()


def get_odds_client() -> OddsClient | None:
    """Return a shared OddsClient, or None if no odds API keys are set."""
    global _client
    keys = {
        "the-odds-api": settings.odds_api_key,
        "oddspapi": settings.oddspapi_api_key,
        "odds-api-io": settings.odds_api_io_key,
    }
    # Filter to providers that actually have a key
    available = {k: v for k, v in keys.items() if v}
    if not available:
        return None
    with _client_lock:
        if _client is None:
            _client = OddsClient(
                keys=available,
                cache_ttl=settings.odds_cache_ttl,
            )
        return _client
