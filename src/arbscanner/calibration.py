"""Calibration layer — historical accuracy analysis by category and time-to-resolution.

Provides context for each arb opportunity: is this edge likely real or noise?
Uses historical prediction market resolution data to compute calibration curves.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
}


@dataclass
class CalibrationContext:
    """Calibration context for an arb opportunity."""

    category: str
    days_to_resolution: int | None
    time_bucket: str  # "0-7", "7-30", "30-90", "90+"
    avg_mispricing: float  # historical average mispricing in this bucket (points, 0-100)
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
        "politics": "politics",
        "political": "politics",
        "election": "politics",
        "elections": "politics",
        "economics": "economics",
        "economy": "economics",
        "finance": "economics",
        "financial": "economics",
        "fed": "economics",
        "macro": "economics",
        "sports": "sports",
        "sport": "sports",
        "football": "sports",
        "basketball": "sports",
        "baseball": "sports",
        "soccer": "sports",
        "entertainment": "entertainment",
        "pop culture": "entertainment",
        "culture": "entertainment",
        "celebrity": "entertainment",
        "movies": "entertainment",
        "music": "entertainment",
        "tv": "entertainment",
        "crypto": "crypto",
        "cryptocurrency": "crypto",
        "bitcoin": "crypto",
        "ethereum": "crypto",
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
            f"Historically, these are mispriced by ~{avg_mispricing:.1f} points. "
            f"Your edge of {edge_points:.1f} points exceeds typical mispricing — likely real."
        )
    else:
        note = (
            f"This is a {cat} market "
            f"{'with ' + str(days) + ' days to resolution' if days is not None else '(resolution date unknown)'}. "
            f"Historically, these are mispriced by ~{avg_mispricing:.1f} points. "
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
                return float(row.iloc[0]["avg_mispricing"])
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
