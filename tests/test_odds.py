"""Tests for arbscanner.odds — sportsbook fair value integration."""

import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from arbscanner.odds import (
    EventMatcher,
    FairValue,
    OddsCache,
    OddsClient,
    american_to_implied_prob,
    consensus_implied_prob,
    decimal_to_implied_prob,
    get_odds_client,
    remove_vig,
    _extract_sport_prefix,
)


# ---------------------------------------------------------------------------
# Odds math
# ---------------------------------------------------------------------------


class TestAmericanToImpliedProb:
    def test_positive_odds(self):
        # +150: $100 bet wins $150 -> prob = 100/250 = 0.40
        assert abs(american_to_implied_prob(150) - 0.40) < 0.001

    def test_negative_odds(self):
        # -200: $200 bet wins $100 -> prob = 200/300 = 0.667
        assert abs(american_to_implied_prob(-200) - 0.6667) < 0.001

    def test_even_odds(self):
        # +100: prob = 100/200 = 0.50
        assert abs(american_to_implied_prob(100) - 0.50) < 0.001

    def test_heavy_favorite(self):
        # -500: prob = 500/600 = 0.833
        assert abs(american_to_implied_prob(-500) - 0.8333) < 0.001


class TestDecimalToImpliedProb:
    def test_standard(self):
        assert abs(decimal_to_implied_prob(2.50) - 0.40) < 0.001

    def test_even(self):
        assert abs(decimal_to_implied_prob(2.0) - 0.50) < 0.001

    def test_heavy_favorite(self):
        assert abs(decimal_to_implied_prob(1.20) - 0.8333) < 0.001

    def test_zero_odds(self):
        assert decimal_to_implied_prob(0) == 0.0

    def test_negative_odds(self):
        assert decimal_to_implied_prob(-1.0) == 0.0


class TestRemoveVig:
    def test_standard_overround(self):
        # Typical 5% overround
        raw = [0.55, 0.50]  # sums to 1.05
        fair = remove_vig(raw)
        assert abs(sum(fair) - 1.0) < 1e-10
        assert abs(fair[0] - 0.55 / 1.05) < 1e-10

    def test_no_overround(self):
        raw = [0.60, 0.40]
        fair = remove_vig(raw)
        assert abs(fair[0] - 0.60) < 1e-10
        assert abs(fair[1] - 0.40) < 1e-10

    def test_zero_total(self):
        raw = [0.0, 0.0]
        fair = remove_vig(raw)
        assert fair == [0.0, 0.0]


class TestConsensusImpliedProb:
    def _make_bookmakers(self, odds_pairs):
        """Create bookmaker dicts from list of (home_decimal, away_decimal)."""
        return [
            {
                "key": f"book_{i}",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Home", "price": home},
                            {"name": "Away", "price": away},
                        ],
                    }
                ],
            }
            for i, (home, away) in enumerate(odds_pairs)
        ]

    def test_single_bookmaker(self):
        bms = self._make_bookmakers([(2.0, 2.0)])
        result = consensus_implied_prob(bms, 0)
        assert result is not None
        median, count, mn, mx = result
        assert count == 1
        assert abs(median - 0.50) < 0.01

    def test_multiple_bookmakers(self):
        bms = self._make_bookmakers([
            (1.80, 2.10),  # home ~53%
            (1.90, 2.00),  # home ~51%
            (1.85, 2.05),  # home ~52%
        ])
        result = consensus_implied_prob(bms, 0)
        assert result is not None
        median, count, mn, mx = result
        assert count == 3
        assert 0.50 < median < 0.55

    def test_away_team_index(self):
        bms = self._make_bookmakers([(1.50, 2.80)])
        home_result = consensus_implied_prob(bms, 0)
        away_result = consensus_implied_prob(bms, 1)
        assert home_result is not None
        assert away_result is not None
        # Home should have higher probability than away
        assert home_result[0] > away_result[0]
        # Should sum to ~1.0
        assert abs(home_result[0] + away_result[0] - 1.0) < 0.01

    def test_empty_bookmakers(self):
        assert consensus_implied_prob([], 0) is None

    def test_no_h2h_market(self):
        bms = [{"key": "book_1", "markets": [{"key": "spreads", "outcomes": []}]}]
        assert consensus_implied_prob(bms, 0) is None


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------


@dataclass
class MockMatchedPair:
    poly_market_id: str = "123"
    poly_title: str = ""
    kalshi_market_id: str = "KXCBAGAME-26APR"
    kalshi_title: str = ""
    confidence: float = 0.95
    source: str = "embedding"
    matched_at: str = ""
    poly_yes_outcome_id: str = ""
    poly_no_outcome_id: str = ""
    kalshi_yes_outcome_id: str = ""
    kalshi_no_outcome_id: str = ""
    category: str = "Sports"
    resolution_date: str = ""


class TestExtractTeams:
    def test_standard_vs(self):
        pair = MockMatchedPair(kalshi_title="Shanxi Loongs vs Beijing Royal Fighters")
        teams = EventMatcher.extract_teams(pair)
        assert teams == ("Shanxi Loongs", "Beijing Royal Fighters")

    def test_vs_dot(self):
        pair = MockMatchedPair(kalshi_title="Team Alpha vs. Team Beta")
        teams = EventMatcher.extract_teams(pair)
        assert teams == ("Team Alpha", "Team Beta")

    def test_poly_duplicate_title(self):
        pair = MockMatchedPair(
            kalshi_title="",
            poly_title="Beijing Royal Fighters vs. Shanxi Loongs - Beijing Royal Fighters vs. Shanxi Loongs",
        )
        teams = EventMatcher.extract_teams(pair)
        assert teams is not None
        assert teams == ("Beijing Royal Fighters", "Shanxi Loongs")

    def test_no_vs(self):
        pair = MockMatchedPair(
            kalshi_title="Fed rate cut June",
            poly_title="Will the Fed cut rates?",
        )
        teams = EventMatcher.extract_teams(pair)
        assert teams is None

    def test_prefers_kalshi(self):
        pair = MockMatchedPair(
            kalshi_title="Home vs Away",
            poly_title="Home Team vs Away Team - Home Team vs Away Team",
        )
        teams = EventMatcher.extract_teams(pair)
        assert teams == ("Home", "Away")


class TestScoreMatch:
    def test_exact_match(self):
        score = EventMatcher.score_match(
            ("Beijing Royal Fighters", "Shanxi Loongs"),
            "Beijing Royal Fighters",
            "Shanxi Loongs",
        )
        assert score > 0.8

    def test_swapped_order(self):
        score = EventMatcher.score_match(
            ("Shanxi Loongs", "Beijing Royal Fighters"),
            "Beijing Royal Fighters",
            "Shanxi Loongs",
        )
        assert score > 0.8

    def test_partial_name(self):
        score = EventMatcher.score_match(
            ("Beijing Fighters", "Shanxi"),
            "Beijing Royal Fighters",
            "Shanxi Loongs",
        )
        assert score > 0.3

    def test_no_match(self):
        score = EventMatcher.score_match(
            ("Lakers", "Celtics"),
            "Bayern Munich",
            "Real Madrid",
        )
        assert score < 0.1

    def test_suffix_stripping(self):
        score = EventMatcher.score_match(
            ("Manchester United FC", "Liverpool FC"),
            "Manchester United",
            "Liverpool",
        )
        # "fc" stripped, so "manchester united" matches well
        assert score > 0.7


class TestFindEvent:
    def test_finds_matching_event(self):
        pair = MockMatchedPair(
            kalshi_title="Shanxi Loongs vs Beijing Royal Fighters",
            resolution_date="2026-04-20T11:00:00+00:00",
        )
        events = [
            {
                "id": "ev1",
                "home_team": "Beijing Royal Fighters",
                "away_team": "Shanxi Loongs",
                "commence_time": "2026-04-20T10:00:00Z",
                "bookmakers": [],
            },
        ]
        result = EventMatcher.find_event(pair, events)
        assert result is not None
        event, team_index = result
        assert event["id"] == "ev1"

    def test_filters_by_date(self):
        pair = MockMatchedPair(
            kalshi_title="Shanxi Loongs vs Beijing Royal Fighters",
            resolution_date="2026-04-20T11:00:00+00:00",
        )
        events = [
            {
                "id": "wrong_date",
                "home_team": "Beijing Royal Fighters",
                "away_team": "Shanxi Loongs",
                "commence_time": "2026-05-15T10:00:00Z",
                "bookmakers": [],
            },
        ]
        result = EventMatcher.find_event(pair, events)
        assert result is None

    def test_no_events(self):
        pair = MockMatchedPair(kalshi_title="A vs B")
        assert EventMatcher.find_event(pair, []) is None

    def test_below_threshold(self):
        pair = MockMatchedPair(kalshi_title="Lakers vs Celtics")
        events = [
            {
                "id": "ev1",
                "home_team": "Bayern Munich",
                "away_team": "Real Madrid",
                "commence_time": "",
                "bookmakers": [],
            },
        ]
        result = EventMatcher.find_event(pair, events)
        assert result is None


# ---------------------------------------------------------------------------
# Extract sport prefix
# ---------------------------------------------------------------------------


class TestExtractSportPrefix:
    def test_standard(self):
        assert _extract_sport_prefix("KXCBAGAME-26APR130735SHAXBRF-BRF") == "KXCBAGAME"

    def test_no_dash(self):
        assert _extract_sport_prefix("KXCBAGAME") == "KXCBAGAME"

    def test_empty(self):
        assert _extract_sport_prefix("") == ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestOddsCache:
    def test_put_and_get(self):
        cache = OddsCache(ttl_seconds=60)
        cache.put("basketball_nba", [{"id": "ev1"}])
        result = cache.get("basketball_nba")
        assert result is not None
        assert len(result) == 1

    def test_miss(self):
        cache = OddsCache(ttl_seconds=60)
        assert cache.get("nonexistent") is None

    def test_expiry(self):
        cache = OddsCache(ttl_seconds=0)  # instant expiry
        cache.put("basketball_nba", [{"id": "ev1"}])
        time.sleep(0.01)
        assert cache.get("basketball_nba") is None

    def test_invalidate_one(self):
        cache = OddsCache(ttl_seconds=60)
        cache.put("a", [])
        cache.put("b", [])
        cache.invalidate("a")
        assert cache.get("a") is None
        assert cache.get("b") is not None

    def test_invalidate_all(self):
        cache = OddsCache(ttl_seconds=60)
        cache.put("a", [])
        cache.put("b", [])
        cache.invalidate()
        assert cache.get("a") is None
        assert cache.get("b") is None


# ---------------------------------------------------------------------------
# FairValue
# ---------------------------------------------------------------------------


class TestFairValue:
    def test_to_dict(self):
        from datetime import datetime, timezone

        fv = FairValue(
            implied_prob=0.6234,
            num_bookmakers=4,
            min_prob=0.58,
            max_prob=0.65,
            spread=0.07,
            home_team="Home",
            away_team="Away",
            sport_key="basketball_nba",
            fetched_at=datetime.now(timezone.utc),
        )
        d = fv.to_dict()
        assert d["implied_prob"] == 0.6234
        assert d["num_bookmakers"] == 4
        assert d["spread"] == 0.07
        assert d["source"] == "odds_api"


# ---------------------------------------------------------------------------
# Client (mocked HTTP)
# ---------------------------------------------------------------------------


class TestOddsClientNoKey:
    def test_get_odds_client_returns_none(self):
        with patch("arbscanner.odds.settings") as mock_settings:
            mock_settings.odds_api_key = ""
            # Re-import to get fresh module state isn't needed since
            # get_odds_client checks settings.odds_api_key directly.
            # But we need to reset the cached client.
            import arbscanner.odds as odds_mod
            odds_mod._client = None
            result = get_odds_client()
            assert result is None


class TestOddsClientGetFairValue:
    def _make_client(self):
        client = OddsClient("test_key", cache_ttl=60, provider="the-odds-api")
        client._available_sports = set()
        return client

    def test_non_sports_prefix_returns_none(self):
        client = self._make_client()
        pair = MockMatchedPair(kalshi_market_id="KXFEDCUT-26JUN")
        assert client.get_fair_value(pair) is None

    def test_all_backends_failed_returns_none(self):
        client = self._make_client()
        client._available_sports = {"basketball_cba"}
        for b in client._backends:
            b._failed = True

        pair = MockMatchedPair(
            kalshi_market_id="KXCBAGAME-26APR",
            kalshi_title="Home vs Away",
        )
        assert client.get_fair_value(pair) is None


class TestMultiBackend:
    def test_provider_selection(self):
        client = OddsClient("key", provider="oddspapi")
        assert client.provider_name == "oddspapi"

    def test_default_provider(self):
        client = OddsClient("key", provider="the-odds-api")
        assert client.provider_name == "the-odds-api"

    def test_fallback_order(self):
        client = OddsClient("key", provider="the-odds-api")
        names = [b.name for b in client._backends]
        assert names[0] == "the-odds-api"
        assert len(names) == 3  # all three backends

    def test_backends_have_correct_types(self):
        from arbscanner.odds import TheOddsApiBackend, OddsApiIoBackend, OddsPapiBackend
        client = OddsClient("key", provider="the-odds-api")
        assert isinstance(client._backends[0], TheOddsApiBackend)

    def test_provider_name_reflects_active(self):
        client = OddsClient("key", provider="odds-api-io")
        assert client.provider_name == "odds-api-io"
