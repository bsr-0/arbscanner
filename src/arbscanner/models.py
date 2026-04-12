"""Data models for arbscanner."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MatchedPair:
    """A confirmed match between a Polymarket and Kalshi market."""

    poly_market_id: str
    poly_title: str
    kalshi_market_id: str
    kalshi_title: str
    confidence: float
    source: str  # "embedding", "embedding+llm", "manual"
    matched_at: str  # ISO 8601

    # Outcome IDs for order book fetching
    poly_yes_outcome_id: str = ""
    poly_no_outcome_id: str = ""
    kalshi_yes_outcome_id: str = ""
    kalshi_no_outcome_id: str = ""

    # Calibration metadata (optional — enables the calibration layer to score
    # edges without re-fetching market metadata at scan time). Both fields
    # default to empty so older cache files that predate the calibration
    # integration still round-trip through load_cache / save_cache.
    category: str = ""
    resolution_date: str = ""  # ISO 8601, empty when unknown


@dataclass
class MatchedPairsCache:
    """Persistent cache of matched market pairs."""

    version: int = 1
    updated_at: str = ""
    pairs: list[MatchedPair] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # "poly_id::kalshi_id"


@dataclass
class CandidatePair:
    """A candidate match before LLM confirmation."""

    poly_market_id: str
    poly_title: str
    poly_description: str
    poly_resolution_date: str
    poly_yes_outcome_id: str
    poly_no_outcome_id: str
    kalshi_market_id: str
    kalshi_title: str
    kalshi_description: str
    kalshi_resolution_date: str
    kalshi_yes_outcome_id: str
    kalshi_no_outcome_id: str
    similarity: float
    poly_category: str = ""
    kalshi_category: str = ""


@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity between two matched markets."""

    poly_title: str
    kalshi_title: str
    poly_market_id: str
    kalshi_market_id: str
    direction: str  # "poly_yes_kalshi_no" or "poly_no_kalshi_yes"
    poly_price: float
    kalshi_price: float
    gross_edge: float
    net_edge: float
    available_size: float  # min liquidity on both sides (contracts)
    expected_profit: float  # net_edge * available_size
    timestamp: datetime = field(default_factory=datetime.now)

    # Calibration metadata (runtime-only enrichment; not persisted in the
    # opportunities SQLite log). Populated by the engine from the matched
    # pair's category + resolution_date at scan time.
    category: str = ""
    resolution_date: str = ""  # ISO 8601, empty when unknown
    calibration: dict | None = None  # CalibrationContext serialized as a dict
