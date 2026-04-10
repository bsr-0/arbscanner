"""Versioned schema migrations for the arbscanner SQLite database.

This module provides a lightweight, dependency-free migrations system so
schema changes can be applied to existing databases in a deterministic,
incremental order.

Design
------
- Each migration is a :class:`Migration` dataclass holding a monotonically
  increasing ``version`` number, a human-readable ``description``, and the
  ``up_sql`` DDL/DML statements to apply.
- Applied versions are tracked in a ``schema_migrations`` table containing
  one row per applied migration (``version`` and ``applied_at``).
- :func:`apply_migrations` iterates :data:`MIGRATIONS` in version order and
  applies any that have not yet been recorded. Each migration runs inside
  its own transaction: on failure, that migration is rolled back and the
  exception is re-raised so the caller can decide what to do.

Chicken-and-egg note
--------------------
The ``schema_migrations`` tracking table is itself created by Migration 3.
To avoid the bootstrap problem where we need the tracking table in order
to know whether the migration that creates the tracking table has run,
:func:`ensure_migrations_table` unconditionally creates the table (using
``CREATE TABLE IF NOT EXISTS``) before anything else. Migration 3's SQL
therefore uses ``IF NOT EXISTS`` as well and is effectively a no-op on
databases where the bootstrap has already run, but it is still recorded
in ``schema_migrations`` so the version history remains complete.

Usage
-----
    import sqlite3
    from arbscanner.migrations import apply_migrations

    conn = sqlite3.connect("arbscanner.db")
    applied = apply_migrations(conn)
    print(f"Applied migrations: {applied}")

This module depends only on the Python standard library.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """A single, ordered schema migration.

    Attributes:
        version: Monotonically increasing integer identifying this migration.
            Versions must be unique across :data:`MIGRATIONS` and are applied
            in ascending order.
        description: Short human-readable summary of what the migration does.
        up_sql: One or more SQL statements to apply the migration. Executed
            via ``sqlite3.Connection.executescript`` so multiple statements
            separated by ``;`` are supported.
    """

    version: int
    description: str
    up_sql: str


# Migration 1: create the opportunities table. DDL matches the original
# SCHEMA constant in arbscanner.db so existing databases are compatible.
_MIGRATION_1_SQL = """
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

# Migration 2: create the three indexes that arbscanner.db currently
# installs alongside the opportunities table.
_MIGRATION_2_SQL = """
CREATE INDEX IF NOT EXISTS idx_opportunities_timestamp
    ON opportunities(timestamp);
CREATE INDEX IF NOT EXISTS idx_opportunities_net_edge
    ON opportunities(net_edge);
CREATE INDEX IF NOT EXISTS idx_opportunities_profit
    ON opportunities(expected_profit);
"""

# Migration 3: create the schema_migrations tracking table. In practice
# ``ensure_migrations_table`` creates this table before any migrations run
# (chicken-and-egg bootstrap), so this statement is a no-op on most
# databases. It is retained here so the version history is complete and a
# fresh database's tracking table is formally associated with a migration.
_MIGRATION_3_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="Create opportunities table",
        up_sql=_MIGRATION_1_SQL,
    ),
    Migration(
        version=2,
        description="Create indexes on opportunities (timestamp, net_edge, expected_profit)",
        up_sql=_MIGRATION_2_SQL,
    ),
    Migration(
        version=3,
        description="Create schema_migrations tracking table",
        up_sql=_MIGRATION_3_SQL,
    ),
]


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` tracking table if it does not exist.

    This handles the bootstrap chicken-and-egg problem: the migration that
    formally creates ``schema_migrations`` is itself recorded in that same
    table. We sidestep it by unconditionally creating the table (using
    ``IF NOT EXISTS``) before any migrations run. Migration 3 then becomes
    an idempotent no-op on databases where this bootstrap has executed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration version numbers already applied."""
    ensure_migrations_table(conn)
    cursor = conn.execute("SELECT version FROM schema_migrations")
    return {int(row[0]) for row in cursor.fetchall()}


def current_version(conn: sqlite3.Connection) -> int:
    """Return the maximum applied migration version, or ``0`` if none."""
    applied = get_applied_versions(conn)
    return max(applied) if applied else 0


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in version order.

    Each migration runs inside its own transaction. On success, the migration
    is recorded in ``schema_migrations`` and the transaction is committed.
    On failure, the transaction is rolled back and the underlying exception
    is re-raised so no partial state is left behind for that migration.

    Args:
        conn: An open SQLite connection. The connection's isolation level is
            respected; migrations are wrapped in ``BEGIN``/``COMMIT`` or
            ``ROLLBACK`` explicitly so autocommit-mode connections work too.

    Returns:
        A list of migration version numbers that were applied during this
        call, in the order they were applied. If the database is already up
        to date, the returned list is empty.

    Raises:
        sqlite3.Error: Propagated from the failing migration after rollback.
    """
    ensure_migrations_table(conn)
    applied = get_applied_versions(conn)
    pending = sorted(
        (m for m in MIGRATIONS if m.version not in applied),
        key=lambda m: m.version,
    )

    if not pending:
        logger.info("No pending migrations; database is up to date")
        return []

    applied_now: list[int] = []
    for migration in pending:
        logger.info(
            "Applying migration %d: %s",
            migration.version,
            migration.description,
        )
        try:
            conn.execute("BEGIN")
            conn.executescript(migration.up_sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (migration.version, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception(
                "Migration %d failed; rolling back", migration.version
            )
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception(
                    "Rollback of migration %d also failed", migration.version
                )
            raise
        applied_now.append(migration.version)
        logger.info("Migration %d applied successfully", migration.version)

    return applied_now


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "demo.sqlite"
        demo_conn = sqlite3.connect(str(db_path))
        try:
            logger.info("Using temporary database at %s", db_path)
            logger.info("Initial version: %d", current_version(demo_conn))

            applied_versions = apply_migrations(demo_conn)
            logger.info("Applied versions: %s", applied_versions)
            logger.info(
                "Current version after first run: %d",
                current_version(demo_conn),
            )

            # Second run should be a no-op, demonstrating idempotency.
            second_run = apply_migrations(demo_conn)
            logger.info("Second run applied: %s (expected [])", second_run)

            tables = [
                row[0]
                for row in demo_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            logger.info("Tables in database: %s", tables)

            indexes = [
                row[0]
                for row in demo_conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
                ).fetchall()
            ]
            logger.info("Indexes in database: %s", indexes)
        finally:
            demo_conn.close()
