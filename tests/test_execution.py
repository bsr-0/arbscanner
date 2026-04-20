"""Tests for the Phase A dry-run execution pipeline."""

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arbscanner.execution import (
    DEFAULT_MAX_TRADE_USD,
    EXECUTION_MODE,
    ExecutionPlan,
    ExecutionResult,
    PlanRejection,
    SimulatedOrder,
    execute_plan,
    format_execution_report,
    get_connection,
    log_execution,
    plan_execution,
)
from arbscanner.models import ArbOpportunity, MatchedPair


# ---------------------------------------------------------------------------
# Phase A invariants
# ---------------------------------------------------------------------------


def test_execution_mode_is_dry_run():
    """Phase A ships dry-run only; flipping this assertion requires a design
    review and explicit opt-in — don't silently toggle it."""
    assert EXECUTION_MODE == "dry_run"


def test_default_max_trade_usd_is_100():
    """Hard safety cap from the user's Phase A design choice."""
    assert DEFAULT_MAX_TRADE_USD == 100.0


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class MockLevel:
    price: float
    amount: float


@dataclass
class MockBook:
    bids: list
    asks: list


def _make_pair(**kwargs) -> MatchedPair:
    defaults = dict(
        poly_market_id="poly_1",
        poly_title="Will the Fed cut rates?",
        kalshi_market_id="kalshi_1",
        kalshi_title="KXFEDCUT",
        confidence=0.95,
        source="embedding",
        matched_at="2026-04-10T00:00:00Z",
        poly_yes_outcome_id="py1",
        poly_no_outcome_id="pn1",
        kalshi_yes_outcome_id="ky1",
        kalshi_no_outcome_id="kn1",
    )
    defaults.update(kwargs)
    return MatchedPair(**defaults)


def _make_opp(**kwargs) -> ArbOpportunity:
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
        available_size=100.0,
        expected_profit=10.0,
        timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def _mock_fetch_books(py_book, kn_book, bid_book=None):
    """Return a side_effect that dispatches by outcome_id."""
    book_map = {"py1": py_book, "kn1": kn_book}

    def side_effect(exchange, outcome_id):
        # If bid_book is provided and we're re-fetching py1 for the unwind,
        # return the bid-side book instead of the original ask-side book.
        if bid_book is not None and outcome_id == "py1":
            # This branch is hit on the second call for the unwind path.
            return bid_book
        return book_map.get(outcome_id)

    return side_effect


# ---------------------------------------------------------------------------
# plan_execution
# ---------------------------------------------------------------------------


def test_plan_execution_healthy_arb_produces_ready_plan():
    pair = _make_pair()
    opp = _make_opp()

    py_book = MockBook(bids=[], asks=[MockLevel(0.40, 50)])
    kn_book = MockBook(bids=[], asks=[MockLevel(0.45, 60)])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(py_book, kn_book),
    ):
        plan = plan_execution(
            opp,
            poly_exchange=MagicMock(),
            kalshi_exchange=MagicMock(),
            pair=pair,
            max_trade_usd=100.0,
        )

    assert isinstance(plan, ExecutionPlan)
    assert plan.status == "ready"
    assert plan.poly_outcome_id == "py1"
    assert plan.kalshi_outcome_id == "kn1"
    assert plan.current_poly_price == 0.40
    assert plan.current_kalshi_price == 0.45
    assert plan.per_contract_cost == pytest.approx(0.85)
    # Size capped by max_trade_usd / per_contract_cost = 100 / 0.85 ≈ 117.6,
    # but liquidity caps at min(50, 60) = 50. So size = 50.
    assert plan.size == 50
    assert plan.total_cost_usd == pytest.approx(50 * 0.85)


def test_plan_execution_caps_size_by_max_trade_usd():
    pair = _make_pair()
    opp = _make_opp(available_size=1000.0)

    py_book = MockBook(bids=[], asks=[MockLevel(0.40, 500)])
    kn_book = MockBook(bids=[], asks=[MockLevel(0.45, 500)])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(py_book, kn_book),
    ):
        plan = plan_execution(
            opp,
            MagicMock(),
            MagicMock(),
            pair=pair,
            max_trade_usd=100.0,
        )

    # per_contract_cost = 0.85, max_affordable = 100 / 0.85 ≈ 117.647
    # liquidity = 500, so cap = 117.647, floored to 117 (integer contracts).
    assert isinstance(plan, ExecutionPlan)
    assert plan.size == 117


def test_plan_execution_caps_size_by_liquidity():
    pair = _make_pair()
    opp = _make_opp()

    py_book = MockBook(bids=[], asks=[MockLevel(0.40, 5)])
    kn_book = MockBook(bids=[], asks=[MockLevel(0.45, 3)])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(py_book, kn_book),
    ):
        plan = plan_execution(opp, MagicMock(), MagicMock(), pair=pair)

    assert isinstance(plan, ExecutionPlan)
    assert plan.size == 3


def test_plan_execution_rejects_stale_arb():
    """If current prices have collapsed, the arb is stale and plan fails."""
    pair = _make_pair()
    opp = _make_opp()

    # Prices moved up so total cost > 1.
    py_book = MockBook(bids=[], asks=[MockLevel(0.60, 50)])
    kn_book = MockBook(bids=[], asks=[MockLevel(0.55, 50)])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(py_book, kn_book),
    ):
        rejection = plan_execution(opp, MagicMock(), MagicMock(), pair=pair)

    assert isinstance(rejection, PlanRejection)
    assert rejection.status == "stale"
    assert "decayed" in rejection.reason.lower() or "stale" in rejection.reason.lower()


def test_plan_execution_rejects_missing_outcome_ids():
    pair = _make_pair(poly_yes_outcome_id="", kalshi_no_outcome_id="")
    opp = _make_opp()

    rejection = plan_execution(opp, MagicMock(), MagicMock(), pair=pair)
    assert isinstance(rejection, PlanRejection)
    assert rejection.status == "missing_outcome_ids"


def test_plan_execution_rejects_none_pair():
    opp = _make_opp()
    rejection = plan_execution(opp, MagicMock(), MagicMock(), pair=None)
    assert isinstance(rejection, PlanRejection)
    assert rejection.status == "missing_outcome_ids"


def test_plan_execution_rejects_empty_order_book():
    pair = _make_pair()
    opp = _make_opp()

    empty_book = MockBook(bids=[], asks=[])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(empty_book, empty_book),
    ):
        rejection = plan_execution(opp, MagicMock(), MagicMock(), pair=pair)
    assert isinstance(rejection, PlanRejection)
    assert rejection.status == "insufficient_liquidity"


def test_plan_execution_rejects_cap_too_small_for_any_contract():
    pair = _make_pair()
    opp = _make_opp()

    py_book = MockBook(bids=[], asks=[MockLevel(0.40, 50)])
    kn_book = MockBook(bids=[], asks=[MockLevel(0.45, 50)])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        side_effect=_mock_fetch_books(py_book, kn_book),
    ):
        # $0.50 cap isn't enough to buy one contract at per_contract_cost 0.85.
        rejection = plan_execution(
            opp, MagicMock(), MagicMock(), pair=pair, max_trade_usd=0.50
        )
    assert isinstance(rejection, PlanRejection)
    assert rejection.status == "insufficient_liquidity"


# ---------------------------------------------------------------------------
# execute_plan — happy path + unwind
# ---------------------------------------------------------------------------


def _ready_plan(**overrides) -> ExecutionPlan:
    defaults = dict(
        opportunity_id=42,
        opportunity_timestamp="2026-04-10T12:00:00+00:00",
        poly_title="Will the Fed cut rates?",
        kalshi_title="KXFEDCUT",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        current_poly_price=0.40,
        current_kalshi_price=0.45,
        size=10.0,
        per_contract_cost=0.85,
        per_contract_fees=0.0354 + 0.0004,  # kalshi bracket + poly 0.1%
        per_contract_net=1.0 - 0.85 - (0.0354 + 0.0004),
        total_cost_usd=10.0 * 0.85,
        total_fees_usd=10.0 * (0.0354 + 0.0004),
        expected_net_profit=10.0 * (1.0 - 0.85 - (0.0354 + 0.0004)),
        max_trade_usd=100.0,
        poly_outcome_id="py1",
        kalshi_outcome_id="kn1",
        status="ready",
    )
    defaults.update(overrides)
    return ExecutionPlan(**defaults)


def test_execute_plan_happy_path_both_legs_filled():
    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=None)

    assert result.result == "success"
    assert result.leg1 is not None
    assert result.leg1.status == "filled"
    assert result.leg1.filled == 10.0
    assert result.leg2 is not None
    assert result.leg2.status == "filled"
    assert result.leg2.filled == 10.0
    assert result.unwind is None
    assert result.unwind_triggered is False

    # Locked-in arb: realized PnL = gross_edge - fees per contract × size
    gross = 1.0 - plan.per_contract_cost
    expected = (gross - plan.per_contract_fees) * plan.size
    assert result.final_net_pnl == pytest.approx(expected)


def test_execute_plan_leg2_failure_triggers_unwind():
    plan = _ready_plan()
    # Unwind should hit the fallback: entry - 0.01 = 0.39.
    result = execute_plan(
        plan,
        poly_exchange=None,  # fallback branch
        kalshi_exchange=None,
        simulate_leg2_failure=True,
    )

    assert result.result == "partial_unwind"
    assert result.unwind_triggered is True
    assert result.unwind is not None
    assert result.unwind.side == "sell"
    assert result.unwind.order_type == "market"
    assert result.unwind.status == "filled"
    assert result.unwind.price == pytest.approx(0.39)

    # Leg 1 bought at 0.40, unwind sold at 0.39 → -0.01/contract before fees.
    # Two poly fees (buy + sell) × poly_fee(price) × size.
    assert result.final_net_pnl < 0  # realized slippage loss


def test_execute_plan_unwind_uses_current_bid_when_exchange_available():
    plan = _ready_plan()
    bid_book = MockBook(bids=[MockLevel(0.37, 100)], asks=[])

    with patch(
        "arbscanner.execution.fetch_order_book_safe",
        return_value=bid_book,
    ):
        result = execute_plan(
            plan,
            poly_exchange=MagicMock(),
            kalshi_exchange=None,
            simulate_leg2_failure=True,
        )

    assert result.unwind is not None
    assert result.unwind.price == pytest.approx(0.37)


def test_execute_plan_not_ready_plan_short_circuits():
    plan = _ready_plan(status="stale")
    plan.rejection_reason = "decayed"
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=None)
    assert result.result == "stale"
    assert result.leg1 is None
    assert result.leg2 is None


# ---------------------------------------------------------------------------
# Dry-run isolation guarantee
# ---------------------------------------------------------------------------


def test_execute_plan_never_calls_create_order():
    """Phase A invariant: no real order placement, ever."""
    poly = MagicMock()
    kalshi = MagicMock()
    plan = _ready_plan()
    execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi)

    # Happy path: neither exchange's create_order is touched.
    assert not poly.create_order.called
    assert not kalshi.create_order.called

    # Same for the unwind branch.
    execute_plan(
        plan,
        poly_exchange=poly,
        kalshi_exchange=kalshi,
        simulate_leg2_failure=True,
    )
    assert not poly.create_order.called
    assert not kalshi.create_order.called


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_log_execution_roundtrip_success():
    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=None)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "exec.db"
        conn = get_connection(db_path)
        try:
            row_id = log_execution(conn, result)
            assert row_id > 0

            row = conn.execute(
                "SELECT result, direction, planned_size, final_net_pnl, "
                "unwind_triggered, dry_run FROM execution_log WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            conn.close()

    assert row is not None
    assert row[0] == "success"
    assert row[1] == "poly_yes_kalshi_no"
    assert row[2] == pytest.approx(10.0)
    assert row[3] == pytest.approx(result.final_net_pnl)
    assert row[4] == 0  # unwind not triggered
    assert row[5] == 1  # dry_run flag


def test_log_execution_roundtrip_partial_unwind():
    plan = _ready_plan()
    result = execute_plan(
        plan,
        poly_exchange=None,
        kalshi_exchange=None,
        simulate_leg2_failure=True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "exec.db"
        conn = get_connection(db_path)
        try:
            row_id = log_execution(conn, result)
            row = conn.execute(
                "SELECT result, unwind_triggered FROM execution_log WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            conn.close()

    assert row[0] == "partial_unwind"
    assert row[1] == 1


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_execution_report_success_includes_key_fields():
    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=None)
    text = format_execution_report(result)

    assert "DRY RUN" in text
    assert "Will the Fed cut rates?" in text
    assert "poly_yes_kalshi_no" in text
    assert "SUCCESS" in text
    assert "FILLED" in text
    assert "$100.00" in text  # max_trade_usd line


def test_format_execution_report_partial_unwind_includes_unwind_line():
    plan = _ready_plan()
    result = execute_plan(
        plan,
        poly_exchange=None,
        kalshi_exchange=None,
        simulate_leg2_failure=True,
    )
    text = format_execution_report(result)

    assert "PARTIAL_UNWIND" in text
    assert "Unwind" in text
    assert "REJECTED" in text  # leg 2 status


# ---------------------------------------------------------------------------
# Simulated order
# ---------------------------------------------------------------------------


def test_simulated_order_notional_computation():
    o = SimulatedOrder(
        exchange="polymarket",
        market_id="m",
        outcome_id="o",
        side="buy",
        order_type="limit",
        amount=10.0,
        price=0.40,
        status="filled",
        filled=10.0,
        remaining=0.0,
        fee=0.04,
    )
    # price × filled + fee = 4.00 + 0.04
    assert o.notional == pytest.approx(4.04)
    assert o.dry_run is True


# ---------------------------------------------------------------------------
# ExecutionResult has valid timestamp
# ---------------------------------------------------------------------------


def test_execution_result_timestamp_is_iso8601():
    plan = _ready_plan()
    result = ExecutionResult(plan=plan)
    ts = result.timestamp
    # Just verify it parses back.
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Phase A.2 — live execution
# ---------------------------------------------------------------------------

from arbscanner.execution import _place_live_order
from arbscanner.exchanges import CredentialError, validate_credentials


def _make_mock_order(status="filled", filled=10.0, remaining=0.0, fee=0.004):
    """Return a MagicMock that looks like a pmxt Order."""
    order = MagicMock()
    order.status = status
    order.filled = filled
    order.remaining = remaining
    order.fee = fee
    return order


def test_place_live_order_calls_create_order():
    exchange = MagicMock()
    exchange.create_order.return_value = _make_mock_order()

    result = _place_live_order(
        exchange=exchange,
        exchange_name="polymarket",
        market_id="poly_1",
        outcome_id="py1",
        side="buy",
        order_type="limit",
        amount=10.0,
        price=0.40,
        fee_rate_fn=lambda p: p * 0.001,
    )

    exchange.create_order.assert_called_once_with(
        market_id="poly_1",
        outcome_id="py1",
        side="buy",
        type="limit",
        amount=10.0,
        price=0.40,
    )
    assert result.status == "filled"
    assert result.filled == 10.0
    assert result.dry_run is False
    assert result.exchange == "polymarket"


def test_place_live_order_uses_fee_from_order():
    exchange = MagicMock()
    exchange.create_order.return_value = _make_mock_order(fee=0.123)

    result = _place_live_order(
        exchange=exchange,
        exchange_name="kalshi",
        market_id="k_1",
        outcome_id="kn1",
        side="buy",
        order_type="limit",
        amount=5.0,
        price=0.50,
        fee_rate_fn=lambda p: 0.035,
    )
    assert result.fee == pytest.approx(0.123)


def test_place_live_order_falls_back_to_fee_rate_fn_when_fee_is_none():
    exchange = MagicMock()
    order = _make_mock_order(fee=None, filled=5.0)
    exchange.create_order.return_value = order

    result = _place_live_order(
        exchange=exchange,
        exchange_name="kalshi",
        market_id="k_1",
        outcome_id="kn1",
        side="buy",
        order_type="limit",
        amount=5.0,
        price=0.50,
        fee_rate_fn=lambda p: 0.035,
    )
    # fee_rate_fn(0.50) * filled = 0.035 * 5.0
    assert result.fee == pytest.approx(0.035 * 5.0)


def test_execute_plan_live_calls_create_order_on_both_legs():
    poly = MagicMock()
    kalshi = MagicMock()
    poly.create_order.return_value = _make_mock_order(filled=10.0, fee=0.004)
    kalshi.create_order.return_value = _make_mock_order(filled=10.0, fee=0.035)

    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi, dry_run=False)

    assert poly.create_order.called
    assert kalshi.create_order.called
    assert result.result == "success"
    assert result.dry_run is False
    assert result.leg1.dry_run is False
    assert result.leg2.dry_run is False


def test_execute_plan_live_unwind_calls_create_order_sell():
    poly = MagicMock()
    kalshi = MagicMock()
    poly.create_order.return_value = _make_mock_order(filled=10.0, fee=0.004)
    # First call: leg 2 rejected
    kalshi.create_order.return_value = _make_mock_order(status="rejected", filled=0.0, fee=0.0)

    bid_book = MockBook(bids=[MockLevel(0.38, 100)], asks=[])

    with patch("arbscanner.execution.fetch_order_book_safe", return_value=bid_book):
        plan = _ready_plan()
        result = execute_plan(
            plan,
            poly_exchange=poly,
            kalshi_exchange=kalshi,
            simulate_leg2_failure=True,
            dry_run=False,
        )

    # Leg 2 was forced rejected by simulate_leg2_failure; in live mode _dispatch_order
    # skips force_rejection (only applies in dry-run), so kalshi.create_order was called.
    # The unwind should have triggered a second poly sell order.
    assert result.unwind_triggered is True
    assert poly.create_order.call_count == 2
    sell_call = poly.create_order.call_args_list[1]
    assert sell_call.kwargs.get("side") == "sell" or sell_call[1].get("side") == "sell"


def test_execute_plan_live_requires_both_exchange_instances():
    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=None, dry_run=False)
    assert result.result == "error"
    assert "authenticated" in result.error_message.lower()


def test_execute_plan_live_requires_poly_exchange():
    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=None, kalshi_exchange=MagicMock(), dry_run=False)
    assert result.result == "error"


def test_log_execution_live_records_dry_run_false():
    poly = MagicMock()
    kalshi = MagicMock()
    poly.create_order.return_value = _make_mock_order(filled=10.0, fee=0.004)
    kalshi.create_order.return_value = _make_mock_order(filled=10.0, fee=0.035)

    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi, dry_run=False)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "exec.db"
        conn = get_connection(db_path)
        try:
            row_id = log_execution(conn, result)
            row = conn.execute(
                "SELECT dry_run FROM execution_log WHERE id = ?", (row_id,)
            ).fetchone()
        finally:
            conn.close()

    assert row[0] == 0  # live execution logged as dry_run=0


def test_format_execution_report_live_says_live_execution():
    poly = MagicMock()
    kalshi = MagicMock()
    poly.create_order.return_value = _make_mock_order(filled=10.0, fee=0.004)
    kalshi.create_order.return_value = _make_mock_order(filled=10.0, fee=0.035)

    plan = _ready_plan()
    result = execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi, dry_run=False)
    text = format_execution_report(result)

    assert "LIVE EXECUTION" in text
    assert "DRY RUN" not in text


def test_validate_credentials_returns_missing_vars(monkeypatch):
    for var in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_PASSPHRASE",
                "POLY_PRIVATE_KEY", "KALSHI_API_KEY", "KALSHI_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)

    missing = validate_credentials()
    assert "POLY_API_KEY" in missing
    assert "KALSHI_PRIVATE_KEY" in missing


def test_validate_credentials_returns_empty_when_all_set(monkeypatch):
    for var in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_PASSPHRASE",
                "POLY_PRIVATE_KEY", "KALSHI_API_KEY", "KALSHI_PRIVATE_KEY"):
        monkeypatch.setenv(var, "test-value")

    missing = validate_credentials()
    assert missing == []


def test_create_authenticated_exchanges_raises_on_missing_creds(monkeypatch):
    from arbscanner.exchanges import create_authenticated_exchanges
    for var in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_PASSPHRASE",
                "POLY_PRIVATE_KEY", "KALSHI_API_KEY", "KALSHI_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(CredentialError):
        create_authenticated_exchanges()


def test_dry_run_still_never_calls_create_order_by_default():
    """Regression: default execute_plan must not call create_order."""
    poly = MagicMock()
    kalshi = MagicMock()
    plan = _ready_plan()

    execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi)
    assert not poly.create_order.called
    assert not kalshi.create_order.called

    execute_plan(plan, poly_exchange=poly, kalshi_exchange=kalshi, simulate_leg2_failure=True)
    assert not poly.create_order.called
    assert not kalshi.create_order.called
