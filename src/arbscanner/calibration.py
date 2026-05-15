"""Calibration layer — historical accuracy analysis by category and time-to-resolution.

Provides context for each arb opportunity: is this edge likely real or noise?
Uses historical prediction market resolution data to compute calibration curves.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from arbscanner.config import CALIBRATION_DATA_DIR, DB_PATH

logger = logging.getLogger(__name__)

# Default calibration profiles based on published research on prediction market accuracy.
# These are used as fallbacks when no historical data is available.
# Format: (category, days_to_resolution_bucket) -> average mispricing in points (0-100 scale)
DEFAULT_PROFILES: dict[tuple[str, str], float] = {
    ("politics", "0-7"): 1.5,
    ("politics", "7-30"): 3.0,
    ("politics", "30-90"): 5.0,
    ("politics", "90+"): 8.0,
    ("economics", "0-7"): 2.0,
    ("economics", "7-30"): 3.5,
    ("economics", "30-90"): 5.5,
    ("economics", "90+"): 7.0,
    ("sports", "0-7"): 3.0,
    ("sports", "7-30"): 5.0,
    ("sports", "30-90"): 7.0,
    ("sports", "90+"): 10.0,
    ("entertainment", "0-7"): 4.0,
    ("entertainment", "7-30"): 6.0,
    ("entertainment", "30-90"): 8.0,
    ("entertainment", "90+"): 12.0,
    ("crypto", "0-7"): 3.5,
    ("crypto", "7-30"): 5.5,
    ("crypto", "30-90"): 7.5,
    ("crypto", "90+"): 10.0,
    ("science_tech", "0-7"): 5.0,
    ("science_tech", "7-30"): 7.0,
    ("science_tech", "30-90"): 10.0,
    ("science_tech", "90+"): 14.0,
    ("weather", "0-7"): 6.0,
    ("weather", "7-30"): 8.0,
    ("weather", "30-90"): 10.0,
    ("weather", "90+"): 12.0,
}


@dataclass
class CalibrationContext:
    """Calibration context for an arb opportunity."""

    category: str
    days_to_resolution: int | None
    time_bucket: str  # "0-7", "7-30", "30-90", "90+"
    avg_mispricing: float  # historical median mispricing in this bucket (points, 0-100)
    edge_likely_real: bool  # is the detected edge larger than typical mispricing?
    confidence_note: str  # human-readable explanation


def days_to_bucket(days: int | None) -> str:
    """Convert days to resolution into a time bucket string."""
    if days is None:
        return "90+"
    if days <= 7:
        return "0-7"
    if days <= 30:
        return "7-30"
    if days <= 90:
        return "30-90"
    return "90+"


def normalize_category(category: str | None) -> str:
    """Normalize a market category string to a canonical form."""
    if not category:
        return "other"
    cat = category.lower().strip()
    # Map common category names to canonical forms
    mapping = {
        # Politics
        "politics": "politics",
        "political": "politics",
        "election": "politics",
        "elections": "politics",
        "world elections": "politics",
        "global elections": "politics",
        "mayoral": "politics",
        "primary": "politics",
        "primaries": "politics",
        "congress": "politics",
        "house of representatives": "politics",
        "trump": "politics",
        "geopolitics": "politics",
        "approvals": "politics",
        "world": "politics",
        "iran": "politics",
        "middle east": "politics",
        "nuke": "politics",
        "canada": "politics",
        "uk": "politics",
        # Economics
        "economics": "economics",
        "economy": "economics",
        "finance": "economics",
        "financial": "economics",
        "financials": "economics",
        "fed": "economics",
        "macro": "economics",
        "powell": "economics",
        "economic policy": "economics",
        # Sports
        "sports": "sports",
        "sport": "sports",
        "football": "sports",
        "basketball": "sports",
        "baseball": "sports",
        "soccer": "sports",
        "tennis": "sports",
        "epl": "sports",
        "nfl": "sports",
        "nba": "sports",
        "mlb": "sports",
        "nhl": "sports",
        # Entertainment
        "entertainment": "entertainment",
        "pop culture": "entertainment",
        "culture": "entertainment",
        "celebrity": "entertainment",
        "celebrities": "entertainment",
        "movies": "entertainment",
        "music": "entertainment",
        "tv": "entertainment",
        "reality tv": "entertainment",
        "netflix": "entertainment",
        "top netflix": "entertainment",
        "spotify": "entertainment",
        "rotten tomatoes": "entertainment",
        "awards": "entertainment",
        "anime": "entertainment",
        "games": "entertainment",
        "cook": "entertainment",
        "taylor swift": "entertainment",
        # Crypto
        "crypto": "crypto",
        "cryptocurrency": "crypto",
        "bitcoin": "crypto",
        "ethereum": "crypto",
        # Science & Tech
        "science": "science_tech",
        "tech": "science_tech",
        "ai": "science_tech",
        "openai": "science_tech",
        "big tech": "science_tech",
        "space": "science_tech",
        "internet": "science_tech",
        "satoshi": "crypto",
        # Weather & Climate
        "weather": "weather",
        "climate": "weather",
        "natural disasters": "weather",
    }
    for key, val in mapping.items():
        if key in cat:
            return val
    return "other"


def get_calibration_context(
    category: str | None,
    resolution_date: datetime | None,
    net_edge: float,
) -> CalibrationContext:
    """Get calibration context for an opportunity.

    Uses historical calibration data if available, otherwise falls back to defaults.
    """
    cat = normalize_category(category)
    now = datetime.now(timezone.utc)

    days = None
    if resolution_date:
        if resolution_date.tzinfo is None:
            resolution_date = resolution_date.replace(tzinfo=timezone.utc)
        delta = resolution_date - now
        days = max(0, delta.days)

    bucket = days_to_bucket(days)

    # Try to load computed calibration data
    avg_mispricing = _lookup_calibration(cat, bucket)

    # Is the edge larger than typical mispricing for this category/bucket?
    edge_points = net_edge * 100  # convert to points
    edge_likely_real = edge_points > avg_mispricing

    if edge_likely_real:
        note = (
            f"This is a {cat} market "
            f"{'with ' + str(days) + ' days to resolution' if days is not None else '(resolution date unknown)'}. "
            f"Historically, the median mispricing is ~{avg_mispricing:.1f} points. "
            f"Your edge of {edge_points:.1f} points exceeds typical mispricing — likely real."
        )
    else:
        note = (
            f"This is a {cat} market "
            f"{'with ' + str(days) + ' days to resolution' if days is not None else '(resolution date unknown)'}. "
            f"Historically, the median mispricing is ~{avg_mispricing:.1f} points. "
            f"Your edge of {edge_points:.1f} points is within normal range — may be noise or execution risk."
        )

    return CalibrationContext(
        category=cat,
        days_to_resolution=days,
        time_bucket=bucket,
        avg_mispricing=avg_mispricing,
        edge_likely_real=edge_likely_real,
        confidence_note=note,
    )


def _lookup_calibration(category: str, bucket: str) -> float:
    """Look up calibration mispricing for a category/bucket.

    Tries computed data first, falls back to defaults.
    """
    # Try loading from computed calibration file
    cal_path = CALIBRATION_DATA_DIR / "calibration_curves.parquet"
    if cal_path.exists():
        try:
            df = pd.read_parquet(cal_path)
            row = df[(df["category"] == category) & (df["time_bucket"] == bucket)]
            if not row.empty:
                return float(row.iloc[0]["median_mispricing"])
        except Exception:
            logger.debug("Failed to read calibration data, using defaults")

    # Fall back to default profiles
    return DEFAULT_PROFILES.get((category, bucket), 5.0)


def compute_calibration_curves(data_path: Path) -> pd.DataFrame:
    """Compute calibration curves from historical resolution data.

    Expects a Parquet file with columns:
    - category: str
    - resolution_date: datetime
    - created_date: datetime
    - final_price: float (0-1, last price before resolution)
    - resolved_yes: bool (did the market resolve YES?)

    Returns a DataFrame with calibration curves by category x time_bucket.
    """
    df = pd.read_parquet(data_path)

    # Compute days to resolution at the time of final_price snapshot
    df["days_to_resolution"] = (
        pd.to_datetime(df["resolution_date"]) - pd.to_datetime(df["created_date"])
    ).dt.days

    df["time_bucket"] = df["days_to_resolution"].apply(
        lambda d: days_to_bucket(int(d)) if pd.notna(d) else "90+"
    )
    df["category"] = df["category"].apply(normalize_category)

    # Drop already-settled prices (final_price snapped to 0 or 1).
    # These are settlement values, not live trading prices — they carry no
    # calibration signal and massively skew the mean (ladder markets priced
    # at 0 cents that resolved YES contribute 100-point "mispricing").
    df = df[(df["final_price"] > 0.02) & (df["final_price"] < 0.98)]

    # For each market, mispricing = |final_price - actual_outcome|
    df["actual"] = df["resolved_yes"].astype(float)
    df["mispricing"] = (df["final_price"] - df["actual"]).abs() * 100  # in points

    # Aggregate by category x time_bucket
    curves = (
        df.groupby(["category", "time_bucket"])
        .agg(
            avg_mispricing=("mispricing", "mean"),
            median_mispricing=("mispricing", "median"),
            count=("mispricing", "count"),
        )
        .reset_index()
    )

    # Save for runtime lookup
    CALIBRATION_DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CALIBRATION_DATA_DIR / "calibration_curves.parquet"
    curves.to_parquet(output_path)
    logger.info("Saved calibration curves to %s (%d rows)", output_path, len(curves))

    return curves


REQUIRED_COLUMNS = {"category", "resolution_date", "created_date", "final_price", "resolved_yes"}


def _validate_schema(df: pd.DataFrame) -> None:
    """Ensure a historical dataset has the columns we need."""
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Historical dataset missing required columns: {sorted(missing)}. "
            f"Expected: {sorted(REQUIRED_COLUMNS)}"
        )


def ingest_from_url(url: str, out_path: Path | None = None) -> int:
    """Download a Parquet historical dataset from a URL.

    Validates that the downloaded file has the expected schema and saves it
    to the calibration data directory for `compute_calibration_curves` to
    process.

    Returns the number of rows in the downloaded dataset.
    """
    out_path = out_path or (CALIBRATION_DATA_DIR / "historical_raw.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading historical dataset from %s", url)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)

    # Validate schema
    df = pd.read_parquet(out_path)
    _validate_schema(df)

    logger.info("Downloaded %d rows to %s", len(df), out_path)
    return len(df)


def ingest_from_exchange(
    exchange: Any,
    exchange_name: str,
    out_path: Path | None = None,
    limit: int | None = None,
) -> int:
    """Build a historical dataset by scraping resolved markets from an exchange.

    Walks paginated markets via pmxt, filters to closed/resolved binary markets,
    and records one row per market suitable for `compute_calibration_curves`.

    This is our fallback when no external dataset (e.g. Jon Becker's) is
    available. It's lower-quality than a curated dataset because pmxt may not
    expose the final price before close for every market, but it's a starting
    point.

    Returns the number of rows written.
    """
    out_path = out_path or (CALIBRATION_DATA_DIR / f"historical_{exchange_name.lower()}.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    cursor = None
    fetched = 0

    while True:
        params: dict[str, Any] = {"limit": 100, "status": "closed"}
        if cursor:
            params["cursor"] = cursor

        try:
            result = exchange.fetch_markets_paginated(**params)
        except Exception:
            logger.exception("Error fetching closed markets from %s", exchange_name)
            break

        for market in result.data:
            if not (market.yes and market.no):
                continue
            # Check if market has resolved: either via status field or by
            # detecting price snap to 0/1 with past resolution date.
            # pmxt returns status=None for Kalshi, so we fall back to
            # price-snap + date heuristic.
            status_resolved = market.status in ("closed", "resolved", "settled")
            past_resolution = (
                market.resolution_date is not None
                and market.resolution_date < pd.Timestamp.now(tz="UTC")
            )
            price_snapped = (
                market.yes.price is not None
                and market.no.price is not None
                and market.yes.price in (0, 1, 0.0, 1.0)
                and market.no.price in (0, 1, 0.0, 1.0)
            )
            if not (status_resolved or (price_snapped and past_resolution)):
                continue
            # Determine which side won: resolved YES if yes.price is ~1.0
            resolved_yes = bool(market.yes.price and market.yes.price > 0.5)
            rows.append({
                "market_id": market.market_id,
                "category": normalize_category(market.category),
                "created_date": pd.NaT,  # pmxt may not expose this; caller can backfill
                "resolution_date": market.resolution_date,
                "final_price": market.yes.price if market.yes else None,
                "resolved_yes": resolved_yes,
                "title": market.title,
                "exchange": exchange_name.lower(),
            })
            fetched += 1
            if limit and fetched >= limit:
                break

        logger.info(
            "Fetched %d closed markets from %s (total: %d)",
            len(result.data),
            exchange_name,
            fetched,
        )

        if limit and fetched >= limit:
            break
        if not result.next_cursor:
            break
        cursor = result.next_cursor

    df = pd.DataFrame(rows)

    # Append to existing file rather than overwriting
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        if not existing.empty and "market_id" in existing.columns:
            existing_ids = set(existing["market_id"].values)
            df = df[~df["market_id"].isin(existing_ids)]
            df = pd.concat([existing, df], ignore_index=True)
            logger.info("Appended %d new markets (total: %d)", len(df) - len(existing), len(df))

    df.to_parquet(out_path)
    logger.info("Saved %d resolved markets to %s", len(df), out_path)
    return len(df)


KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def ingest_kalshi_direct(
    out_path: Path | None = None,
    limit: int | None = None,
) -> int:
    """Ingest resolved Kalshi markets by hitting their API directly.

    pmxt doesn't support Kalshi's status field or historical endpoints,
    so we query both the live and historical API tiers and merge results.
    """
    out_path = out_path or (CALIBRATION_DATA_DIR / "historical_kalshi.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []

    # Query both endpoints: historical (before cutoff) and live (after cutoff)
    endpoints = [
        f"{KALSHI_API_BASE}/historical/markets",
        f"{KALSHI_API_BASE}/markets",
    ]

    for endpoint in endpoints:
        cursor = None
        endpoint_label = "historical" if "historical" in endpoint else "live"
        params_base: dict[str, Any] = {"limit": 200}
        if "historical" not in endpoint:
            params_base["status"] = "settled"

        consecutive_errors = 0
        while True:
            params = {**params_base}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = httpx.get(endpoint, params=params, timeout=30)
                if resp.status_code == 429:
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        logger.warning("Too many 429s from Kalshi %s, stopping", endpoint_label)
                        break
                    import time
                    wait = min(2 ** consecutive_errors, 60)
                    logger.info("Rate limited by Kalshi, waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                consecutive_errors = 0
            except httpx.HTTPStatusError:
                logger.exception("Error fetching from Kalshi %s endpoint", endpoint_label)
                break
            except Exception:
                logger.exception("Error fetching from Kalshi %s endpoint", endpoint_label)
                break

            data = resp.json()
            markets = data.get("markets", [])

            for m in markets:
                ticker = m.get("ticker", "")
                # Skip multi-leg parlay markets
                if "MVE" in ticker:
                    continue
                result = m.get("result")
                if result not in ("yes", "no"):
                    continue
                # Kalshi API uses dollar-denominated fields (0.00-1.00 strings)
                last_price_str = m.get("last_price_dollars") or m.get("last_price")
                try:
                    final_price = float(last_price_str) if last_price_str is not None else None
                except (ValueError, TypeError):
                    final_price = None
                rows.append({
                    "market_id": ticker,
                    "category": normalize_category(m.get("event_ticker", "")),
                    "created_date": pd.to_datetime(m.get("open_time"), utc=True, errors="coerce"),
                    "resolution_date": pd.to_datetime(m.get("close_time"), utc=True, errors="coerce"),
                    "final_price": final_price,
                    "resolved_yes": result == "yes",
                    "title": m.get("title", ""),
                    "exchange": "kalshi",
                })

            logger.info(
                "Fetched %d markets from Kalshi %s (collected: %d)",
                len(markets), endpoint_label, len(rows),
            )

            cursor = data.get("cursor")
            if not cursor or len(markets) == 0:
                break
            if limit and len(rows) >= limit:
                break

        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break

    df = pd.DataFrame(rows)

    # Append to existing file rather than overwriting
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        if not existing.empty and "market_id" in existing.columns:
            existing_ids = set(existing["market_id"].values)
            new_only = df[~df["market_id"].isin(existing_ids)]
            logger.info("Appending %d new Kalshi markets to %d existing", len(new_only), len(existing))
            df = pd.concat([existing, new_only], ignore_index=True)

    df.to_parquet(out_path)
    logger.info("Saved %d resolved Kalshi markets to %s", len(df), out_path)
    return len(df)


# Kalshi event-ticker prefix → canonical category.  Sorted longest-first so
# more-specific prefixes (e.g. KXEURUSD) beat shorter ones (KXEUR).
_KALSHI_PREFIX_CATEGORIES: list[tuple[str, str]] = sorted(
    [
        ("KXBTC", "crypto"),
        ("KXETH", "crypto"),
        ("KXSOL", "crypto"),
        ("KXEURUSD", "economics"),
        ("KXUSDJPY", "economics"),
        ("KXGBPUSD", "economics"),
        ("KXUSDCAD", "economics"),
        ("KXAUDUSD", "economics"),
        ("KXINX", "economics"),
        ("KXNASD", "economics"),
        ("KXSP5", "economics"),
        ("KXGOLD", "economics"),
        ("KXOIL", "economics"),
        ("KXFED", "economics"),
        ("KXINFL", "economics"),
        ("KXUNRATE", "economics"),
        ("KXGDP", "economics"),
        ("KXNBA", "sports"),
        ("KXNFL", "sports"),
        ("KXMLB", "sports"),
        ("KXNHL", "sports"),
        ("KXMLS", "sports"),
        ("KXPGA", "sports"),
        ("KXUFC", "sports"),
        ("KXBOXING", "sports"),
        ("KXTENNIS", "sports"),
        ("KXDAVISCUP", "sports"),
        ("KXFORMULA", "sports"),
        ("KXNASCAR", "sports"),
        ("KXCRICKET", "sports"),
        ("KXRUGBY", "sports"),
        ("KXOLYMPIC", "sports"),
        ("KXSPOTIFY", "entertainment"),
        ("KXNETFLIX", "entertainment"),
        ("KXOSCAR", "entertainment"),
        ("KXGRAMMY", "entertainment"),
        ("KXEMMY", "entertainment"),
        ("KXACADEMY", "entertainment"),
        ("KXPRES", "politics"),
        ("KXGOV", "politics"),
        ("KXSEN", "politics"),
        ("KXHOUSE", "politics"),
        ("KXELECT", "politics"),
        ("KXUKPOL", "politics"),
        ("KXHURRICANE", "weather"),
        ("KXWEATHER", "weather"),
        ("KXTYPHOON", "weather"),
    ],
    key=lambda t: -len(t[0]),  # longest prefix first
)


def _kalshi_event_category(event_ticker: str) -> str:
    """Derive a canonical category from a Kalshi event ticker.

    Checks structured KX-prefixes first for reliable mapping, then falls
    back to the general :func:`normalize_category` keyword search.
    """
    upper = event_ticker.upper()
    for prefix, cat in _KALSHI_PREFIX_CATEGORIES:
        if upper.startswith(prefix):
            return cat
    return normalize_category(event_ticker)


def ingest_from_becker_dir(data_dir: Path, out_path: Path | None = None) -> int:
    """Build a calibration dataset from the local Becker prediction-data repo.

    Reads every Parquet file under ``data_dir/kalshi/markets/``, filters to
    resolved binary markets (non-parlay), and outputs a file with the schema
    expected by :func:`compute_calibration_curves`.

    Column mapping from Kalshi Becker schema:
    - ``ticker``       → ``market_id``
    - ``event_ticker`` → ``category`` (via :func:`normalize_category`)
    - ``open_time``    → ``created_date``
    - ``close_time``   → ``resolution_date``
    - ``last_price``   → ``final_price`` (cents ÷ 100, so 0–1)
    - ``result``       → ``resolved_yes`` (``"yes"`` → True)

    Args:
        data_dir: Root of the Becker repo (directory that contains
                  ``data/kalshi/markets/*.parquet``).
        out_path: Where to write the output Parquet.  Defaults to
                  ``<CALIBRATION_DATA_DIR>/historical_becker.parquet``.

    Returns:
        Number of resolved market rows written.

    Raises:
        FileNotFoundError: If no Parquet files are found under
                           ``data_dir/kalshi/markets/``.
    """
    out_path = out_path or (CALIBRATION_DATA_DIR / "historical_becker.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    kalshi_dir = Path(data_dir) / "data" / "kalshi" / "markets"
    files = sorted(kalshi_dir.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No Parquet files found in {kalshi_dir}")

    logger.info("Reading %d Kalshi market files from %s", len(files), kalshi_dir)

    chunks: list[pd.DataFrame] = []
    for f in files:
        df = pd.read_parquet(
            f,
            columns=["ticker", "event_ticker", "title", "result", "last_price", "open_time", "close_time"],
        )
        resolved = df[
            df["result"].isin(["yes", "no"])
            & ~df["ticker"].str.startswith("KXMVE", na=False)
        ].copy()
        if resolved.empty:
            continue

        resolved["category"] = resolved["event_ticker"].apply(_kalshi_event_category)
        resolved["final_price"] = resolved["last_price"] / 100.0
        resolved["resolved_yes"] = resolved["result"] == "yes"
        resolved["exchange"] = "kalshi"
        resolved = resolved.rename(
            columns={"ticker": "market_id", "open_time": "created_date", "close_time": "resolution_date"}
        )
        chunks.append(
            resolved[["market_id", "category", "created_date", "resolution_date", "final_price", "resolved_yes", "title", "exchange"]]
        )

    if not chunks:
        logger.warning("No resolved markets found in %s", kalshi_dir)
        return 0

    result = pd.concat(chunks, ignore_index=True)
    result.to_parquet(out_path)
    logger.info("Saved %d Becker calibration rows to %s", len(result), out_path)
    return len(result)


def get_historical_edge_stats(db_path: Path | None = None) -> dict:
    """Compute summary stats from logged opportunities in SQLite."""
    path = db_path or DB_PATH
    if not path.exists():
        return {}

    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            """SELECT
                 COUNT(*) as total,
                 AVG(net_edge) as avg_edge,
                 MAX(net_edge) as max_edge,
                 AVG(expected_profit) as avg_profit,
                 SUM(expected_profit) as total_profit
               FROM opportunities"""
        ).fetchone()

        if not rows or rows[0] == 0:
            return {}

        return {
            "total_opportunities": rows[0],
            "avg_net_edge": rows[1],
            "max_net_edge": rows[2],
            "avg_profit": rows[3],
            "total_profit": rows[4],
        }
    finally:
        conn.close()
