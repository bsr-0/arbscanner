"""Tests for the FastAPI web backend."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from arbscanner.db import get_connection, log_opportunities
from arbscanner.models import ArbOpportunity
from datetime import datetime, timedelta, timezone


def _get_test_app():
    """Create a test app with a temporary database."""
    from arbscanner.web import app

    return app


def _make_opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Test Market",
        kalshi_title="KX-TEST",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=50,
        expected_profit=5.0,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def test_get_opportunities():
    """Test the /api/opportunities endpoint."""
    app = _get_test_app()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp()])

        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/opportunities?hours=24&min_edge=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["market_title"] == "Test Market"
        assert data[0]["net_edge"] == 0.10
        conn.close()


def test_get_opportunities_filtered():
    """Test filtering opportunities by min_edge."""
    app = _get_test_app()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [
            _make_opp(net_edge=0.05, expected_profit=2.5),
            _make_opp(net_edge=0.01, expected_profit=0.5, poly_title="Small Edge"),
        ])

        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/opportunities?min_edge=0.03&hours=24")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["net_edge"] == 0.05
        conn.close()


def test_get_opportunities_timestamp_filter():
    """The `hours` query param should filter out old opportunities."""
    app = _get_test_app()
    now = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(
            conn,
            [
                _make_opp(
                    poly_title="Recent", timestamp=now - timedelta(minutes=30)
                ),
                _make_opp(
                    poly_title="Old", timestamp=now - timedelta(hours=5)
                ),
            ],
        )

        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)

        # 1 hour lookback should only return the recent one
        resp = client.get("/api/opportunities?hours=1&min_edge=0")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["market_title"] == "Recent"

        # 6 hour lookback should return both
        resp = client.get("/api/opportunities?hours=6&min_edge=0")
        data = resp.json()
        assert len(data) == 2

        conn.close()


def test_get_pairs():
    """Test the /api/pairs endpoint."""
    from arbscanner.models import MatchedPair, MatchedPairsCache

    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    mock_cache = MatchedPairsCache(
        pairs=[
            MatchedPair(
                poly_market_id="p1",
                poly_title="Will X?",
                kalshi_market_id="k1",
                kalshi_title="KX-X",
                confidence=0.9,
                source="embedding",
                matched_at="2026-04-10",
            )
        ],
        updated_at="2026-04-10",
    )

    with patch("arbscanner.web.load_cache", return_value=mock_cache):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/pairs")
        data = resp.json()
        assert data["count"] == 1
        assert data["pairs"][0]["poly_title"] == "Will X?"

    app.state.db.close()


def test_get_stats():
    """Test the /api/stats endpoint."""
    from arbscanner.models import MatchedPairsCache

    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    with patch("arbscanner.web.load_cache", return_value=MatchedPairsCache()), \
         patch("arbscanner.web.get_historical_edge_stats", return_value={"total_opportunities": 42}):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/stats")
        data = resp.json()
        assert data["total_opportunities"] == 42
        assert data["matched_pairs"] == 0

    app.state.db.close()


def test_get_calibration():
    """Test the /api/calibration endpoint."""
    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/calibration?category=politics&days_to_resolution=5&net_edge=0.01")
    data = resp.json()
    assert data["category"] == "politics"
    assert data["time_bucket"] == "0-7"
    assert "confidence_note" in data

    app.state.db.close()


def test_landing_page():
    """Test the landing page renders."""
    from arbscanner.models import MatchedPairsCache

    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    with patch("arbscanner.web.load_cache", return_value=MatchedPairsCache()), \
         patch("arbscanner.web.get_historical_edge_stats", return_value={}):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ArbScanner" in resp.text

    app.state.db.close()


def test_dashboard_page():
    """Test the dashboard page renders."""
    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "ArbScanner" in resp.text
    # Paper trading panel markup + API hookup must be present so the dashboard
    # can surface simulated positions when --paper is enabled.
    assert 'id="paper-panel"' in resp.text
    assert "/api/paper/summary" in resp.text
    assert "Paper Trading Account" in resp.text

    app.state.db.close()
