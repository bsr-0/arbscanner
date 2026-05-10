"""Export scanner data to static JSON for GitHub Pages dashboard."""

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbscanner.config import PAGES_DATA_PATH
from arbscanner.db import get_connection
from arbscanner.matcher import load_cache, normalize_title

DEFAULT_OUTPUT = PAGES_DATA_PATH


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
        rows = _fetch_opportunity_rows(conn, hours=hours, min_edge=min_edge)
    finally:
        conn.close()

    cache = load_cache()
    pair_index = {
        f"{pair.poly_market_id}::{pair.kalshi_market_id}": pair
        for pair in cache.pairs
    }
    opportunities, duplicate_rows_removed = _build_export_opportunities(
        rows, pair_index=pair_index, limit=limit
    )
    stats = _build_stats(opportunities)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matched_pairs": len(cache.pairs),
        "stats": stats,
        "diagnostics": {
            "raw_opportunities": len(rows),
            "unique_opportunities": len(opportunities),
            "duplicate_rows_removed": duplicate_rows_removed,
        },
        "opportunities": opportunities,
    }

    output_path.write_text(json.dumps(data, indent=2))
    return output_path


def _fetch_opportunity_rows(
    conn: sqlite3.Connection,
    hours: int,
    min_edge: float,
) -> list[sqlite3.Row | tuple]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        """SELECT id, timestamp, poly_market_id, kalshi_market_id, market_title,
                  direction, gross_edge, net_edge, available_size,
                  expected_profit, poly_price, kalshi_price
           FROM opportunities
           WHERE net_edge >= ? AND timestamp >= ?
           ORDER BY expected_profit DESC, timestamp DESC""",
        (min_edge, cutoff),
    ).fetchall()


def _display_title(poly_title: str, kalshi_title: str) -> str:
    if not kalshi_title or normalize_title(poly_title) == normalize_title(kalshi_title):
        return poly_title
    return f"{poly_title} / {kalshi_title}"


def _build_export_opportunities(
    rows: Iterable[tuple],
    *,
    pair_index: dict[str, object],
    limit: int,
) -> tuple[list[dict], int]:
    opportunities: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates_removed = 0

    for row in rows:
        pair_key = f"{row[2]}::{row[3]}"
        dedupe_key = (row[2], row[3], row[5])
        if dedupe_key in seen:
            duplicates_removed += 1
            continue
        seen.add(dedupe_key)

        pair = pair_index.get(pair_key)
        poly_title = pair.poly_title if pair is not None else row[4]
        kalshi_title = pair.kalshi_title if pair is not None else ""

        opportunities.append(
            {
                "id": row[0],
                "pair_key": pair_key,
                "timestamp": row[1],
                "market_title": row[4],
                "display_title": _display_title(poly_title, kalshi_title),
                "poly_market_id": row[2],
                "kalshi_market_id": row[3],
                "poly_title": poly_title,
                "kalshi_title": kalshi_title,
                "direction": row[5],
                "gross_edge": row[6],
                "net_edge": row[7],
                "available_size": row[8],
                "expected_profit": row[9],
                "poly_price": row[10],
                "kalshi_price": row[11],
                "match_confidence": pair.confidence if pair is not None else None,
                "match_source": pair.source if pair is not None else None,
            }
        )
        if len(opportunities) >= limit:
            break

    return opportunities, duplicates_removed


def _build_stats(opportunities: list[dict]) -> dict:
    if not opportunities:
        return {
            "total_opportunities": 0,
            "avg_net_edge": 0.0,
            "max_net_edge": 0.0,
            "total_expected_profit": 0.0,
        }

    avg_net_edge = sum(o["net_edge"] for o in opportunities) / len(opportunities)
    max_net_edge = max(o["net_edge"] for o in opportunities)
    total_expected_profit = sum(o["expected_profit"] for o in opportunities)

    return {
        "total_opportunities": len(opportunities),
        "avg_net_edge": round(avg_net_edge, 6),
        "max_net_edge": round(max_net_edge, 6),
        "total_expected_profit": round(total_expected_profit, 4),
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
