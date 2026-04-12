"""Tests for the PaperTradingEngine."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from arbscanner.models import ArbOpportunity
from arbscanner.paper_trading import PaperTradingEngine


def _make_opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Will X happen?",
        kalshi_title="KX-X",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=100.0,
        expected_profit=10.0,
        timestamp=datetime(2026, 4, 10, 12, 0, 0),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


@pytest.fixture
def engine(tmp_path: Path) -> PaperTradingEngine:
    return PaperTradingEngine(db_path=tmp_path / "paper.db", initial_balance=1000.0)


def test_open_position_records_size_and_expected_profit(engine: PaperTradingEngine) -> None:
    opp = _make_opp(available_size=50.0, expected_profit=5.0)
    position = engine.open_position(opp)

    assert position.id > 0
    assert position.size == pytest.approx(50.0)
    assert position.expected_profit == pytest.approx(5.0)
    assert position.poly_side == "yes"
    assert position.kalshi_side == "no"
    assert position.status == "open"
    assert position.pair_id == "poly_1::kalshi_1"


def test_open_position_uses_custom_size(engine: PaperTradingEngine) -> None:
    opp = _make_opp(available_size=100.0, expected_profit=10.0)
    # Ask for half the available size; expected_profit should scale pro-rata.
    position = engine.open_position(opp, size=50.0)

    assert position.size == pytest.approx(50.0)
    assert position.expected_profit == pytest.approx(5.0)


def test_open_position_scales_down_when_over_balance(tmp_path: Path) -> None:
    """If balance can't cover the full size, it should be capped."""
    engine = PaperTradingEngine(db_path=tmp_path / "p.db", initial_balance=50.0)
    opp = _make_opp(available_size=1000.0, poly_price=0.40, kalshi_price=0.45)
    # per-contract cost = 0.85 → max affordable ≈ 58.8 contracts at $50 balance
    position = engine.open_position(opp)
    assert position.size == pytest.approx(50.0 / 0.85, rel=1e-3)


def test_open_position_rejects_zero_size(engine: PaperTradingEngine) -> None:
    with pytest.raises(ValueError):
        engine.open_position(_make_opp(available_size=0.0))


def test_close_position_mark_to_market(engine: PaperTradingEngine) -> None:
    opp = _make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0)
    position = engine.open_position(opp)

    # Close at higher prices on both legs → positive PnL.
    realized = engine.close_position(position.id, poly_price=0.50, kalshi_price=0.50)
    # (0.50-0.40) * 10 + (0.50-0.45) * 10 = 1.0 + 0.5 = 1.5
    assert realized == pytest.approx(1.5)

    summary = engine.summary()
    assert summary["total_trades"] == 1
    assert summary["open_positions"] == 0
    assert summary["total_pnl"] == pytest.approx(1.5)


def test_close_position_rejects_already_closed(engine: PaperTradingEngine) -> None:
    position = engine.open_position(_make_opp())
    engine.close_position(position.id, 0.5, 0.5)
    with pytest.raises(ValueError):
        engine.close_position(position.id, 0.5, 0.5)


def test_resolve_position_yes_wins(engine: PaperTradingEngine) -> None:
    """poly_yes_kalshi_no: if YES wins, poly leg pays $1, kalshi NO leg pays $0."""
    opp = _make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0)
    position = engine.open_position(opp)
    realized = engine.close_resolved_position(position.id, yes_won=True)
    # (1.0 - 0.40 + 0.0 - 0.45) * 10 = 0.15 * 10 = 1.5
    assert realized == pytest.approx(1.5)


def test_resolve_position_no_wins(engine: PaperTradingEngine) -> None:
    """poly_yes_kalshi_no: if NO wins, poly YES pays $0, kalshi NO pays $1."""
    opp = _make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0)
    position = engine.open_position(opp)
    realized = engine.close_resolved_position(position.id, yes_won=False)
    # (0.0 - 0.40 + 1.0 - 0.45) * 10 = 0.15 * 10 = 1.5
    assert realized == pytest.approx(1.5)


def test_arb_opportunity_pays_regardless_of_outcome(engine: PaperTradingEngine) -> None:
    """A real arb (sum < $1) pays the same positive PnL whichever side resolves."""
    opp = _make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0)
    p1 = engine.open_position(opp)
    p2 = engine.open_position(opp)

    pnl_yes = engine.close_resolved_position(p1.id, yes_won=True)
    pnl_no = engine.close_resolved_position(p2.id, yes_won=False)

    assert pnl_yes == pytest.approx(pnl_no)
    assert pnl_yes > 0


def test_summary_reports_win_rate(engine: PaperTradingEngine) -> None:
    # 2 winners, 1 loser
    p1 = engine.open_position(_make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0))
    p2 = engine.open_position(_make_opp(poly_price=0.40, kalshi_price=0.45, available_size=10.0))
    p3 = engine.open_position(
        _make_opp(poly_price=0.60, kalshi_price=0.60, available_size=10.0)
    )
    engine.close_resolved_position(p1.id, yes_won=True)
    engine.close_resolved_position(p2.id, yes_won=False)
    # p3: sum = 1.20 → either resolution loses money
    engine.close_resolved_position(p3.id, yes_won=True)

    summary = engine.summary()
    assert summary["total_trades"] == 3
    assert summary["open_positions"] == 0
    assert summary["win_rate"] == pytest.approx(2 / 3)


def test_get_open_positions_filters(engine: PaperTradingEngine) -> None:
    p1 = engine.open_position(_make_opp())
    p2 = engine.open_position(_make_opp(poly_market_id="poly_2", kalshi_market_id="kalshi_2"))
    engine.close_position(p1.id, 0.5, 0.5)

    open_positions = engine.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].id == p2.id


def test_direction_no_maps_correctly(engine: PaperTradingEngine) -> None:
    opp = _make_opp(direction="poly_no_kalshi_yes")
    position = engine.open_position(opp)
    assert position.poly_side == "no"
    assert position.kalshi_side == "yes"


def test_persistence_across_engine_instances(tmp_path: Path) -> None:
    """A position opened in one engine is visible from a fresh engine on the same DB."""
    db_path = tmp_path / "persist.db"
    engine1 = PaperTradingEngine(db_path=db_path, initial_balance=500.0)
    p = engine1.open_position(_make_opp(available_size=10.0))

    engine2 = PaperTradingEngine(db_path=db_path, initial_balance=500.0)
    reloaded = engine2.get_open_positions()
    assert len(reloaded) == 1
    assert reloaded[0].id == p.id
