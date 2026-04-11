"""Backtest harness — replay logged opportunities against resolved outcomes.

CLAUDE.md Day 5 says the scanner should "log historical opportunities to SQLite
for backtesting later". This module delivers the "later": it joins the
opportunity log (``db.py``) to the resolved-market Parquet files ingested by
the calibration module (``calibration.ingest_from_exchange``) and replays every
logged opportunity through the paper trading engine, holding each position
to resolution.

The backtest answers the product thesis directly: *if I had paper-traded every
detected opportunity, would the scanner's detected edge survive execution?*
Results are broken down by category (from the matched-pair cache) so users can
see which market buckets produce realized edge.

Design notes
------------
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
from datetime import datetime
from pathlib import Path

import pandas as pd

from arbscanner.config import CALIBRATION_DATA_DIR, DB_PATH
from arbscanner.db import get_connection
from arbscanner.matcher import load_cache
from arbscanner.models import ArbOpportunity
from arbscanner.paper_trading import PaperTradingEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-loading helpers
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
# Result model
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
# Main entry point
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
# Report formatting
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
