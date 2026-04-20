"""Tests for the market matching pipeline."""

import json
import tempfile
from pathlib import Path

from arbscanner.matcher import load_cache, normalize_title, save_cache
from arbscanner.models import MatchedPair, MatchedPairsCache


def test_normalize_basic():
    """Test basic title normalization."""
    assert normalize_title("Will the Fed cut rates?") == "federal reserve cut rates"
    assert normalize_title("  Hello World!  ") == "hello world"


def test_normalize_question_words():
    """Test stripping of leading question words."""
    assert normalize_title("Will Trump win?") == "trump win"
    assert normalize_title("What will GDP be?") == "will gross domestic product be"
    assert normalize_title("Who will win the election?") == "will win the election"


def test_normalize_abbreviations():
    """Test abbreviation expansion."""
    assert "federal reserve" in normalize_title("Fed rate cut")
    assert "consumer price index" in normalize_title("CPI above 3%")
    assert "supreme court" in normalize_title("SCOTUS ruling")


def test_normalize_punctuation():
    """Test punctuation removal."""
    result = normalize_title("Will it happen? Yes/No!")
    assert "?" not in result
    assert "!" not in result
    assert "/" not in result


def test_normalize_preserves_hyphens():
    """Test that hyphens are preserved."""
    result = normalize_title("KXFEDCUT-26JUN")
    assert "-" in result


def test_cache_roundtrip():
    """Test saving and loading the matched pairs cache."""
    cache = MatchedPairsCache(
        version=1,
        updated_at="2026-04-10T00:00:00Z",
        pairs=[
            MatchedPair(
                poly_market_id="p1",
                poly_title="Will X?",
                kalshi_market_id="k1",
                kalshi_title="KX-EVENT",
                confidence=0.92,
                source="embedding+llm",
                matched_at="2026-04-10T00:00:00Z",
                poly_yes_outcome_id="py1",
                poly_no_outcome_id="pn1",
                kalshi_yes_outcome_id="ky1",
                kalshi_no_outcome_id="kn1",
            )
        ],
        rejected=["p2::k2"],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "matched_pairs.json"
        save_cache(cache, path)

        # Verify file exists and is valid JSON
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data["pairs"]) == 1
        assert data["pairs"][0]["poly_market_id"] == "p1"
        assert data["rejected"] == ["p2::k2"]

        # Load back
        loaded = load_cache(path)
        assert len(loaded.pairs) == 1
        assert loaded.pairs[0].poly_market_id == "p1"
        assert loaded.pairs[0].confidence == 0.92
        assert loaded.rejected == ["p2::k2"]


def test_load_cache_missing_file():
    """Test loading cache when file doesn't exist."""
    cache = load_cache(Path("/nonexistent/path.json"))
    assert len(cache.pairs) == 0
    assert len(cache.rejected) == 0


def test_load_cache_backward_compat_missing_calibration_fields():
    """Pre-calibration cache entries should still load with default values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "old.json"
        # Simulate a cache file written before the category/resolution_date
        # fields existed. The loader should tolerate the missing keys.
        old_payload = {
            "version": 1,
            "updated_at": "2026-04-10T00:00:00Z",
            "pairs": [
                {
                    "poly_market_id": "p1",
                    "poly_title": "Will X?",
                    "kalshi_market_id": "k1",
                    "kalshi_title": "KX-EVENT",
                    "confidence": 0.92,
                    "source": "embedding+llm",
                    "matched_at": "2026-04-10T00:00:00Z",
                    "poly_yes_outcome_id": "py1",
                    "poly_no_outcome_id": "pn1",
                    "kalshi_yes_outcome_id": "ky1",
                    "kalshi_no_outcome_id": "kn1",
                }
            ],
            "rejected": [],
        }
        path.write_text(json.dumps(old_payload))
        loaded = load_cache(path)
        assert len(loaded.pairs) == 1
        assert loaded.pairs[0].category == ""
        assert loaded.pairs[0].resolution_date == ""


def test_load_cache_tolerates_unknown_keys():
    """Forward compat: unknown keys in the pair dict should be silently dropped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "newer.json"
        payload = {
            "version": 1,
            "updated_at": "2026-04-10T00:00:00Z",
            "pairs": [
                {
                    "poly_market_id": "p1",
                    "poly_title": "Will X?",
                    "kalshi_market_id": "k1",
                    "kalshi_title": "KX-EVENT",
                    "confidence": 0.92,
                    "source": "embedding+llm",
                    "matched_at": "2026-04-10T00:00:00Z",
                    "category": "politics",
                    "resolution_date": "2026-06-15T00:00:00Z",
                    # Hypothetical future field a newer version of arbscanner added.
                    "unknown_future_field": {"anything": [1, 2, 3]},
                }
            ],
            "rejected": [],
        }
        path.write_text(json.dumps(payload))
        loaded = load_cache(path)
        assert len(loaded.pairs) == 1
        assert loaded.pairs[0].category == "politics"
        assert loaded.pairs[0].resolution_date == "2026-06-15T00:00:00Z"


def test_candidate_to_matched_pair_carries_calibration_metadata():
    """Matcher conversion should propagate category + resolution_date."""
    from arbscanner.matcher import candidate_to_matched_pair
    from arbscanner.models import CandidatePair

    candidate = CandidatePair(
        poly_market_id="p1",
        poly_title="Will the Fed cut rates?",
        poly_description="",
        poly_resolution_date="2026-06-15T00:00:00Z",
        poly_yes_outcome_id="py1",
        poly_no_outcome_id="pn1",
        kalshi_market_id="k1",
        kalshi_title="KXFEDCUT-26JUN",
        kalshi_description="",
        kalshi_resolution_date="2026-06-20T00:00:00Z",
        kalshi_yes_outcome_id="ky1",
        kalshi_no_outcome_id="kn1",
        similarity=0.95,
        poly_category="Economics",
        kalshi_category="",
    )
    pair = candidate_to_matched_pair(candidate, source="embedding")
    assert pair.category == "Economics"
    # Polymarket resolution_date wins when present.
    assert pair.resolution_date == "2026-06-15T00:00:00Z"


def test_confirm_matches_llm_no_key_keeps_only_high_confidence(monkeypatch):
    """Without an ANTHROPIC_API_KEY, we keep only pairs above llm_confirm_high.

    The old behavior auto-accepted every candidate above the embedding
    threshold, which in no-key mode silently flooded the cache with
    ambiguous [0.7, 0.9) pairs that nothing could adjudicate. The new
    behavior drops that ambiguous band — you get fewer matches, but every
    match is high-confidence.
    """
    from arbscanner import matcher
    from arbscanner.models import CandidatePair

    def _cand(sim: float, poly_id: str, kalshi_id: str) -> CandidatePair:
        return CandidatePair(
            poly_market_id=poly_id,
            poly_title="x",
            poly_description="",
            poly_resolution_date="",
            poly_yes_outcome_id="py",
            poly_no_outcome_id="pn",
            kalshi_market_id=kalshi_id,
            kalshi_title="KX",
            kalshi_description="",
            kalshi_resolution_date="",
            kalshi_yes_outcome_id="ky",
            kalshi_no_outcome_id="kn",
            similarity=sim,
            poly_category="",
            kalshi_category="",
        )

    monkeypatch.setattr(matcher.settings, "anthropic_api_key", "")
    # llm_confirm_high defaults to 0.9.
    candidates = [
        _cand(0.95, "p1", "k1"),  # high-confidence, kept
        _cand(0.90, "p2", "k2"),  # exactly at the floor, kept
        _cand(0.85, "p3", "k3"),  # ambiguous band, dropped without LLM
        _cand(0.72, "p4", "k4"),  # low, dropped
    ]

    result = matcher.confirm_matches_llm(candidates)

    assert [c.poly_market_id for c, accepted in result if accepted] == ["p1", "p2"]
    assert all(accepted for _, accepted in result)


def test_candidate_to_matched_pair_prefers_non_empty_category():
    """If poly category is empty, we fall back to the kalshi category."""
    from arbscanner.matcher import candidate_to_matched_pair
    from arbscanner.models import CandidatePair

    candidate = CandidatePair(
        poly_market_id="p1",
        poly_title="x",
        poly_description="",
        poly_resolution_date="",
        poly_yes_outcome_id="py",
        poly_no_outcome_id="pn",
        kalshi_market_id="k1",
        kalshi_title="KX",
        kalshi_description="",
        kalshi_resolution_date="2026-06-20T00:00:00Z",
        kalshi_yes_outcome_id="ky",
        kalshi_no_outcome_id="kn",
        similarity=0.9,
        poly_category="",
        kalshi_category="sports",
    )
    pair = candidate_to_matched_pair(candidate, source="embedding")
    assert pair.category == "sports"
    assert pair.resolution_date == "2026-06-20T00:00:00Z"
