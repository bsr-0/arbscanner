"""Tests for the backtest aggregation module."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbscanner.backtest import compute_backtest_report
from arbscanner.db import get_connection, log_opportunities
from arbscanner.models import ArbOpportunity
from arbscanner.paper_trading import PaperTradingEngine


def _opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Market",
        kalshi_title="KX-M",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=50.0,
        expected_profit=5.0,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def test_empty_database_returns_zero_report(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "empty.db")
    try:
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()
    assert report.total_opportunities == 0
    assert report.hypothetical_profit == 0.0
    assert report.avg_net_edge == 0.0
    assert report.by_direction == []
    assert report.daily_pnl == []
    assert report.paper_closed_trades == 0


def test_aggregates_hypothetical_profit(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "agg.db")
    try:
        log_opportunities(
            conn,
            [
                _opp(expected_profit=10.0, net_edge=0.05),
                _opp(expected_profit=7.5, net_edge=0.03),
                _opp(expected_profit=2.5, net_edge=0.01),
            ],
        )
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    assert report.total_opportunities == 3
    assert report.hypothetical_profit == pytest.approx(20.0)
    assert report.avg_net_edge == pytest.approx(0.03)
    assert report.max_net_edge == pytest.approx(0.05)


def test_respects_hours_window(tmp_path: Path) -> None:
    """Opportunities outside the lookback window are excluded."""
    now = datetime.now(timezone.utc)
    conn = get_connection(tmp_path / "window.db")
    try:
        log_opportunities(
            conn,
            [
                _opp(expected_profit=10.0, timestamp=now - timedelta(hours=1)),
                _opp(expected_profit=99.0, timestamp=now - timedelta(days=30)),
            ],
        )
        report = compute_backtest_report(conn, hours=24)
    finally:
        conn.close()

    assert report.total_opportunities == 1
    assert report.hypothetical_profit == pytest.approx(10.0)


def test_min_edge_filter(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "minedge.db")
    try:
        log_opportunities(
            conn,
            [
                _opp(net_edge=0.005, expected_profit=1.0),
                _opp(net_edge=0.03, expected_profit=5.0),
                _opp(net_edge=0.07, expected_profit=12.0),
            ],
        )
        report = compute_backtest_report(conn, hours=168, min_edge=0.02)
    finally:
        conn.close()

    assert report.total_opportunities == 2
    assert report.hypothetical_profit == pytest.approx(17.0)


def test_by_direction_breakdown(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "dir.db")
    try:
        log_opportunities(
            conn,
            [
                _opp(direction="poly_yes_kalshi_no", expected_profit=10.0),
                _opp(direction="poly_yes_kalshi_no", expected_profit=5.0),
                _opp(direction="poly_no_kalshi_yes", expected_profit=3.0),
            ],
        )
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    assert len(report.by_direction) == 2
    # Sorted by total profit descending.
    assert report.by_direction[0].direction == "poly_yes_kalshi_no"
    assert report.by_direction[0].count == 2
    assert report.by_direction[0].total_profit == pytest.approx(15.0)
    assert report.by_direction[1].direction == "poly_no_kalshi_yes"
    assert report.by_direction[1].total_profit == pytest.approx(3.0)


def test_daily_pnl_is_cumulative(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    conn = get_connection(tmp_path / "daily.db")
    try:
        log_opportunities(
            conn,
            [
                _opp(expected_profit=5.0, timestamp=now - timedelta(days=3)),
                _opp(expected_profit=3.0, timestamp=now - timedelta(days=2)),
                _opp(expected_profit=7.0, timestamp=now - timedelta(days=1)),
            ],
        )
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    assert len(report.daily_pnl) == 3
    # Monotonically non-decreasing cumulative profit.
    cumulative = [d["cumulative_profit"] for d in report.daily_pnl]
    assert cumulative == sorted(cumulative)
    assert cumulative[-1] == pytest.approx(15.0)


def test_paper_stats_included_when_positions_exist(tmp_path: Path) -> None:
    """Paper trading realized stats surface in the backtest report."""
    db_path = tmp_path / "paper.db"
    engine = PaperTradingEngine(db_path=db_path, initial_balance=1000.0)
    opp = _opp(
        poly_price=0.40, kalshi_price=0.45, available_size=10.0, expected_profit=1.5
    )
    p1 = engine.open_position(opp)
    p2 = engine.open_position(opp)
    # One winning close, one resolved.
    engine.close_position(p1.id, poly_price=0.50, kalshi_price=0.50)
    engine.close_resolved_position(p2.id, yes_won=True)

    # Log an opportunity so the report has something to aggregate.
    conn = get_connection(db_path)
    try:
        log_opportunities(conn, [opp])
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    assert report.paper_closed_trades == 2
    assert report.paper_open_positions == 0
    assert report.paper_realized_pnl > 0
    assert report.paper_win_rate == pytest.approx(1.0)


def test_paper_stats_zero_when_table_missing(tmp_path: Path) -> None:
    """Backtest must not crash on a fresh DB with no paper_positions table."""
    conn = get_connection(tmp_path / "nopaper.db")
    try:
        log_opportunities(conn, [_opp()])
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    assert report.paper_closed_trades == 0
    assert report.paper_realized_pnl == 0.0
    assert report.paper_win_rate == 0.0
    assert report.paper_open_positions == 0


def test_as_dict_roundtrip(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "dict.db")
    try:
        log_opportunities(conn, [_opp(expected_profit=4.2)])
        report = compute_backtest_report(conn, hours=168)
    finally:
        conn.close()

    d = report.as_dict()
    assert d["total_opportunities"] == 1
    assert d["hypothetical_profit"] == pytest.approx(4.2)
    assert isinstance(d["by_direction"], list)
    assert isinstance(d["daily_pnl"], list)
