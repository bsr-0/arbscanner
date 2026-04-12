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
        # Calibration key is always present in the response payload, even when
        # the pair cache has no matching entry.
        assert "calibration" in data[0]
        conn.close()


def test_get_opportunities_enriched_with_calibration():
    """When the matched-pair cache carries category + resolution_date,
    /api/opportunities should join it onto each row as a calibration dict."""
    from datetime import timedelta
    from unittest.mock import patch

    from arbscanner.models import MatchedPair, MatchedPairsCache

    app = _get_test_app()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp(net_edge=0.08)])
        app.state.db = conn
        app.state.start_time = 0

        future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        mock_cache = MatchedPairsCache(
            pairs=[
                MatchedPair(
                    poly_market_id="poly_1",
                    poly_title="Test Market",
                    kalshi_market_id="kalshi_1",
                    kalshi_title="KX-TEST",
                    confidence=0.95,
                    source="embedding",
                    matched_at="2026-04-10T00:00:00Z",
                    category="politics",
                    resolution_date=future,
                )
            ],
        )

        with patch("arbscanner.web.load_cache", return_value=mock_cache):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/opportunities?hours=24&min_edge=0")
            data = resp.json()
            assert len(data) == 1
            cal = data[0]["calibration"]
            assert cal is not None
            assert cal["category"] == "politics"
            assert cal["time_bucket"] == "30-90"
            assert "edge_likely_real" in cal
            assert "confidence_note" in cal

        conn.close()


def test_get_opportunities_calibration_none_for_unknown_pair():
    """A logged opportunity whose pair is no longer in the cache should get
    calibration=None instead of raising."""
    app = _get_test_app()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp()])
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/opportunities?hours=24&min_edge=0")
        data = resp.json()
        assert data[0]["calibration"] is None
        conn.close()


# ---------------------------------------------------------------------------
# Free / Pro tier gating (CLAUDE.md Day 10)
# ---------------------------------------------------------------------------


def _seed_ten_opportunities(conn):
    """Helper: log 10 old opportunities with descending expected profit."""
    now = datetime.now(timezone.utc)
    opps = [
        _make_opp(
            poly_market_id=f"p{i}",
            kalshi_market_id=f"k{i}",
            poly_title=f"Market {i}",
            expected_profit=100.0 - i,
            net_edge=0.10 - i * 0.001,
            # All rows are 10 minutes old so the free-tier 5-min delay
            # window includes every row.
            timestamp=now - timedelta(minutes=10) - timedelta(seconds=i),
        )
        for i in range(10)
    ]
    log_opportunities(conn, opps)


def test_opportunities_free_tier_caps_to_top_three():
    """Free tier must return at most FREE_MAX_OPPORTUNITIES (=3) rows."""
    from arbscanner.config import FREE_MAX_OPPORTUNITIES

    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        _seed_ten_opportunities(conn)
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        # Even asking for 50, the free tier caps to 3.
        resp = client.get(
            "/api/opportunities?hours=24&min_edge=0&limit=50",
            headers={"X-Arbscanner-Tier": "free"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == FREE_MAX_OPPORTUNITIES
        # And those must be the top 3 by expected_profit (descending).
        assert data[0]["market_title"] == "Market 0"
        assert data[1]["market_title"] == "Market 1"
        assert data[2]["market_title"] == "Market 2"
        conn.close()


def test_opportunities_pro_tier_returns_full_table():
    """Pro tier returns the full un-capped result set."""
    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        _seed_ten_opportunities(conn)
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/opportunities?hours=24&min_edge=0&limit=50",
            headers={"X-Arbscanner-Tier": "pro"},
        )
        data = resp.json()
        assert len(data) == 10
        conn.close()


def test_opportunities_free_tier_five_minute_delay():
    """Free tier must hide any opportunity less than 5 minutes old."""
    app = _get_test_app()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(
            conn,
            [
                # 1 minute old — must be hidden from free tier
                _make_opp(
                    poly_market_id="fresh_p",
                    kalshi_market_id="fresh_k",
                    poly_title="Fresh market",
                    expected_profit=999.0,  # highest profit so it would sort first
                    timestamp=now - timedelta(minutes=1),
                ),
                # 10 minutes old — visible to free tier
                _make_opp(
                    poly_market_id="old_p",
                    kalshi_market_id="old_k",
                    poly_title="Old market",
                    expected_profit=1.0,
                    timestamp=now - timedelta(minutes=10),
                ),
            ],
        )
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        free_resp = client.get(
            "/api/opportunities?hours=24&min_edge=0",
            headers={"X-Arbscanner-Tier": "free"},
        )
        free_data = free_resp.json()
        titles = {o["market_title"] for o in free_data}
        assert "Old market" in titles
        assert "Fresh market" not in titles

        # Pro tier still sees both rows.
        pro_resp = client.get(
            "/api/opportunities?hours=24&min_edge=0",
            headers={"X-Arbscanner-Tier": "pro"},
        )
        pro_data = pro_resp.json()
        pro_titles = {o["market_title"] for o in pro_data}
        assert "Old market" in pro_titles
        assert "Fresh market" in pro_titles
        conn.close()


def test_opportunities_free_tier_strips_calibration():
    """Calibration context is a Pro-tier feature — free tier must get null."""
    from unittest.mock import patch

    from arbscanner.models import MatchedPair, MatchedPairsCache

    app = _get_test_app()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(
            conn,
            [
                _make_opp(
                    poly_market_id="poly_1",
                    kalshi_market_id="kalshi_1",
                    poly_title="Market",
                    timestamp=now - timedelta(minutes=10),
                )
            ],
        )
        app.state.db = conn
        app.state.start_time = 0

        future = (now + timedelta(days=60)).isoformat()
        mock_cache = MatchedPairsCache(
            pairs=[
                MatchedPair(
                    poly_market_id="poly_1",
                    poly_title="Market",
                    kalshi_market_id="kalshi_1",
                    kalshi_title="KX-EVENT",
                    confidence=0.95,
                    source="embedding",
                    matched_at=now.isoformat(),
                    category="politics",
                    resolution_date=future,
                )
            ],
        )

        with patch("arbscanner.web.load_cache", return_value=mock_cache):
            client = TestClient(app, raise_server_exceptions=False)
            free = client.get(
                "/api/opportunities?hours=24&min_edge=0",
                headers={"X-Arbscanner-Tier": "free"},
            ).json()
            pro = client.get(
                "/api/opportunities?hours=24&min_edge=0",
                headers={"X-Arbscanner-Tier": "pro"},
            ).json()

        assert free[0]["calibration"] is None
        assert pro[0]["calibration"] is not None
        assert pro[0]["calibration"]["category"] == "politics"
        conn.close()


def test_api_calibration_free_tier_returns_402():
    """Free tier should get HTTP 402 on /api/calibration."""
    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/calibration?category=politics&days_to_resolution=5&net_edge=0.01",
        headers={"X-Arbscanner-Tier": "free"},
    )
    assert resp.status_code == 402
    assert "Pro" in resp.json()["detail"]

    app.state.db.close()


def test_api_calibration_pro_tier_unchanged():
    """Pro tier should still get the full calibration response."""
    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/calibration?category=politics&days_to_resolution=5&net_edge=0.01",
        headers={"X-Arbscanner-Tier": "pro"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] == "politics"
    assert "confidence_note" in data

    app.state.db.close()


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
    # Calibration column + badge renderer must be wired into the row JS so
    # the moat is visible next to every opportunity.
    assert "<th>Calibration</th>" in resp.text
    assert "renderCalibrationBadge" in resp.text
    assert "calib-badge" in resp.text

    app.state.db.close()


def test_get_opportunities_includes_id():
    """The opportunities endpoint must expose SQLite row ids for paper trading."""
    app = _get_test_app()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp()])

        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/opportunities?hours=24&min_edge=0")
        data = resp.json()
        assert len(data) == 1
        assert "id" in data[0]
        assert isinstance(data[0]["id"], int)
        assert data[0]["id"] > 0
        conn.close()


def test_get_backtest_empty():
    """Backtest endpoint returns zeroed report on an empty database."""
    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "bt.db"
        conn = get_connection(db_path)
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/backtest?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_opportunities"] == 0
        assert data["hypothetical_profit"] == 0.0
        assert data["by_direction"] == []
        conn.close()


def test_get_backtest_with_data():
    """Backtest endpoint aggregates logged opportunities."""
    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "bt.db"
        conn = get_connection(db_path)
        log_opportunities(
            conn,
            [
                _make_opp(expected_profit=10.0, net_edge=0.05),
                _make_opp(expected_profit=5.0, net_edge=0.03),
            ],
        )
        app.state.db = conn
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/backtest?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_opportunities"] == 2
        assert data["hypothetical_profit"] == 15.0
        assert len(data["by_direction"]) == 1
        conn.close()


def test_paper_open_and_summary_endpoints():
    """Full paper trading lifecycle through the web API."""
    from arbscanner.paper_trading import PaperTradingEngine

    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "paper.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp(available_size=10.0, expected_profit=1.5)])

        app.state.db = conn
        app.state.paper_engine = PaperTradingEngine(db_path=db_path, initial_balance=1000.0)
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)

        # Look up the opportunity id we just inserted.
        opp_id = conn.execute("SELECT id FROM opportunities LIMIT 1").fetchone()[0]

        # Open a paper position.
        resp = client.post("/api/paper/open", json={"opportunity_id": opp_id})
        assert resp.status_code == 200, resp.text
        position = resp.json()
        assert position["status"] == "open"
        assert position["opportunity_id"] == opp_id
        position_id = position["id"]

        # Summary reflects the open position.
        resp = client.get("/api/paper/summary")
        data = resp.json()
        assert data["open_positions"] == 1
        assert data["total_trades"] == 1

        # Positions list returns it.
        resp = client.get("/api/paper/positions?status=open")
        data = resp.json()
        assert len(data["positions"]) == 1
        assert data["positions"][0]["id"] == position_id

        # Close at resolution.
        resp = client.post(
            f"/api/paper/close/{position_id}", json={"yes_won": True}
        )
        assert resp.status_code == 200, resp.text
        assert "realized_pnl" in resp.json()

        # Summary now shows zero open positions.
        resp = client.get("/api/paper/summary")
        data = resp.json()
        assert data["open_positions"] == 0
        assert data["total_trades"] == 1

        conn.close()


def test_paper_open_missing_opportunity_returns_404():
    """Opening a paper position against a non-existent opportunity is a 404."""
    from arbscanner.paper_trading import PaperTradingEngine

    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "paper.db"
        conn = get_connection(db_path)
        app.state.db = conn
        app.state.paper_engine = PaperTradingEngine(db_path=db_path, initial_balance=1000.0)
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/paper/open", json={"opportunity_id": 9999})
        assert resp.status_code == 404
        conn.close()


def test_paper_close_without_mode_returns_400():
    """Closing a position without either mode (resolve or mtm) is invalid."""
    from arbscanner.paper_trading import PaperTradingEngine

    app = _get_test_app()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "paper.db"
        conn = get_connection(db_path)
        log_opportunities(conn, [_make_opp(available_size=10.0)])
        opp_id = conn.execute("SELECT id FROM opportunities LIMIT 1").fetchone()[0]

        app.state.db = conn
        app.state.paper_engine = PaperTradingEngine(db_path=db_path, initial_balance=1000.0)
        app.state.start_time = 0

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/paper/open", json={"opportunity_id": opp_id})
        position_id = resp.json()["id"]

        resp = client.post(f"/api/paper/close/{position_id}", json={})
        assert resp.status_code == 400
        conn.close()


def test_backtest_page_renders():
    """The /backtest HTML page renders."""
    app = _get_test_app()
    app.state.db = sqlite3.connect(":memory:")
    app.state.start_time = 0

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/backtest")
    assert resp.status_code == 200
    assert "Backtest" in resp.text

    app.state.db.close()
