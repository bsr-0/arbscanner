"""SQLite logging for historical arb opportunities."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from arbscanner.config import DB_PATH
from arbscanner.migrations import apply_migrations
from arbscanner.models import ArbOpportunity


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection and apply all pending schema migrations."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def _deserialize_calibration(raw: str | None) -> dict | None:
    """Parse a serialized calibration snapshot."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _prediction_band(opp: ArbOpportunity) -> tuple[float, float, float]:
    """Return the implied YES band and midpoint from the two leg prices."""
    if opp.direction == "poly_yes_kalshi_no":
        low = opp.poly_price
        high = 1.0 - opp.kalshi_price
    else:
        low = opp.kalshi_price
        high = 1.0 - opp.poly_price
    low = max(0.0, min(1.0, low))
    high = max(0.0, min(1.0, high))
    if low > high:
        low, high = high, low
    return low, high, (low + high) / 2.0


def _prediction_snapshot(opp: ArbOpportunity) -> tuple[float, float, float, str, str]:
    """Derive persisted prediction fields for an opportunity."""
    low, high, midpoint = _prediction_band(opp)
    fair_value = None
    if isinstance(opp.calibration, dict):
        fair_value = opp.calibration.get("fair_value")
    if isinstance(fair_value, dict) and isinstance(fair_value.get("implied_prob"), (int, float)):
        return (
            float(fair_value["implied_prob"]),
            low,
            high,
            str(fair_value.get("source") or "fair_value"),
            "original",
        )
    return midpoint, low, high, "implied_band", "original"


def _serialize_opportunity_row(opp: ArbOpportunity) -> tuple:
    """Convert an opportunity into one INSERT row with persisted snapshot fields."""
    (
        prediction_yes,
        prediction_yes_low,
        prediction_yes_high,
        prediction_source,
        prediction_origin,
    ) = _prediction_snapshot(opp)
    return (
        opp.timestamp.isoformat(),
        opp.poly_market_id,
        opp.kalshi_market_id,
        opp.poly_title,
        opp.direction,
        opp.gross_edge,
        opp.net_edge,
        opp.available_size,
        opp.expected_profit,
        opp.poly_price,
        opp.kalshi_price,
        opp.poly_title,
        opp.kalshi_title,
        opp.category or None,
        opp.resolution_date or None,
        opp.match_confidence,
        opp.match_source or None,
        prediction_yes,
        prediction_yes_low,
        prediction_yes_high,
        prediction_source,
        prediction_origin,
        json.dumps(opp.calibration, sort_keys=True) if opp.calibration is not None else None,
    )


def get_opportunity_by_id(
    conn: sqlite3.Connection, opportunity_id: int
) -> ArbOpportunity | None:
    """Fetch a single logged opportunity by id and rehydrate into ArbOpportunity.

    Returns ``None`` if the row does not exist. Used by paper trading flows
    that need to re-open a specific historical opportunity.
    """
    row = conn.execute(
        """SELECT timestamp, poly_market_id, kalshi_market_id, market_title,
                  poly_title_snapshot, kalshi_title_snapshot,
                  direction, gross_edge, net_edge, available_size,
                  expected_profit, poly_price, kalshi_price,
                  category_snapshot, resolution_date_snapshot,
                  match_confidence, match_source, calibration_json
           FROM opportunities
           WHERE id = ?""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        return None
    return ArbOpportunity(
        poly_title=row["poly_title_snapshot"] or row["market_title"],
        kalshi_title=row["kalshi_title_snapshot"] or row["market_title"],
        poly_market_id=row["poly_market_id"],
        kalshi_market_id=row["kalshi_market_id"],
        direction=row["direction"],
        poly_price=row["poly_price"],
        kalshi_price=row["kalshi_price"],
        gross_edge=row["gross_edge"],
        net_edge=row["net_edge"],
        available_size=row["available_size"],
        expected_profit=row["expected_profit"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        category=row["category_snapshot"] or "",
        resolution_date=row["resolution_date_snapshot"] or "",
        match_confidence=row["match_confidence"],
        match_source=row["match_source"] or "",
        calibration=_deserialize_calibration(row["calibration_json"]),
    )


def log_opportunities(conn: sqlite3.Connection, opportunities: list[ArbOpportunity]) -> None:
    """Insert a batch of arb opportunities into the database."""
    if not opportunities:
        return
    conn.executemany(
        """INSERT INTO opportunities
           (timestamp, poly_market_id, kalshi_market_id, market_title,
            direction, gross_edge, net_edge, available_size,
            expected_profit, poly_price, kalshi_price,
            poly_title_snapshot, kalshi_title_snapshot,
            category_snapshot, resolution_date_snapshot,
           match_confidence, match_source,
            prediction_yes, prediction_yes_low, prediction_yes_high,
            prediction_source, prediction_origin, calibration_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [_serialize_opportunity_row(opp) for opp in opportunities],
    )
    conn.commit()
