"""Tests for the paper trading engine, scan integration, and web endpoints."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from arbscanner.cli import _auto_open_paper_positions
from arbscanner.db import get_connection
from arbscanner.models import ArbOpportunity
from arbscanner.paper_trading import PaperTradingEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Will the Fed cut rates in June?",
        kalshi_title="KXFEDCUT-26JUN",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=100.0,
        expected_profit=10.0,
        timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


@pytest.fixture()
def engine():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        eng = PaperTradingEngine(db_path=db_path, initial_balance=1000.0)
        try:
            yield eng
        finally:
            eng.close()


# ---------------------------------------------------------------------------
# Engine unit tests
# ---------------------------------------------------------------------------


def test_open_position_records_row(engine):
    opp = _make_opp()
    pos = engine.open_position(opp, size=10)
    assert pos.id > 0
    assert pos.status == "open"
    assert pos.size == 10
    assert pos.poly_side == "yes"
    assert pos.kalshi_side == "no"
    assert pos.pair_id == "poly_1::kalshi_1"
    assert abs(pos.expected_profit - opp.net_edge * 10) < 1e-9


def test_open_position_respects_available_size(engine):
    opp = _make_opp(available_size=5.0)
    pos = engine.open_position(opp, size=100)
    assert pos.size == 5.0


def test_open_position_caps_by_balance():
    # Tiny balance forces sizing down.
    with tempfile.TemporaryDirectory() as tmp:
        eng = PaperTradingEngine(db_path=Path(tmp) / "p.db", initial_balance=5.0)
        try:
            opp = _make_opp(available_size=1000.0)  # cost per contract = 0.85
            pos = eng.open_position(opp)
            # 5 / 0.85 ≈ 5.88
            assert pos.size < 6.0
            assert pos.size > 5.5
        finally:
            eng.close()


def test_open_position_raises_on_zero_size(engine):
    opp = _make_opp(available_size=0.0)
    with pytest.raises(ValueError):
        engine.open_position(opp)


def test_close_position_marks_to_market(engine):
    # Entry poly=0.40, kalshi=0.45. Close both at 0.50.
    # Poly leg: (0.50-0.40)*10 = 1.0. Kalshi leg: (0.50-0.45)*10 = 0.5. Total 1.5.
    opp = _make_opp()
    pos = engine.open_position(opp, size=10)
    pnl = engine.close_position(pos.id, poly_price=0.50, kalshi_price=0.50)
    assert pnl == pytest.approx(1.5)
    reloaded = engine._get_position(pos.id)
    assert reloaded.status == "closed"
    assert reloaded.closed_at is not None


def test_close_position_rejects_already_closed(engine):
    opp = _make_opp()
    pos = engine.open_position(opp, size=10)
    engine.close_position(pos.id, 0.5, 0.5)
    with pytest.raises(ValueError):
        engine.close_position(pos.id, 0.5, 0.5)


def test_resolve_position_yes_won(engine):
    # poly=yes kalshi=no -> yes wins => poly leg pays $1, kalshi leg pays $0
    opp = _make_opp(direction="poly_yes_kalshi_no", poly_price=0.40, kalshi_price=0.45)
    pos = engine.open_position(opp, size=10)
    pnl = engine.close_resolved_position(pos.id, yes_won=True)
    # (1 - 0.4) + (0 - 0.45) = 0.15 per contract * 10 = 1.5
    assert abs(pnl - 1.5) < 1e-9


def test_resolve_position_no_won(engine):
    opp = _make_opp(direction="poly_yes_kalshi_no", poly_price=0.40, kalshi_price=0.45)
    pos = engine.open_position(opp, size=10)
    pnl = engine.close_resolved_position(pos.id, yes_won=False)
    # (0 - 0.4) + (1 - 0.45) = 0.15 per contract * 10 = 1.5
    assert abs(pnl - 1.5) < 1e-9


def test_has_open_position(engine):
    opp = _make_opp()
    assert not engine.has_open_position("poly_1::kalshi_1")
    engine.open_position(opp, size=5)
    assert engine.has_open_position("poly_1::kalshi_1")
    assert engine.has_open_position("poly_1::kalshi_1", direction="poly_yes_kalshi_no")
    assert not engine.has_open_position(
        "poly_1::kalshi_1", direction="poly_no_kalshi_yes"
    )


def test_summary_tracks_wins_and_losses(engine):
    # Entry poly=0.40, kalshi=0.45 by default.
    winning = _make_opp(poly_market_id="w", kalshi_market_id="w")
    losing = _make_opp(poly_market_id="l", kalshi_market_id="l")

    pw = engine.open_position(winning, size=10)
    pl = engine.open_position(losing, size=10)

    # Winner: +0.15 poly + +0.10 kalshi = +0.25/contract * 10 = +$2.5
    engine.close_position(pw.id, poly_price=0.55, kalshi_price=0.55)
    # Loser: -0.10 poly + -0.15 kalshi = -0.25/contract * 10 = -$2.5
    engine.close_position(pl.id, poly_price=0.30, kalshi_price=0.30)

    s = engine.summary()
    assert s["total_trades"] == 2
    assert s["open_positions"] == 0
    assert s["total_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert s["win_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Scan-loop integration (`--paper` flag)
# ---------------------------------------------------------------------------


def test_auto_open_paper_positions_opens_once_per_pair(engine):
    opp = _make_opp(net_edge=0.05)
    # First call opens the position.
    _auto_open_paper_positions(engine, [opp], threshold=0.02)
    assert len(engine.get_open_positions()) == 1

    # Second call on the same pair+direction should be a no-op.
    _auto_open_paper_positions(engine, [opp], threshold=0.02)
    assert len(engine.get_open_positions()) == 1


def test_auto_open_paper_positions_respects_threshold(engine):
    low = _make_opp(net_edge=0.005, available_size=10)
    _auto_open_paper_positions(engine, [low], threshold=0.02)
    assert len(engine.get_open_positions()) == 0


def test_auto_open_paper_positions_opens_both_directions(engine):
    a = _make_opp(direction="poly_yes_kalshi_no", net_edge=0.05)
    b = _make_opp(direction="poly_no_kalshi_yes", net_edge=0.05)
    _auto_open_paper_positions(engine, [a, b], threshold=0.02)
    open_positions = engine.get_open_positions()
    assert len(open_positions) == 2
    directions = {p.direction for p in open_positions}
    assert directions == {"poly_yes_kalshi_no", "poly_no_kalshi_yes"}


# ---------------------------------------------------------------------------
# Web API tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_client():
    from arbscanner.web import app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "arb.db"
        paper_db_path = Path(tmp) / "paper.db"
        app.state.db = get_connection(db_path)
        app.state.paper_engine = PaperTradingEngine(
            db_path=paper_db_path, initial_balance=1000.0
        )
        app.state.start_time = 0
        try:
            yield TestClient(app, raise_server_exceptions=False), app.state.paper_engine
        finally:
            app.state.db.close()
            app.state.paper_engine.close()


def test_paper_summary_endpoint_empty(web_client):
    client, _ = web_client
    resp = client.get("/api/paper/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 1000.0
    assert data["open_positions"] == 0
    assert data["total_trades"] == 0


def test_paper_positions_endpoint(web_client):
    client, engine = web_client
    engine.open_position(_make_opp(), size=10)

    resp = client.get("/api/paper/positions?status=open")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["positions"][0]["status"] == "open"
    assert data["positions"][0]["size"] == 10
    assert data["positions"][0]["pair_id"] == "poly_1::kalshi_1"


def test_paper_close_endpoint(web_client):
    client, engine = web_client
    pos = engine.open_position(_make_opp(), size=10)

    resp = client.post(
        f"/api/paper/positions/{pos.id}/close",
        json={"poly_price": 0.50, "kalshi_price": 0.50},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["position_id"] == pos.id
    assert body["realized_pnl"] == pytest.approx(1.5)

    # Second close must fail.
    resp = client.post(
        f"/api/paper/positions/{pos.id}/close",
        json={"poly_price": 0.50, "kalshi_price": 0.50},
    )
    assert resp.status_code == 404


def test_paper_resolve_endpoint(web_client):
    client, engine = web_client
    pos = engine.open_position(_make_opp(), size=10)

    resp = client.post(
        f"/api/paper/positions/{pos.id}/resolve",
        json={"yes_won": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["position_id"] == pos.id
    assert abs(body["realized_pnl"] - 1.5) < 1e-9


def test_paper_close_invalid_payload(web_client):
    client, _ = web_client
    resp = client.post(
        "/api/paper/positions/1/close",
        json={"poly_price": 1.5, "kalshi_price": 0.5},  # > 1.0
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# CLI loader
# ---------------------------------------------------------------------------


def test_load_opportunity_reconstructs_arb():
    """cli._load_opportunity should round-trip a logged opportunity."""
    from arbscanner.cli import _load_opportunity
    from arbscanner.db import log_opportunities

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = get_connection(db_path)
        opp = _make_opp()
        log_opportunities(conn, [opp])
        conn.close()

        with patch("arbscanner.cli.get_connection", return_value=get_connection(db_path)):
            loaded = _load_opportunity(1)

        assert loaded is not None
        assert loaded.direction == opp.direction
        assert loaded.net_edge == opp.net_edge
        assert loaded.poly_price == opp.poly_price
        assert loaded.kalshi_price == opp.kalshi_price


def test_load_opportunity_missing_returns_none():
    from arbscanner.cli import _load_opportunity

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "empty.db"
        conn = get_connection(db_path)
        conn.close()
        with patch("arbscanner.cli.get_connection", return_value=get_connection(db_path)):
            assert _load_opportunity(999) is None


# ---------------------------------------------------------------------------
# Terminal dashboard integration
# ---------------------------------------------------------------------------


def test_build_caption_without_paper_summary():
    from arbscanner.dashboard import _build_caption

    caption = _build_caption(
        opp_count=3,
        pairs_count=42,
        last_refresh=datetime(2026, 4, 10, 12, 34, 56, tzinfo=timezone.utc),
        paper_summary=None,
    )
    assert "Matched pairs: 42" in caption
    assert "Active opps: 3" in caption
    assert "Paper:" not in caption


def test_build_caption_with_paper_summary():
    from arbscanner.dashboard import _build_caption

    summary = {
        "balance": 10050.25,
        "open_positions": 2,
        "total_trades": 5,
        "total_pnl": 50.25,
        "win_rate": 0.60,
        "avg_pnl_per_trade": 10.05,
    }
    caption = _build_caption(
        opp_count=3,
        pairs_count=42,
        last_refresh=datetime(2026, 4, 10, 12, 34, 56, tzinfo=timezone.utc),
        paper_summary=summary,
    )
    assert "Paper:" in caption
    assert "bal=$10050.25" in caption
    assert "open=2" in caption
    assert "trades=5" in caption
    assert "pnl=$50.25" in caption
    assert "win=60.0%" in caption


def test_build_table_passes_paper_summary():
    """build_table should accept a paper_summary kwarg and render its caption."""
    from arbscanner.dashboard import build_table

    summary = {
        "balance": 9500.0,
        "open_positions": 1,
        "total_trades": 3,
        "total_pnl": -500.0,
        "win_rate": 0.33,
        "avg_pnl_per_trade": -166.67,
    }
    table = build_table(
        opportunities=[],
        matched_pairs_count=10,
        last_refresh=datetime(2026, 4, 10, tzinfo=timezone.utc),
        paper_summary=summary,
    )
    # Table.caption is a str-ish; coerce to string for the assertion.
    assert "Paper:" in str(table.caption)
    assert "bal=$9500.00" in str(table.caption)
    assert "pnl=$-500.00" in str(table.caption)


def test_run_dashboard_invokes_paper_summary_fn():
    """run_dashboard should call paper_summary_fn each cycle and pass its result."""
    from unittest.mock import MagicMock

    from arbscanner.dashboard import run_dashboard

    scan_fn = MagicMock(return_value=([], 5))
    paper_summary_fn = MagicMock(return_value={
        "balance": 1000.0,
        "open_positions": 0,
        "total_trades": 0,
        "total_pnl": 0.0,
        "win_rate": 0.0,
        "avg_pnl_per_trade": 0.0,
    })

    # After one scan, raise KeyboardInterrupt via sleep to exit the loop.
    with patch("arbscanner.dashboard.time.sleep", side_effect=KeyboardInterrupt):
        run_dashboard(scan_fn, interval=1, paper_summary_fn=paper_summary_fn)

    scan_fn.assert_called_once()
    paper_summary_fn.assert_called_once()
