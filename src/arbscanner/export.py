"""Export scanner data to static JSON for GitHub Pages dashboard."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbscanner.config import DB_PATH, MATCHED_PAIRS_PATH
from arbscanner.db import get_connection
from arbscanner.matcher import load_cache

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent.parent / "docs" / "data.json"


def export_dashboard_data(
    hours: int = 24,
    min_edge: float = 0.0,
    limit: int = 100,
    output_path: Path | None = None,
) -> Path:
    """Export recent opportunities and stats to a static JSON file.

    Returns the path to the written file.
    """
    output_path = output_path or DEFAULT_OUTPUT
    output_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    try:
        opportunities = _fetch_opportunities(conn, hours=hours, min_edge=min_edge, limit=limit)
        stats = _fetch_stats(conn, hours=hours)
    finally:
        conn.close()

    cache = load_cache()

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matched_pairs": len(cache.pairs),
        "stats": stats,
        "opportunities": opportunities,
    }

    output_path.write_text(json.dumps(data, indent=2))
    return output_path


def _fetch_opportunities(
    conn: sqlite3.Connection,
    hours: int,
    min_edge: float,
    limit: int,
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """SELECT id, timestamp, poly_market_id, kalshi_market_id, market_title,
                  direction, gross_edge, net_edge, available_size,
                  expected_profit, poly_price, kalshi_price
           FROM opportunities
           WHERE net_edge >= ? AND timestamp >= ?
           ORDER BY expected_profit DESC
           LIMIT ?""",
        (min_edge, cutoff, limit),
    ).fetchall()

    return [
        {
            "id": r[0],
            "timestamp": r[1],
            "market_title": r[4],
            "direction": r[5],
            "gross_edge": r[6],
            "net_edge": r[7],
            "available_size": r[8],
            "expected_profit": r[9],
            "poly_price": r[10],
            "kalshi_price": r[11],
        }
        for r in rows
    ]


def _fetch_stats(conn: sqlite3.Connection, hours: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    row = conn.execute(
        """SELECT COUNT(*), COALESCE(AVG(net_edge), 0), COALESCE(MAX(net_edge), 0),
                  COALESCE(SUM(expected_profit), 0)
           FROM opportunities
           WHERE timestamp >= ?""",
        (cutoff,),
    ).fetchone()

    return {
        "total_opportunities": row[0],
        "avg_net_edge": round(row[1], 6),
        "max_net_edge": round(row[2], 6),
        "total_expected_profit": round(row[3], 4),
    }


def main() -> None:
    """CLI entry point for data export."""
    import argparse

    parser = argparse.ArgumentParser(description="Export scanner data for GitHub Pages")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window (default: 24)")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Min net edge filter")
    parser.add_argument("--limit", type=int, default=100, help="Max opportunities")
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    args = parser.parse_args()

    output = Path(args.output) if args.output else None
    path = export_dashboard_data(
        hours=args.hours, min_edge=args.min_edge, limit=args.limit, output_path=output
    )
    print(f"Exported to {path}")


if __name__ == "__main__":
    main()
