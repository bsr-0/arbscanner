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
