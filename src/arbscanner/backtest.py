"""Backtest aggregation over the historical opportunities log.

This module turns the ``opportunities`` table into a performance view of
the scanner. For every detected opportunity it assumes the paper
trading engine *would* have captured the full ``expected_profit``
reported at detection time — giving a "hypothetical PnL" ceiling — and
then joins realized results from the ``paper_positions`` table for any
opportunity that was actually simulated end to end.

The intent is twofold:

* **Marketing**: show prospective users how much the scanner's detected
  edge is worth over a rolling window.
* **Calibration feedback**: let us compare hypothetical vs. realized PnL
  on paper positions so we can tell whether detected edges survive
  contact with real execution (even simulated).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbscanner.config import DB_PATH

logger = logging.getLogger(__name__)


@dataclass
class DirectionStats:
    """Per-direction slice of the hypothetical performance summary."""

    direction: str
    count: int
    total_profit: float
    avg_net_edge: float


@dataclass
class BacktestReport:
    """Aggregate view of hypothetical + paper trading performance."""

    # Hypothetical (assuming every detected opportunity was captured at
    # expected_profit).
    hours: int
    total_opportunities: int
    hypothetical_profit: float
    avg_net_edge: float
    max_net_edge: float
    avg_expected_profit: float
    by_direction: list[DirectionStats] = field(default_factory=list)

    # Daily cumulative hypothetical PnL, newest last, for chart rendering.
    daily_pnl: list[dict] = field(default_factory=list)

    # Realized stats from paper_positions (if any).
    paper_closed_trades: int = 0
    paper_realized_pnl: float = 0.0
    paper_win_rate: float = 0.0
    paper_open_positions: int = 0

    def as_dict(self) -> dict:
        return {
            "hours": self.hours,
            "total_opportunities": self.total_opportunities,
            "hypothetical_profit": self.hypothetical_profit,
            "avg_net_edge": self.avg_net_edge,
            "max_net_edge": self.max_net_edge,
            "avg_expected_profit": self.avg_expected_profit,
            "by_direction": [
                {
                    "direction": d.direction,
                    "count": d.count,
                    "total_profit": d.total_profit,
                    "avg_net_edge": d.avg_net_edge,
                }
                for d in self.by_direction
            ],
            "daily_pnl": self.daily_pnl,
            "paper_closed_trades": self.paper_closed_trades,
            "paper_realized_pnl": self.paper_realized_pnl,
            "paper_win_rate": self.paper_win_rate,
            "paper_open_positions": self.paper_open_positions,
        }


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def compute_backtest_report(
    conn: sqlite3.Connection,
    hours: int = 168,
    min_edge: float = 0.0,
) -> BacktestReport:
    """Aggregate historical opportunities + paper positions into a report.

    ``hours`` bounds the opportunities window (default: 7 days).
    ``min_edge`` filters out opportunities below a net-edge floor, which
    is useful for isolating the "actionable" tail of the distribution
    instead of including every near-zero signal.
    """
    cutoff = _cutoff_iso(hours)

    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             COALESCE(SUM(expected_profit), 0.0) AS hypothetical,
             COALESCE(AVG(net_edge), 0.0) AS avg_edge,
             COALESCE(MAX(net_edge), 0.0) AS max_edge,
             COALESCE(AVG(expected_profit), 0.0) AS avg_profit
           FROM opportunities
           WHERE timestamp >= ? AND net_edge >= ?""",
        (cutoff, min_edge),
    ).fetchone()

    total = int(row[0] or 0)
    hypothetical = float(row[1] or 0.0)
    avg_edge = float(row[2] or 0.0)
    max_edge = float(row[3] or 0.0)
    avg_profit = float(row[4] or 0.0)

    direction_rows = conn.execute(
        """SELECT direction, COUNT(*) AS cnt,
                  COALESCE(SUM(expected_profit), 0.0) AS prof,
                  COALESCE(AVG(net_edge), 0.0) AS edge
           FROM opportunities
           WHERE timestamp >= ? AND net_edge >= ?
           GROUP BY direction
           ORDER BY prof DESC""",
        (cutoff, min_edge),
    ).fetchall()

    by_direction = [
        DirectionStats(
            direction=r[0],
            count=int(r[1]),
            total_profit=float(r[2]),
            avg_net_edge=float(r[3]),
        )
        for r in direction_rows
    ]

    daily_rows = conn.execute(
        """SELECT substr(timestamp, 1, 10) AS day,
                  COUNT(*) AS cnt,
                  COALESCE(SUM(expected_profit), 0.0) AS prof
           FROM opportunities
           WHERE timestamp >= ? AND net_edge >= ?
           GROUP BY day
           ORDER BY day ASC""",
        (cutoff, min_edge),
    ).fetchall()

    daily_pnl: list[dict] = []
    running = 0.0
    for r in daily_rows:
        running += float(r[2])
        daily_pnl.append({
            "day": r[0],
            "count": int(r[1]),
            "profit": float(r[2]),
            "cumulative_profit": running,
        })

    paper_stats = _compute_paper_stats(conn)

    report = BacktestReport(
        hours=hours,
        total_opportunities=total,
        hypothetical_profit=hypothetical,
        avg_net_edge=avg_edge,
        max_net_edge=max_edge,
        avg_expected_profit=avg_profit,
        by_direction=by_direction,
        daily_pnl=daily_pnl,
        **paper_stats,
    )
    logger.debug("Backtest report: %s", report)
    return report


def _compute_paper_stats(conn: sqlite3.Connection) -> dict:
    """Pull realized paper trading stats if the table exists.

    The paper_positions table is created lazily by PaperTradingEngine on
    first open_position(), so we can't assume it exists when the web
    backend queries from a cold database.
    """
    has_table = conn.execute(
        """SELECT name FROM sqlite_master
           WHERE type='table' AND name='paper_positions'"""
    ).fetchone()
    if not has_table:
        return {
            "paper_closed_trades": 0,
            "paper_realized_pnl": 0.0,
            "paper_win_rate": 0.0,
            "paper_open_positions": 0,
        }

    row = conn.execute(
        """SELECT
             SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_trades,
             SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_positions,
             COALESCE(
               SUM(CASE WHEN status='closed' THEN realized_pnl ELSE 0 END), 0.0
             ) AS realized_pnl,
             SUM(CASE WHEN status='closed' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
           FROM paper_positions"""
    ).fetchone()
    closed = int(row[0] or 0)
    open_positions = int(row[1] or 0)
    realized = float(row[2] or 0.0)
    wins = int(row[3] or 0)
    win_rate = (wins / closed) if closed > 0 else 0.0
    return {
        "paper_closed_trades": closed,
        "paper_realized_pnl": realized,
        "paper_win_rate": win_rate,
        "paper_open_positions": open_positions,
    }


def compute_from_path(
    db_path: Path | None = None, hours: int = 168, min_edge: float = 0.0
) -> BacktestReport:
    """Convenience wrapper that opens its own connection."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    try:
        return compute_backtest_report(conn, hours=hours, min_edge=min_edge)
    finally:
        conn.close()
