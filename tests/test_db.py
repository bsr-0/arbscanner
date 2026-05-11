"""Tests for SQLite migrations and snapshot persistence."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbscanner.db import get_connection, log_opportunities
from arbscanner.models import ArbOpportunity


def _opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Poly Title",
        kalshi_title="Kalshi Title",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.42,
        kalshi_price=0.43,
        gross_edge=0.15,
        net_edge=0.12,
        available_size=10,
        expected_profit=1.2,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def test_get_connection_applies_snapshot_migrations_to_legacy_db():
    """Opening a legacy DB should add the new snapshot columns without data loss."""
    legacy_schema = """
    CREATE TABLE opportunities (
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

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(legacy_schema)
        conn.execute(
            """INSERT INTO opportunities
               (timestamp, poly_market_id, kalshi_market_id, market_title,
                direction, gross_edge, net_edge, available_size,
                expected_profit, poly_price, kalshi_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                "poly_1",
                "kalshi_1",
                "Legacy Market",
                "poly_yes_kalshi_no",
                0.10,
                0.08,
                5.0,
                0.4,
                0.41,
                0.43,
            ),
        )
        conn.commit()
        conn.close()

        migrated = get_connection(db_path)
        try:
            columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(opportunities)").fetchall()
            }
            assert "poly_title_snapshot" in columns
            assert "prediction_yes" in columns
            assert "calibration_json" in columns
            assert migrated.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
            assert migrated.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0] >= 4
        finally:
            migrated.close()


def test_log_opportunities_persists_snapshot_fields():
    """New opportunities should persist titles, prediction fields, and calibration."""
    calibration = {
        "category": "sports",
        "time_bucket": "0-7",
        "avg_mispricing": 3.0,
        "edge_likely_real": True,
        "confidence_note": "stored snapshot",
        "fair_value": {
            "implied_prob": 0.63,
            "num_bookmakers": 4,
            "spread": 0.02,
            "source": "odds_api",
        },
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "snapshot.db"
        conn = get_connection(db_path)
        try:
            log_opportunities(
                conn,
                [
                    _opp(
                        category="sports",
                        resolution_date="2026-06-01T00:00:00+00:00",
                        match_confidence=0.91,
                        match_source="embedding+llm",
                        calibration=calibration,
                    )
                ],
            )
            row = conn.execute(
                """SELECT poly_title_snapshot, kalshi_title_snapshot,
                          category_snapshot, resolution_date_snapshot,
                          match_confidence, match_source,
                          prediction_yes, prediction_yes_low, prediction_yes_high,
                          prediction_source, prediction_origin, calibration_json
                   FROM opportunities
                   LIMIT 1"""
            ).fetchone()
            assert row["poly_title_snapshot"] == "Poly Title"
            assert row["kalshi_title_snapshot"] == "Kalshi Title"
            assert row["category_snapshot"] == "sports"
            assert row["resolution_date_snapshot"] == "2026-06-01T00:00:00+00:00"
            assert row["match_confidence"] == 0.91
            assert row["match_source"] == "embedding+llm"
            assert row["prediction_yes"] == 0.63
            assert row["prediction_yes_low"] == 0.42
            assert row["prediction_yes_high"] == pytest.approx(0.57)
            assert row["prediction_source"] == "odds_api"
            assert row["prediction_origin"] == "original"
            assert json.loads(row["calibration_json"])["confidence_note"] == "stored snapshot"
        finally:
            conn.close()
