"""Backtest utilities — both historical replay and aggregate reporting.

This module combines two backtest views:

* :func:`run_backtest` — CLAUDE.md Day 5 says the scanner should "log historical
  opportunities to SQLite for backtesting later". ``run_backtest`` replays every
  logged opportunity through the paper trading engine, holding each position to
  resolution (looked up from the calibration module's ingested Parquet files),
  and answers: *if I had paper-traded every detected opportunity, would the
  scanner's detected edge survive execution?* Results are broken down by
  category so users can see which market buckets produce realized edge.

* :func:`compute_backtest_report` — An aggregate view of the opportunities
  table used by the web ``/backtest`` dashboard. For every detected opportunity
  it assumes the paper trading engine *would* have captured the full
  ``expected_profit`` reported at detection time — giving a "hypothetical PnL"
  ceiling — and then joins realized results from the ``paper_positions`` table
  for any opportunity that was actually simulated end to end.

Design notes for ``run_backtest``
---------------------------------
* **Isolated DB.** The backtest must not pollute the live ``paper_positions``
  table. ``run_backtest`` instantiates its own :class:`PaperTradingEngine`
  against a temp-file SQLite path that is cleaned up on exit.
* **Pair-agnostic resolution.** ``close_resolved_position(yes_won)`` takes the
  underlying event's outcome. If both Polymarket and Kalshi have ingested
  resolutions we prefer the Polymarket side and log a warning on disagreement.
* **Sequential compounding.** Positions open and close back-to-back, so the
  paper balance naturally restores after each close and positions never
  overlap.
* **No new dependencies.** ``pandas`` and ``pyarrow`` are already required.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from arbscanner.config import CALIBRATION_DATA_DIR, DB_PATH
from arbscanner.db import get_connection
from arbscanner.matcher import load_cache
from arbscanner.models import ArbOpportunity
from arbscanner.paper_trading import PaperTradingEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-loading helpers (run_backtest)
# ---------------------------------------------------------------------------


def load_historical_resolutions(exchange_name: str) -> dict[str, bool]:
    """Return ``{market_id -> resolved_yes}`` for an exchange's Parquet file.

    Reads ``CALIBRATION_DATA_DIR / historical_{exchange}.parquet`` (written by
    :func:`arbscanner.calibration.ingest_from_exchange`). Rows whose
    ``resolved_yes`` is missing are dropped. Returns an empty dict if the
    file does not exist.
    """
    path = CALIBRATION_DATA_DIR / f"historical_{exchange_name.lower()}.parquet"
    if not path.exists():
        logger.debug("No resolved-market Parquet at %s", path)
        return {}

    try:
        df = pd.read_parquet(path)
    except Exception:
        logger.exception("Failed to read %s", path)
        return {}

    if df.empty or "market_id" not in df.columns or "resolved_yes" not in df.columns:
        return {}

    df = df.dropna(subset=["market_id", "resolved_yes"])
    return {
        str(row["market_id"]): bool(row["resolved_yes"])
        for _, row in df.iterrows()
    }


def _load_category_index() -> dict[str, str]:
    """Build ``{poly_id::kalshi_id -> category}`` from the matched-pair cache."""
    cache = load_cache()
    index: dict[str, str] = {}
    for p in cache.pairs:
        if p.category:
            index[f"{p.poly_market_id}::{p.kalshi_market_id}"] = p.category
    return index


def _row_to_opportunity(row: tuple) -> ArbOpportunity:
    """Reconstruct an :class:`ArbOpportunity` from a SELECT row.

    The row order must match the SELECT in :func:`_fetch_opportunity_rows`.
    """
    return ArbOpportunity(
        poly_title=row[3],
        kalshi_title=row[3],
        poly_market_id=row[1],
        kalshi_market_id=row[2],
        direction=row[4],
        poly_price=row[9],
        kalshi_price=row[10],
        gross_edge=row[5],
        net_edge=row[6],
        available_size=row[7],
        expected_profit=row[8],
        timestamp=datetime.fromisoformat(row[0]),
    )


def _fetch_opportunity_rows(
    conn: sqlite3.Connection,
    start: datetime | None,
    end: datetime | None,
    min_edge: float,
) -> list[tuple]:
    """Chronological walk over the opportunities table, filtered by time + edge."""
    query = """
        SELECT timestamp, poly_market_id, kalshi_market_id, market_title,
               direction, gross_edge, net_edge, available_size,
               expected_profit, poly_price, kalshi_price
        FROM opportunities
        WHERE net_edge >= ?
          AND (? IS NULL OR timestamp >= ?)
          AND (? IS NULL OR timestamp <  ?)
        ORDER BY timestamp ASC
    """
    start_iso = start.isoformat() if start else None
    end_iso = end.isoformat() if end else None
    return conn.execute(
        query,
        (min_edge, start_iso, start_iso, end_iso, end_iso),
    ).fetchall()


# ---------------------------------------------------------------------------
# Result model (run_backtest)
# ---------------------------------------------------------------------------


@dataclass
class CategoryStats:
    """Per-category realized PnL stats rolled up across a backtest run."""

    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass
class BacktestResult:
    """Aggregate result of a single backtest run."""

    total_opportunities: int = 0
    resolved: int = 0
    unresolved: int = 0
    skipped_disagreement: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    initial_balance: float = 10000.0
    final_balance: float = 10000.0
    by_category: dict[str, CategoryStats] = field(default_factory=dict)
    start: str | None = None
    end: str | None = None

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return self.wins / closed if closed else 0.0

    @property
    def avg_pnl_per_trade(self) -> float:
        closed = self.wins + self.losses
        return self.total_pnl / closed if closed else 0.0


# ---------------------------------------------------------------------------
# Main entry point (run_backtest)
# ---------------------------------------------------------------------------


def _resolution_for(
    poly_id: str,
    kalshi_id: str,
    poly_resolutions: dict[str, bool],
    kalshi_resolutions: dict[str, bool],
) -> tuple[bool | None, bool]:
    """Resolve an opportunity's outcome from the two Parquet indexes.

    Returns ``(yes_won, disagreement)``. ``yes_won`` is ``None`` when no
    resolution is available on either side. ``disagreement`` is ``True`` iff
    both sides are present but disagree on the outcome — the caller should
    skip such opportunities.
    """
    poly_outcome = poly_resolutions.get(poly_id)
    kalshi_outcome = kalshi_resolutions.get(kalshi_id)

    if poly_outcome is None and kalshi_outcome is None:
        return None, False
    if poly_outcome is not None and kalshi_outcome is not None:
        if poly_outcome != kalshi_outcome:
            return None, True
        return poly_outcome, False
    # Exactly one side is present — use it.
    return (poly_outcome if poly_outcome is not None else kalshi_outcome), False


def run_backtest(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    min_edge: float = 0.0,
    initial_balance: float = 10000.0,
    db_path: Path | None = None,
) -> BacktestResult:
    """Replay the opportunity log against resolved-market outcomes.

    Parameters
    ----------
    start, end
        ISO 8601 bounds on the ``opportunities.timestamp`` window. Unbounded
        by default.
    min_edge
        Floor on ``net_edge`` — opportunities below this edge are ignored.
    initial_balance
        Starting balance for the simulated paper trading account.
    db_path
        Source SQLite path for the opportunity log. Defaults to
        ``arbscanner.config.DB_PATH``.

    Returns
    -------
    :class:`BacktestResult`
    """
    result = BacktestResult(
        initial_balance=initial_balance,
        final_balance=initial_balance,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
    )

    # 1. Load resolution indexes + category index up front.
    poly_resolutions = load_historical_resolutions("polymarket")
    kalshi_resolutions = load_historical_resolutions("kalshi")
    category_index = _load_category_index()

    # 2. Open the source opportunity log. Use get_connection() so the
    #    opportunities schema is created on-demand if the DB file exists
    #    but was initialized by another subsystem (e.g. paper_trading).
    source_path = db_path or DB_PATH
    source_conn = get_connection(source_path)

    # 3. Spin up an isolated paper trading engine in a temp-file DB so we
    #    never touch the live paper_positions table.
    tmp = tempfile.NamedTemporaryFile(
        prefix="arbscanner_backtest_", suffix=".db", delete=False
    )
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        engine = PaperTradingEngine(db_path=tmp_path, initial_balance=initial_balance)
        try:
            rows = _fetch_opportunity_rows(source_conn, start, end, min_edge)
            result.total_opportunities = len(rows)

            for row in rows:
                opp = _row_to_opportunity(row)
                yes_won, disagreement = _resolution_for(
                    opp.poly_market_id,
                    opp.kalshi_market_id,
                    poly_resolutions,
                    kalshi_resolutions,
                )
                if disagreement:
                    result.skipped_disagreement += 1
                    logger.warning(
                        "Skipping opportunity for pair %s::%s: Polymarket and "
                        "Kalshi resolutions disagree",
                        opp.poly_market_id,
                        opp.kalshi_market_id,
                    )
                    continue
                if yes_won is None:
                    result.unresolved += 1
                    continue

                # Open + close back-to-back (sequential compounding).
                try:
                    position = engine.open_position(opp)
                except ValueError:
                    logger.debug(
                        "Could not open backtest position for %s::%s; skipping",
                        opp.poly_market_id,
                        opp.kalshi_market_id,
                    )
                    result.unresolved += 1
                    continue

                pnl = engine.close_resolved_position(position.id, yes_won=yes_won)

                result.resolved += 1
                result.total_pnl += pnl
                if pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1

                pair_id = f"{opp.poly_market_id}::{opp.kalshi_market_id}"
                category = category_index.get(pair_id, "other")
                bucket = result.by_category.setdefault(category, CategoryStats())
                bucket.trades += 1
                bucket.total_pnl += pnl
                if pnl > 0:
                    bucket.wins += 1

            result.final_balance = float(engine.summary()["balance"])
        finally:
            engine.close()
    finally:
        source_conn.close()
        try:
            tmp_path.unlink()
        except OSError:
            logger.debug("Temp backtest DB %s already removed", tmp_path)

    return result


# ---------------------------------------------------------------------------
# Report formatting (run_backtest)
# ---------------------------------------------------------------------------


def format_backtest_report(result: BacktestResult) -> str:
    """Render a :class:`BacktestResult` as a human-readable multi-line string."""
    window = ""
    if result.start or result.end:
        window = f" ({result.start or 'unbounded'} → {result.end or 'unbounded'})"

    lines = [
        f"Backtest results{window}",
        "=" * 60,
        f"  Logged opportunities:     {result.total_opportunities}",
        f"  Resolved (replayed):      {result.resolved}",
        f"  Unresolved (skipped):     {result.unresolved}",
        f"  Disagreement (skipped):   {result.skipped_disagreement}",
        "",
        f"  Initial balance:          ${result.initial_balance:.2f}",
        f"  Final balance:            ${result.final_balance:.2f}",
        f"  Realized PnL:             ${result.total_pnl:.2f}",
        f"  Wins / Losses:            {result.wins} / {result.losses}",
        f"  Win rate:                 {result.win_rate:.1%}",
        f"  Avg PnL / trade:          ${result.avg_pnl_per_trade:.2f}",
    ]

    if result.by_category:
        lines.append("")
        lines.append("  By category:")
        lines.append(f"    {'Category':<16} {'Trades':>7} {'Wins':>6} {'Win%':>8} {'Total PnL':>12}")
        lines.append(f"    {'-' * 16} {'-' * 7} {'-' * 6} {'-' * 8} {'-' * 12}")
        for category in sorted(result.by_category):
            stats = result.by_category[category]
            lines.append(
                f"    {category:<16} {stats.trades:>7} {stats.wins:>6} "
                f"{stats.win_rate:>7.1%} ${stats.total_pnl:>10.2f}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Aggregate report (compute_backtest_report) — used by the web dashboard
# ---------------------------------------------------------------------------


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
