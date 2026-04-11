"""Market matching pipeline — find equivalent markets across Polymarket and Kalshi."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import numpy as np
from sentence_transformers import SentenceTransformer

from arbscanner.config import MATCHED_PAIRS_PATH, settings
from arbscanner.models import CandidatePair, MatchedPair, MatchedPairsCache

logger = logging.getLogger(__name__)

# Common abbreviation expansions for normalization
ABBREVIATIONS = {
    "fed": "federal reserve",
    "gdp": "gross domestic product",
    "cpi": "consumer price index",
    "ppi": "producer price index",
    "fomc": "federal open market committee",
    "scotus": "supreme court",
    "potus": "president",
    "gop": "republican",
    "dem": "democratic",
    "nfl": "national football league",
    "nba": "national basketball association",
    "mlb": "major league baseball",
    "ufc": "ultimate fighting championship",
}


def normalize_title(title: str) -> str:
    """Normalize a market title for comparison.

    Lowercases, strips leading question words, expands abbreviations,
    removes punctuation, and collapses whitespace.
    """
    text = title.lower().strip()

    # Strip leading question words
    text = re.sub(r"^(will\s+the\s+|will\s+|what\s+|who\s+|how\s+)", "", text)

    # Expand abbreviations (word-boundary aware)
    for abbr, expansion in ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(abbr)}\b", expansion, text)

    # Remove punctuation except hyphens
    text = re.sub(r"[^\w\s-]", "", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def compute_candidate_pairs(
    poly_markets: list,
    kalshi_markets: list,
    threshold: float | None = None,
) -> list[CandidatePair]:
    """Use sentence-transformers to find candidate market matches.

    Encodes normalized titles from both platforms, computes cosine similarity,
    and returns pairs above the threshold sorted by similarity descending.
    """
    if threshold is None:
        threshold = settings.embedding_threshold

    if not poly_markets or not kalshi_markets:
        return []

    model = SentenceTransformer(settings.embedding_model)

    poly_titles = [normalize_title(m.title) for m in poly_markets]
    kalshi_titles = [normalize_title(m.title) for m in kalshi_markets]

    logger.info(
        "Encoding %d Polymarket + %d Kalshi titles", len(poly_titles), len(kalshi_titles)
    )
    poly_embeddings = model.encode(poly_titles, normalize_embeddings=True)
    kalshi_embeddings = model.encode(kalshi_titles, normalize_embeddings=True)

    # Cosine similarity matrix (embeddings are already normalized)
    similarity_matrix = poly_embeddings @ kalshi_embeddings.T

    candidates: list[CandidatePair] = []
    for i, pm in enumerate(poly_markets):
        for j, km in enumerate(kalshi_markets):
            sim = float(similarity_matrix[i, j])
            if sim >= threshold:
                candidates.append(
                    CandidatePair(
                        poly_market_id=pm.market_id,
                        poly_title=pm.title,
                        poly_description=pm.description or "",
                        poly_resolution_date=(
                            pm.resolution_date.isoformat() if pm.resolution_date else ""
                        ),
                        poly_yes_outcome_id=pm.yes.outcome_id if pm.yes else "",
                        poly_no_outcome_id=pm.no.outcome_id if pm.no else "",
                        kalshi_market_id=km.market_id,
                        kalshi_title=km.title,
                        kalshi_description=km.description or "",
                        kalshi_resolution_date=(
                            km.resolution_date.isoformat() if km.resolution_date else ""
                        ),
                        kalshi_yes_outcome_id=km.yes.outcome_id if km.yes else "",
                        kalshi_no_outcome_id=km.no.outcome_id if km.no else "",
                        similarity=sim,
                        poly_category=getattr(pm, "category", "") or "",
                        kalshi_category=getattr(km, "category", "") or "",
                    )
                )

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    logger.info("Found %d candidate pairs above threshold %.2f", len(candidates), threshold)
    return candidates


def confirm_matches_llm(candidates: list[CandidatePair]) -> list[tuple[CandidatePair, bool]]:
    """Use Claude to confirm or reject ambiguous candidate pairs.

    Only sends pairs with similarity in [llm_confirm_low, llm_confirm_high).
    Pairs above llm_confirm_high are auto-accepted.
    Returns list of (candidate, accepted) tuples.
    """
    if not settings.anthropic_api_key:
        logger.warning("No ANTHROPIC_API_KEY set — auto-accepting all candidates")
        return [(c, True) for c in candidates]

    auto_accept = []
    needs_llm = []

    for c in candidates:
        if c.similarity >= settings.llm_confirm_high:
            auto_accept.append((c, True))
        elif c.similarity >= settings.llm_confirm_low:
            needs_llm.append(c)
        # below llm_confirm_low should already be filtered out by threshold

    if not needs_llm:
        return auto_accept

    # Build prompt for batch LLM confirmation
    pairs_text = ""
    for idx, c in enumerate(needs_llm):
        pairs_text += f"""
Pair {idx + 1}:
  Platform A: "{c.poly_title}"
    Description: {c.poly_description[:200] if c.poly_description else 'N/A'}
    Resolution: {c.poly_resolution_date or 'N/A'}
  Platform B: "{c.kalshi_title}"
    Description: {c.kalshi_description[:200] if c.kalshi_description else 'N/A'}
    Resolution: {c.kalshi_resolution_date or 'N/A'}
  Similarity score: {c.similarity:.3f}
"""

    prompt = f"""You are an analyst comparing prediction market listings from two different platforms.
For each pair below, determine if they refer to the SAME real-world event/question.
Two markets match if a YES on one platform and a YES on the other would resolve the same way.

{pairs_text}

Respond with a JSON array of objects, one per pair:
[{{"pair": 1, "match": true/false, "reason": "brief explanation"}}]

Only output the JSON array, nothing else."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()

    # Parse JSON response
    try:
        # Handle potential markdown code fences
        if response_text.startswith("```"):
            response_text = re.sub(r"```\w*\n?", "", response_text).strip()
        results = json.loads(response_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response: %s", response_text[:200])
        # Fall back to accepting all on parse failure
        return auto_accept + [(c, True) for c in needs_llm]

    llm_results = []
    for idx, c in enumerate(needs_llm):
        matched = True  # default accept on missing
        for r in results:
            if r.get("pair") == idx + 1:
                matched = r.get("match", True)
                if matched:
                    logger.info("LLM confirmed: '%s' <-> '%s'", c.poly_title, c.kalshi_title)
                else:
                    logger.info(
                        "LLM rejected: '%s' <-> '%s' (%s)",
                        c.poly_title,
                        c.kalshi_title,
                        r.get("reason", ""),
                    )
                break
        llm_results.append((c, matched))

    return auto_accept + llm_results


def candidate_to_matched_pair(candidate: CandidatePair, source: str) -> MatchedPair:
    """Convert a confirmed candidate to a MatchedPair.

    Carries calibration metadata (category + resolution_date) through so the
    engine can score every opportunity against the historical calibration
    curves without re-fetching market metadata at scan time. Category is
    taken from whichever side reports a non-empty value; resolution_date
    prefers the Polymarket side since Kalshi's tickers are often closer to
    the notional close time.
    """
    category = candidate.poly_category or candidate.kalshi_category or ""
    resolution_date = candidate.poly_resolution_date or candidate.kalshi_resolution_date or ""
    return MatchedPair(
        poly_market_id=candidate.poly_market_id,
        poly_title=candidate.poly_title,
        kalshi_market_id=candidate.kalshi_market_id,
        kalshi_title=candidate.kalshi_title,
        confidence=candidate.similarity,
        source=source,
        matched_at=datetime.now(timezone.utc).isoformat(),
        poly_yes_outcome_id=candidate.poly_yes_outcome_id,
        poly_no_outcome_id=candidate.poly_no_outcome_id,
        kalshi_yes_outcome_id=candidate.kalshi_yes_outcome_id,
        kalshi_no_outcome_id=candidate.kalshi_no_outcome_id,
        category=category,
        resolution_date=resolution_date,
    )


_MATCHED_PAIR_FIELDS = {f.name for f in MatchedPair.__dataclass_fields__.values()}


def _dict_to_matched_pair(p: dict) -> MatchedPair:
    """Build a MatchedPair from a dict, tolerating missing or extra keys.

    Missing fields fall back to dataclass defaults (so older cache files
    pre-dating the calibration fields still load). Unknown keys are silently
    dropped so a cache written by a newer version of arbscanner doesn't
    explode older deployments.
    """
    filtered = {k: v for k, v in p.items() if k in _MATCHED_PAIR_FIELDS}
    return MatchedPair(**filtered)


def load_cache(path: Path | None = None) -> MatchedPairsCache:
    """Load matched pairs cache from disk."""
    path = path or MATCHED_PAIRS_PATH
    if not path.exists():
        return MatchedPairsCache()
    try:
        data = json.loads(path.read_text())
        pairs = [_dict_to_matched_pair(p) for p in data.get("pairs", [])]
        return MatchedPairsCache(
            version=data.get("version", 1),
            updated_at=data.get("updated_at", ""),
            pairs=pairs,
            rejected=data.get("rejected", []),
        )
    except Exception:
        logger.exception("Failed to load cache from %s", path)
        return MatchedPairsCache()


def save_cache(cache: MatchedPairsCache, path: Path | None = None) -> None:
    """Save matched pairs cache to disk."""
    path = path or MATCHED_PAIRS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cache.updated_at = datetime.now(timezone.utc).isoformat()
    data = {
        "version": cache.version,
        "updated_at": cache.updated_at,
        "pairs": [
            {
                "poly_market_id": p.poly_market_id,
                "poly_title": p.poly_title,
                "kalshi_market_id": p.kalshi_market_id,
                "kalshi_title": p.kalshi_title,
                "confidence": p.confidence,
                "source": p.source,
                "matched_at": p.matched_at,
                "poly_yes_outcome_id": p.poly_yes_outcome_id,
                "poly_no_outcome_id": p.poly_no_outcome_id,
                "kalshi_yes_outcome_id": p.kalshi_yes_outcome_id,
                "kalshi_no_outcome_id": p.kalshi_no_outcome_id,
                "category": p.category,
                "resolution_date": p.resolution_date,
            }
            for p in cache.pairs
        ],
        "rejected": cache.rejected,
    }
    path.write_text(json.dumps(data, indent=2))
    logger.info("Saved %d matched pairs to %s", len(cache.pairs), path)


def run_matching(
    poly_markets: list,
    kalshi_markets: list,
    rematch: bool = False,
) -> MatchedPairsCache:
    """Run the full matching pipeline: normalize, embed, LLM confirm, cache.

    If rematch is False, skips markets already in the cache.
    """
    cache = MatchedPairsCache() if rematch else load_cache()

    # Build sets of already-matched market IDs
    known_poly_ids = {p.poly_market_id for p in cache.pairs}
    known_kalshi_ids = {p.kalshi_market_id for p in cache.pairs}
    rejected_set = set(cache.rejected)

    # Filter to unmatched markets
    new_poly = [m for m in poly_markets if m.market_id not in known_poly_ids]
    new_kalshi = [m for m in kalshi_markets if m.market_id not in known_kalshi_ids]

    if not new_poly and not new_kalshi:
        logger.info("No new markets to match")
        return cache

    logger.info("Matching %d new Polymarket + %d new Kalshi markets", len(new_poly), len(new_kalshi))

    # Stage 1-2: Compute embedding similarity
    candidates = compute_candidate_pairs(new_poly, new_kalshi)

    # Filter out previously rejected pairs
    candidates = [
        c
        for c in candidates
        if f"{c.poly_market_id}::{c.kalshi_market_id}" not in rejected_set
    ]

    if not candidates:
        logger.info("No new candidate pairs found")
        save_cache(cache)
        return cache

    # Stage 3: LLM confirmation
    confirmed = confirm_matches_llm(candidates)

    for candidate, accepted in confirmed:
        pair_key = f"{candidate.poly_market_id}::{candidate.kalshi_market_id}"
        if accepted:
            source = (
                "embedding"
                if candidate.similarity >= settings.llm_confirm_high
                else "embedding+llm"
            )
            cache.pairs.append(candidate_to_matched_pair(candidate, source))
        else:
            cache.rejected.append(pair_key)

    save_cache(cache)
    logger.info(
        "Matching complete: %d total pairs, %d rejected",
        len(cache.pairs),
        len(cache.rejected),
    )
    return cache
