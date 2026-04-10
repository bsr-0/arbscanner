"""Paper trading engine for simulated arb execution.

This module provides a fully simulated execution layer for arb
opportunities detected by arbscanner. It lets users track expected
vs. realized edge on detected opportunities without touching real
exchange APIs or risking capital.

Positions are opened at the prices reported by an ``ArbOpportunity``,
capped by the available size on the thinner side of the book, and
persisted in a dedicated ``paper_positions`` SQLite table that is
kept separate from the live ``opportunities`` log in ``db.py``.

Closing a position may happen in two ways:

* ``close_position`` — mark-to-market close at a user-supplied pair
  of prices (useful for intraday exits or forced unwinds).
* ``close_resolved_position`` — close at final market resolution,
  where one side pays $1 per contract and the other pays $0.

The engine also tracks a simple cash balance and exposes a summary
dict for dashboards/CLI integration. It is intentionally standalone
and uses only the Python stdlib.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from arbscanner.config import DB_PATH
from arbscanner.models import ArbOpportunity

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    opened_at TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    poly_side TEXT NOT NULL,
    kalshi_side TEXT NOT NULL,
    entry_poly_price REAL NOT NULL,
    entry_kalshi_price REAL NOT NULL,
    size REAL NOT NULL,
    expected_profit REAL NOT NULL,
    status TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl REAL
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_pair_id ON paper_positions(pair_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_opened_at ON paper_positions(opened_at)",
]


@dataclass
class PaperPosition:
    """A single simulated arb position across Polymarket + Kalshi."""

    id: int
    opportunity_id: int | None
    opened_at: datetime
    pair_id: str  # "poly_market_id::kalshi_market_id"
    direction: str  # mirrors ArbOpportunity.direction
    poly_side: str  # "yes" or "no"
    kalshi_side: str  # "yes" or "no"
    entry_poly_price: float
    entry_kalshi_price: float
    size: float
    expected_profit: float
    status: str  # "open" or "closed"
    closed_at: datetime | None = None
    realized_pnl: float | None = None


@dataclass
class PaperAccount:
    """Aggregate view of the paper trading account."""

    balance: float = 10000.0
    positions: list[PaperPosition] = field(default_factory=list)
    total_pnl: float = 0.0


class PaperTradingEngine:
    """Simulated execution engine for arb opportunities.

    Maintains its own ``paper_positions`` SQLite table alongside the
    regular opportunities log. All operations are local-only; no
    exchange API calls are ever made.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        initial_balance: float = 10000.0,
    ) -> None:
        self.db_path: Path = db_path or DB_PATH
        self.initial_balance: float = initial_balance
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        for index_sql in INDEXES:
            self._conn.execute(index_sql)
        self._conn.commit()
        logger.info(
            "PaperTradingEngine initialized (db=%s, initial_balance=%.2f)",
            self.db_path,
            self.initial_balance,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sides_from_direction(direction: str) -> tuple[str, str]:
        """Derive (poly_side, kalshi_side) from an opportunity direction."""
        if direction == "poly_yes_kalshi_no":
            return "yes", "no"
        if direction == "poly_no_kalshi_yes":
            return "no", "yes"
        # Fall back to ("yes", "yes") for unknown tags; still log the action.
        logger.warning("Unknown direction %r, defaulting sides to yes/yes", direction)
        return "yes", "yes"

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PaperPosition:
        return PaperPosition(
            id=int(row["id"]),
            opportunity_id=(
                int(row["opportunity_id"]) if row["opportunity_id"] is not None else None
            ),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            pair_id=row["pair_id"],
            direction=row["direction"],
            poly_side=row["poly_side"],
            kalshi_side=row["kalshi_side"],
            entry_poly_price=float(row["entry_poly_price"]),
            entry_kalshi_price=float(row["entry_kalshi_price"]),
            size=float(row["size"]),
            expected_profit=float(row["expected_profit"]),
            status=row["status"],
            closed_at=(
                datetime.fromisoformat(row["closed_at"])
                if row["closed_at"] is not None
                else None
            ),
            realized_pnl=(
                float(row["realized_pnl"]) if row["realized_pnl"] is not None else None
            ),
        )

    def _get_position(self, position_id: int) -> PaperPosition:
        cur = self._conn.execute(
            "SELECT * FROM paper_positions WHERE id = ?", (position_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No paper position with id={position_id}")
        return self._row_to_position(row)

    def _current_balance(self) -> float:
        """Compute current cash balance from initial balance + realized PnL - open cost."""
        cur = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'closed' THEN realized_pnl ELSE 0 END), 0.0)
                    AS realized,
                COALESCE(
                    SUM(
                        CASE WHEN status = 'open'
                             THEN (entry_poly_price + entry_kalshi_price) * size
                             ELSE 0 END
                    ), 0.0
                ) AS open_cost
            FROM paper_positions
            """
        )
        row = cur.fetchone()
        realized = float(row["realized"]) if row is not None else 0.0
        open_cost = float(row["open_cost"]) if row is not None else 0.0
        return self.initial_balance + realized - open_cost

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_position(
        self,
        opp: ArbOpportunity,
        size: float | None = None,
        opportunity_id: int | None = None,
    ) -> PaperPosition:
        """Simulate opening a paper position for an arb opportunity.

        The position size is capped by ``opp.available_size``. Cost
        (poly_price + kalshi_price) * size is deducted from the balance;
        if funds are insufficient, size is reduced proportionally.
        """
        requested = size if size is not None else opp.available_size
        capped = min(requested, opp.available_size)
        if capped <= 0:
            raise ValueError(
                f"Cannot open position with non-positive size (requested={requested}, "
                f"available={opp.available_size})"
            )

        per_contract_cost = opp.poly_price + opp.kalshi_price
        balance = self._current_balance()
        max_affordable = balance / per_contract_cost if per_contract_cost > 0 else capped
        final_size = min(capped, max_affordable)
        if final_size <= 0:
            raise ValueError(
                f"Insufficient balance to open position "
                f"(balance={balance:.2f}, per_contract_cost={per_contract_cost:.4f})"
            )

        poly_side, kalshi_side = self._sides_from_direction(opp.direction)
        pair_id = f"{opp.poly_market_id}::{opp.kalshi_market_id}"
        # Scale the expected profit to the actual size executed.
        scale = final_size / opp.available_size if opp.available_size > 0 else 1.0
        expected_profit = opp.expected_profit * scale
        opened_at = datetime.now()

        cur = self._conn.execute(
            """
            INSERT INTO paper_positions (
                opportunity_id, opened_at, pair_id, direction,
                poly_side, kalshi_side,
                entry_poly_price, entry_kalshi_price, size,
                expected_profit, status, closed_at, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL)
            """,
            (
                opportunity_id,
                opened_at.isoformat(),
                pair_id,
                opp.direction,
                poly_side,
                kalshi_side,
                opp.poly_price,
                opp.kalshi_price,
                final_size,
                expected_profit,
            ),
        )
        self._conn.commit()
        position_id = int(cur.lastrowid or 0)

        logger.info(
            "Opened paper position id=%d pair=%s direction=%s size=%.4f "
            "entry=(poly=%.4f, kalshi=%.4f) expected_profit=%.4f",
            position_id,
            pair_id,
            opp.direction,
            final_size,
            opp.poly_price,
            opp.kalshi_price,
            expected_profit,
        )
        return self._get_position(position_id)

    def close_position(
        self,
        position_id: int,
        poly_price: float,
        kalshi_price: float,
    ) -> float:
        """Mark-to-market close at supplied prices. Returns realized PnL."""
        position = self._get_position(position_id)
        if position.status != "open":
            raise ValueError(
                f"Position id={position_id} is not open (status={position.status})"
            )

        # PnL per contract on each leg is (exit - entry) if we're "long" that side.
        # Paper trading treats every leg as bought at entry price for $1 payoff.
        poly_pnl = (poly_price - position.entry_poly_price) * position.size
        kalshi_pnl = (kalshi_price - position.entry_kalshi_price) * position.size
        realized_pnl = poly_pnl + kalshi_pnl
        closed_at = datetime.now()

        self._conn.execute(
            """
            UPDATE paper_positions
               SET status = 'closed',
                   closed_at = ?,
                   realized_pnl = ?
             WHERE id = ?
            """,
            (closed_at.isoformat(), realized_pnl, position_id),
        )
        self._conn.commit()

        logger.info(
            "Closed paper position id=%d at (poly=%.4f, kalshi=%.4f) "
            "realized_pnl=%.4f (expected=%.4f)",
            position_id,
            poly_price,
            kalshi_price,
            realized_pnl,
            position.expected_profit,
        )
        return realized_pnl

    def close_resolved_position(self, position_id: int, yes_won: bool) -> float:
        """Close a position at final resolution.

        ``yes_won`` describes the actual market outcome. A "yes" side
        of a leg pays $1 if yes won (else $0); a "no" side pays $1 if
        yes lost (else $0).
        """
        position = self._get_position(position_id)
        if position.status != "open":
            raise ValueError(
                f"Position id={position_id} is not open (status={position.status})"
            )

        def _payoff(side: str) -> float:
            if side == "yes":
                return 1.0 if yes_won else 0.0
            if side == "no":
                return 0.0 if yes_won else 1.0
            logger.warning("Unknown side %r, paying 0.0", side)
            return 0.0

        poly_exit = _payoff(position.poly_side)
        kalshi_exit = _payoff(position.kalshi_side)
        realized_pnl = (
            (poly_exit - position.entry_poly_price)
            + (kalshi_exit - position.entry_kalshi_price)
        ) * position.size
        closed_at = datetime.now()

        self._conn.execute(
            """
            UPDATE paper_positions
               SET status = 'closed',
                   closed_at = ?,
                   realized_pnl = ?
             WHERE id = ?
            """,
            (closed_at.isoformat(), realized_pnl, position_id),
        )
        self._conn.commit()

        logger.info(
            "Resolved paper position id=%d yes_won=%s "
            "payoffs=(poly=%.2f, kalshi=%.2f) realized_pnl=%.4f (expected=%.4f)",
            position_id,
            yes_won,
            poly_exit,
            kalshi_exit,
            realized_pnl,
            position.expected_profit,
        )
        return realized_pnl

    def get_open_positions(self) -> list[PaperPosition]:
        """Return all positions currently in the ``open`` state."""
        cur = self._conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' ORDER BY opened_at"
        )
        positions = [self._row_to_position(row) for row in cur.fetchall()]
        logger.debug("Fetched %d open paper positions", len(positions))
        return positions

    def get_account(self) -> PaperAccount:
        """Return an aggregated snapshot of the paper trading account."""
        cur = self._conn.execute(
            "SELECT * FROM paper_positions ORDER BY opened_at"
        )
        positions = [self._row_to_position(row) for row in cur.fetchall()]
        total_pnl = sum(
            p.realized_pnl for p in positions if p.realized_pnl is not None
        )
        return PaperAccount(
            balance=self._current_balance(),
            positions=positions,
            total_pnl=total_pnl,
        )

    def summary(self) -> dict:
        """Return a lightweight summary dict suitable for dashboards."""
        cur = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_positions,
                COALESCE(
                    SUM(CASE WHEN status = 'closed' THEN realized_pnl ELSE 0 END),
                    0.0
                ) AS total_pnl,
                SUM(
                    CASE WHEN status = 'closed' AND realized_pnl > 0 THEN 1 ELSE 0 END
                ) AS wins,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_trades
            FROM paper_positions
            """
        )
        row = cur.fetchone()
        total_trades = int(row["total_trades"] or 0)
        open_positions = int(row["open_positions"] or 0)
        total_pnl = float(row["total_pnl"] or 0.0)
        wins = int(row["wins"] or 0)
        closed_trades = int(row["closed_trades"] or 0)
        win_rate = (wins / closed_trades) if closed_trades > 0 else 0.0
        avg_pnl_per_trade = (total_pnl / closed_trades) if closed_trades > 0 else 0.0

        result = {
            "open_positions": open_positions,
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "balance": self._current_balance(),
            "win_rate": win_rate,
            "avg_pnl_per_trade": avg_pnl_per_trade,
        }
        logger.debug("Paper trading summary: %s", result)
        return result
