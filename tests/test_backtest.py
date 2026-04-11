"""Tests for the `arbscanner backtest` harness."""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from arbscanner import backtest as backtest_mod
from arbscanner.backtest import (
    BacktestResult,
    CategoryStats,
    _resolution_for,
    format_backtest_report,
    load_historical_resolutions,
    run_backtest,
)
from arbscanner.db import get_connection, log_opportunities
from arbscanner.models import ArbOpportunity, MatchedPair, MatchedPairsCache
from arbscanner.paper_trading import PaperTradingEngine


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_opp(**kwargs) -> ArbOpportunity:
    """Build an ArbOpportunity with a locked-in 0.15 gross_edge."""
    defaults = dict(
        poly_title="Will the Fed cut rates?",
        kalshi_title="KXFEDCUT",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=50.0,
        expected_profit=7.5,
        timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def _write_resolution_parquet(
    calibration_dir: Path,
    exchange: str,
    rows: list[dict],
) -> None:
    """Write a historical resolutions Parquet file mirroring the schema of
    calibration.ingest_from_exchange."""
    calibration_dir.mkdir(parents=True, exist_ok=True)
    # Fill in defaults for columns the real ingest emits so the loader is
    # exercised over a realistic schema.
    defaults = {
        "category": "politics",
        "created_date": pd.NaT,
        "resolution_date": pd.Timestamp("2026-06-15", tz="UTC"),
        "final_price": 0.9,
        "title": "Test market",
        "exchange": exchange,
    }
    normalized = [{**defaults, **row} for row in rows]
    df = pd.DataFrame(normalized)
    df.to_parquet(calibration_dir / f"historical_{exchange}.parquet")


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """Per-test sandbox: temp opportunities.db, temp CALIBRATION_DATA_DIR,
    empty pair cache by default."""
    db_path = tmp_path / "arbscanner.db"
    calibration_dir = tmp_path / "calibration"
    # Redirect the backtest module's globals so the temp dir is used.
    monkeypatch.setattr(backtest_mod, "CALIBRATION_DATA_DIR", calibration_dir)
    monkeypatch.setattr(backtest_mod, "DB_PATH", db_path)
    # Default: no matched-pair cache entries.
    monkeypatch.setattr(backtest_mod, "load_cache", lambda: MatchedPairsCache())
    return {
        "db_path": db_path,
        "calibration_dir": calibration_dir,
    }


def _seed(db_path: Path, opps: list[ArbOpportunity]) -> None:
    conn = get_connection(db_path)
    try:
        log_opportunities(conn, opps)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit tests: loaders
# ---------------------------------------------------------------------------


def test_load_historical_resolutions_missing_file(tmp_env):
    # calibration dir doesn't even exist yet — must not raise.
    assert load_historical_resolutions("polymarket") == {}


def test_load_historical_resolutions_roundtrip(tmp_env):
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [
            {"market_id": "poly_1", "resolved_yes": True},
            {"market_id": "poly_2", "resolved_yes": False},
        ],
    )
    resolutions = load_historical_resolutions("polymarket")
    assert resolutions == {"poly_1": True, "poly_2": False}


def test_resolution_for_prefers_poly_when_both_agree():
    yes, disagreement = _resolution_for(
        "p", "k", {"p": True}, {"k": True}
    )
    assert yes is True
    assert disagreement is False


def test_resolution_for_flags_disagreement():
    yes, disagreement = _resolution_for(
        "p", "k", {"p": True}, {"k": False}
    )
    assert yes is None
    assert disagreement is True


def test_resolution_for_falls_back_to_kalshi():
    yes, disagreement = _resolution_for(
        "p", "k", {}, {"k": True}
    )
    assert yes is True
    assert disagreement is False


def test_resolution_for_missing_both():
    yes, disagreement = _resolution_for("p", "k", {}, {})
    assert yes is None
    assert disagreement is False


# ---------------------------------------------------------------------------
# Unit tests: run_backtest
# ---------------------------------------------------------------------------


def test_run_backtest_empty_db(tmp_env):
    result = run_backtest(db_path=tmp_env["db_path"])
    assert result.total_opportunities == 0
    assert result.resolved == 0
    assert result.unresolved == 0
    assert result.total_pnl == 0.0
    assert result.final_balance == result.initial_balance


def test_run_backtest_single_resolved_opportunity_profitable(tmp_env):
    _seed(tmp_env["db_path"], [_make_opp()])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [{"market_id": "poly_1", "resolved_yes": True}],
    )
    result = run_backtest(db_path=tmp_env["db_path"])
    assert result.total_opportunities == 1
    assert result.resolved == 1
    assert result.unresolved == 0
    # Locked-in arb → PnL == gross_edge × size regardless of outcome.
    # gross_edge=0.15, size=50 → 7.50
    assert result.total_pnl == pytest.approx(7.5)
    assert result.wins == 1
    assert result.losses == 0
    assert result.final_balance == pytest.approx(result.initial_balance + 7.5)


def test_run_backtest_locked_in_arb_pnl_is_outcome_invariant(tmp_env):
    """A well-formed arb should yield PnL == gross_edge * size whether YES
    or NO wins. This guards the core property of cross-platform arbitrage."""
    # First run: YES wins
    _seed(tmp_env["db_path"], [_make_opp()])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [{"market_id": "poly_1", "resolved_yes": True}],
    )
    result_yes = run_backtest(db_path=tmp_env["db_path"])

    # Clear everything for the NO-wins run
    tmp_env["db_path"].unlink()
    (tmp_env["calibration_dir"] / "historical_polymarket.parquet").unlink()

    _seed(tmp_env["db_path"], [_make_opp()])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [{"market_id": "poly_1", "resolved_yes": False}],
    )
    result_no = run_backtest(db_path=tmp_env["db_path"])

    assert result_yes.total_pnl == pytest.approx(result_no.total_pnl)
    assert result_yes.total_pnl == pytest.approx(7.5)


def test_run_backtest_unresolved_opportunity_skipped(tmp_env):
    """An opportunity with no matching resolution is counted as unresolved."""
    _seed(tmp_env["db_path"], [_make_opp()])
    # No Parquet files written → no resolutions available.
    result = run_backtest(db_path=tmp_env["db_path"])
    assert result.total_opportunities == 1
    assert result.resolved == 0
    assert result.unresolved == 1
    assert result.total_pnl == 0.0
    assert result.final_balance == result.initial_balance


def test_run_backtest_poly_kalshi_disagreement_logged_and_skipped(tmp_env, caplog):
    _seed(tmp_env["db_path"], [_make_opp()])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [{"market_id": "poly_1", "resolved_yes": True}],
    )
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "kalshi",
        [{"market_id": "kalshi_1", "resolved_yes": False}],
    )

    with caplog.at_level("WARNING", logger="arbscanner.backtest"):
        result = run_backtest(db_path=tmp_env["db_path"])

    assert result.total_opportunities == 1
    assert result.skipped_disagreement == 1
    assert result.resolved == 0
    assert any("disagree" in rec.message for rec in caplog.records)


def test_run_backtest_uses_kalshi_when_poly_missing(tmp_env):
    _seed(tmp_env["db_path"], [_make_opp()])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "kalshi",
        [{"market_id": "kalshi_1", "resolved_yes": True}],
    )
    result = run_backtest(db_path=tmp_env["db_path"])
    assert result.resolved == 1
    assert result.total_pnl == pytest.approx(7.5)


def test_run_backtest_time_range_filter(tmp_env):
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    opps = [
        _make_opp(
            poly_market_id="poly_early",
            kalshi_market_id="kalshi_early",
            timestamp=base,
        ),
        _make_opp(
            poly_market_id="poly_mid",
            kalshi_market_id="kalshi_mid",
            timestamp=base + timedelta(days=3),
        ),
        _make_opp(
            poly_market_id="poly_late",
            kalshi_market_id="kalshi_late",
            timestamp=base + timedelta(days=6),
        ),
    ]
    _seed(tmp_env["db_path"], opps)
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [
            {"market_id": "poly_early", "resolved_yes": True},
            {"market_id": "poly_mid", "resolved_yes": True},
            {"market_id": "poly_late", "resolved_yes": True},
        ],
    )

    # Bracket the middle opp only.
    result = run_backtest(
        db_path=tmp_env["db_path"],
        start=base + timedelta(days=2),
        end=base + timedelta(days=4),
    )
    assert result.total_opportunities == 1
    assert result.resolved == 1


def test_run_backtest_category_breakdown(tmp_env, monkeypatch):
    politics_opp = _make_opp(
        poly_market_id="poly_pol",
        kalshi_market_id="kalshi_pol",
        gross_edge=0.10,
        net_edge=0.08,
        available_size=100,
        poly_price=0.45,
        kalshi_price=0.45,
    )
    sports_opp = _make_opp(
        poly_market_id="poly_sports",
        kalshi_market_id="kalshi_sports",
        gross_edge=0.20,
        net_edge=0.17,
        available_size=50,
        poly_price=0.35,
        kalshi_price=0.45,
    )
    _seed(tmp_env["db_path"], [politics_opp, sports_opp])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [
            {"market_id": "poly_pol", "resolved_yes": True},
            {"market_id": "poly_sports", "resolved_yes": True},
        ],
    )

    mock_cache = MatchedPairsCache(
        pairs=[
            MatchedPair(
                poly_market_id="poly_pol",
                poly_title="Politics Market",
                kalshi_market_id="kalshi_pol",
                kalshi_title="KX-POL",
                confidence=0.95,
                source="embedding",
                matched_at="2026-04-10T00:00:00Z",
                category="politics",
            ),
            MatchedPair(
                poly_market_id="poly_sports",
                poly_title="Sports Market",
                kalshi_market_id="kalshi_sports",
                kalshi_title="KX-SPORTS",
                confidence=0.95,
                source="embedding",
                matched_at="2026-04-10T00:00:00Z",
                category="sports",
            ),
        ]
    )
    monkeypatch.setattr(backtest_mod, "load_cache", lambda: mock_cache)

    result = run_backtest(db_path=tmp_env["db_path"])

    assert set(result.by_category.keys()) == {"politics", "sports"}
    pol = result.by_category["politics"]
    sports = result.by_category["sports"]
    assert pol.trades == 1
    assert pol.total_pnl == pytest.approx(0.10 * 100)  # gross_edge * size = 10.0
    assert pol.wins == 1
    assert sports.trades == 1
    assert sports.total_pnl == pytest.approx(0.20 * 50)  # 10.0
    assert sports.wins == 1


def test_run_backtest_isolated_from_live_paper_db(tmp_env):
    """The backtest must not touch the live paper_positions table."""
    # Seed the live paper DB with one open position so we can detect writes.
    live_engine = PaperTradingEngine(db_path=tmp_env["db_path"], initial_balance=5000.0)
    opp = _make_opp(poly_market_id="live_p", kalshi_market_id="live_k")
    live_position = live_engine.open_position(opp)
    live_positions_before = live_engine.get_open_positions()
    live_engine.close()
    assert len(live_positions_before) == 1

    # Seed an opportunity + resolution and run the backtest.
    _seed(tmp_env["db_path"], [_make_opp(poly_market_id="bt_p", kalshi_market_id="bt_k")])
    _write_resolution_parquet(
        tmp_env["calibration_dir"],
        "polymarket",
        [{"market_id": "bt_p", "resolved_yes": True}],
    )
    result = run_backtest(db_path=tmp_env["db_path"])
    assert result.resolved == 1

    # Re-open the live engine and verify the live position is untouched.
    live_engine2 = PaperTradingEngine(db_path=tmp_env["db_path"])
    try:
        still_open = live_engine2.get_open_positions()
        assert len(still_open) == 1
        assert still_open[0].id == live_position.id
        assert still_open[0].status == "open"
    finally:
        live_engine2.close()


# ---------------------------------------------------------------------------
# Unit tests: report formatter
# ---------------------------------------------------------------------------


def test_format_backtest_report_smoke():
    result = BacktestResult(
        total_opportunities=10,
        resolved=7,
        unresolved=2,
        skipped_disagreement=1,
        total_pnl=123.45,
        wins=6,
        losses=1,
        initial_balance=10000.0,
        final_balance=10123.45,
        by_category={
            "politics": CategoryStats(trades=4, wins=4, total_pnl=80.0),
            "sports": CategoryStats(trades=3, wins=2, total_pnl=43.45),
        },
    )
    text = format_backtest_report(result)
    # Headline numbers
    assert "Logged opportunities:     10" in text
    assert "Resolved (replayed):      7" in text
    assert "Unresolved (skipped):     2" in text
    assert "Disagreement (skipped):   1" in text
    assert "$123.45" in text
    assert "85.7%" in text  # 6/7 win rate
    # Category section — the per-row PnL is right-aligned in a column, so
    # the number can be padded with spaces between the $ sign and the value.
    assert "By category" in text
    assert "politics" in text
    assert "sports" in text
    assert "80.00" in text
    assert "43.45" in text


def test_format_backtest_report_no_categories():
    """Reports render cleanly even when no category data is available."""
    result = BacktestResult(total_opportunities=0)
    text = format_backtest_report(result)
    assert "By category" not in text
    assert "0" in text


# ---------------------------------------------------------------------------
# Silence unused-import warnings from helpers imported for type checkers.
# ---------------------------------------------------------------------------

_ = (tempfile, sqlite3, patch)
