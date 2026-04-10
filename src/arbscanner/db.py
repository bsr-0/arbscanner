"""SQLite logging for historical arb opportunities."""

import sqlite3
from pathlib import Path

from arbscanner.config import DB_PATH
from arbscanner.models import ArbOpportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    poly_market_id TEXT NOT NULL,
    kalshi_market_id TEXT NOT NULL,
    market_title TEXT NOT NULL,
    direction TEXT NOT NULL,
    gross_edge REAL NOT NULL,
    net_edge REAL NOT NULL,
    available_size REAL NOT NULL,
    expected_profit REAL NOT NULL,
    poly_price REAL NOT NULL,
    kalshi_price REAL NOT NULL
);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection, creating the schema if needed."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def log_opportunities(conn: sqlite3.Connection, opportunities: list[ArbOpportunity]) -> None:
    """Insert a batch of arb opportunities into the database."""
    if not opportunities:
        return
    conn.executemany(
        """INSERT INTO opportunities
           (timestamp, poly_market_id, kalshi_market_id, market_title,
            direction, gross_edge, net_edge, available_size,
            expected_profit, poly_price, kalshi_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
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
            )
            for opp in opportunities
        ],
    )
    conn.commit()
