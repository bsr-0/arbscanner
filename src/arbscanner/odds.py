"""Sportsbook fair value via The Odds API.

Fetches consensus odds from multiple bookmakers for sports events and
matches them to arbscanner's MatchedPair objects. The fair value is
nested inside the existing calibration dict so the dashboard and API
get it without schema changes.

Fully optional — if ODDS_API_KEY is unset, ``get_odds_client()``
returns None and the rest of the system proceeds unchanged.
"""

from __future__ import annotations

import logging
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
# Kalshi market-ID prefix -> The Odds API sport_key
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
# Odds math
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

    Args:
        bookmakers: List of bookmaker dicts from The Odds API response.
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
            # Convert all outcomes to implied prob, then remove vig
            raw = [decimal_to_implied_prob(o.get("price", 0)) for o in outcomes]
            fair = remove_vig(raw)
            if team_index < len(fair):
                fair_probs.append(fair[team_index])
            break  # only one h2h market per bookmaker

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

    implied_prob: float  # consensus probability for the "yes" side (0.0-1.0)
    num_bookmakers: int
    min_prob: float
    max_prob: float
    spread: float  # max - min (bookmaker disagreement)
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
# Event matching
# ---------------------------------------------------------------------------

# Words to strip when comparing team names
_TEAM_SUFFIXES = frozenset({
    "fc", "sc", "bc", "ac", "cf", "fk", "sk", "afc", "bk",
    "city", "united", "athletic", "sporting", "club",
})

_VS_PATTERN = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
# Polymarket often duplicates the title: "Team A vs Team B - Team A vs Team B"
_DASH_DUPLICATE = re.compile(r"\s*-\s*.+$")


class EventMatcher:
    """Match MatchedPair team names to Odds API events."""

    @staticmethod
    def extract_teams(pair: MatchedPair) -> tuple[str, str] | None:
        """Parse two team names from the pair's titles.

        Tries kalshi_title first (shorter/cleaner), falls back to poly_title.
        Returns (team_a, team_b) or None if unparseable.
        """
        for title in (pair.kalshi_title, pair.poly_title):
            # Strip trailing duplicate after " - "
            cleaned = _DASH_DUPLICATE.sub("", title).strip()
            parts = _VS_PATTERN.split(cleaned, maxsplit=1)
            if len(parts) == 2:
                a, b = parts[0].strip(), parts[1].strip()
                if a and b:
                    return (a, b)
        return None

    @staticmethod
    def _tokenize(name: str) -> set[str]:
        """Lowercase, split, strip suffixes."""
        tokens = set(name.lower().split())
        return tokens - _TEAM_SUFFIXES

    @staticmethod
    def score_match(
        pair_teams: tuple[str, str],
        event_home: str,
        event_away: str,
    ) -> float:
        """Jaccard similarity between pair teams and event teams.

        Tries both orderings (pair teams might be swapped vs. event).
        Returns best score 0.0-1.0.
        """
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

        # Try both orderings
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

        Args:
            pair: The matched market pair.
            events: Odds API events for the relevant sport.
            match_threshold: Minimum Jaccard score to accept.

        Returns:
            (event_dict, team_index) where team_index is 0 (home) or 1 (away)
            for whichever team corresponds to the "YES" side of the pair.
            None if no match found.
        """
        teams = EventMatcher.extract_teams(pair)
        if teams is None:
            return None

        # Filter by date if resolution_date is available
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

        # Determine which team is the "first" team in the pair title
        # (the YES side for Polymarket-style "Team A vs Team B" markets).
        home = best_event.get("home_team", "")
        away = best_event.get("away_team", "")
        pa_tok = EventMatcher._tokenize(teams[0])
        eh_tok = EventMatcher._tokenize(home)
        ea_tok = EventMatcher._tokenize(away)

        def jaccard(a: set, b: set) -> float:
            inter = len(a & b)
            union = len(a | b)
            return inter / union if union else 0.0

        # team_index 0 = home, 1 = away
        if jaccard(pa_tok, eh_tok) >= jaccard(pa_tok, ea_tok):
            team_index = 0
        else:
            team_index = 1

        return (best_event, team_index)


def _event_within_window(event: dict, target: datetime, hours: int = 48) -> bool:
    """Check if an event's commence_time is within +/- hours of target."""
    commence = event.get("commence_time", "")
    if not commence:
        return True  # no date info, include it
    try:
        event_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        delta = abs((event_dt - target).total_seconds())
        return delta <= hours * 3600
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class OddsCache:
    """Thread-safe TTL cache for Odds API responses, keyed by sport."""

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
# Client
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.the-odds-api.com/v4"


def _extract_sport_prefix(kalshi_market_id: str) -> str | None:
    """Extract the sport prefix from a Kalshi market ID.

    "KXCBAGAME-26APR130735SHAXBRF-BRF" -> "KXCBAGAME"
    """
    parts = kalshi_market_id.split("-", 1)
    if parts:
        return parts[0]
    return None


class OddsClient:
    """Client for The Odds API with caching and budget tracking."""

    def __init__(self, api_key: str, cache_ttl: int = 300):
        self._api_key = api_key
        self._cache = OddsCache(ttl_seconds=cache_ttl)
        self._limiter = RateLimiter(calls_per_sec=1.0)
        self._available_sports: set[str] | None = None
        self._requests_remaining: int | None = None
        self._budget_exhausted = False
        self._matcher = EventMatcher()

    @retry_with_backoff(max_attempts=2, base_delay=1.0)
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """Make a GET request to The Odds API."""
        self._limiter.acquire()
        url = f"{_BASE_URL}{path}"
        all_params = {"apiKey": self._api_key}
        if params:
            all_params.update(params)
        resp = httpx.get(url, params=all_params, timeout=15)
        resp.raise_for_status()
        # Track API budget from response headers
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            self._requests_remaining = int(remaining)
            if self._requests_remaining < 20:
                logger.warning(
                    "Odds API budget low: %d requests remaining",
                    self._requests_remaining,
                )
                if self._requests_remaining < 5:
                    self._budget_exhausted = True
        return resp

    def fetch_available_sports(self) -> list[str]:
        """Fetch list of available sport keys. Cached for session lifetime."""
        if self._available_sports is not None:
            return sorted(self._available_sports)
        try:
            resp = self._get("/sports")
            sports = [s["key"] for s in resp.json() if s.get("active", True)]
            self._available_sports = set(sports)
            return sorted(self._available_sports)
        except Exception:
            logger.debug("Failed to fetch available sports from Odds API")
            self._available_sports = set()
            return []

    def fetch_odds(self, sport_key: str) -> list[dict]:
        """Fetch odds for a sport. Returns cached data if available."""
        cached = self._cache.get(sport_key)
        if cached is not None:
            return cached

        if self._budget_exhausted:
            logger.debug("Odds API budget exhausted, skipping fetch for %s", sport_key)
            return []

        # Check if sport is available
        if self._available_sports is not None and sport_key not in self._available_sports:
            return []

        try:
            resp = self._get(
                f"/sports/{sport_key}/odds",
                params={
                    "regions": "us,eu,uk",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            events = resp.json()
            self._cache.put(sport_key, events)
            logger.info(
                "Fetched %d events for %s (remaining: %s)",
                len(events),
                sport_key,
                self._requests_remaining,
            )
            return events
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                logger.error("Odds API key is invalid (401)")
                self._budget_exhausted = True
            elif exc.response.status_code == 429:
                logger.warning("Odds API rate limited (429)")
                self._budget_exhausted = True
            else:
                logger.debug("Odds API error for %s: %s", sport_key, exc)
            return []
        except Exception:
            logger.debug("Failed to fetch odds for %s", sport_key, exc_info=True)
            return []

    def get_fair_value(self, pair: MatchedPair) -> FairValue | None:
        """Get sportsbook fair value for a matched sports pair.

        Returns None if: not a sports pair, sport not covered,
        event not matched, or API unavailable.
        """
        # Map Kalshi prefix to sport key
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
    """Return a shared OddsClient, or None if ODDS_API_KEY is not set."""
    global _client
    if not settings.odds_api_key:
        return None
    with _client_lock:
        if _client is None:
            _client = OddsClient(
                api_key=settings.odds_api_key,
                cache_ttl=settings.odds_cache_ttl,
            )
        return _client
