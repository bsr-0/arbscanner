"""Arb execution pipeline — Phase A (dry-run) and Phase A.2 (live).

CLAUDE.md's Delivery block lists "v3: One-click execution via pmxt" as the
final delivery milestone. This module delivers the full execution pipeline:
planning, safety checks, two-leg placement, partial-fill unwind, and audit
logging. It runs in dry-run mode by default; pass ``dry_run=False`` (and
supply authenticated exchange instances) to place real orders.

Design choices:

* **Dry-run default.** ``execute_plan(dry_run=True)`` simulates every order
  without calling ``pmxt.*.create_order``. No credentials needed.
* **Live via ``dry_run=False``.** ``execute_plan(dry_run=False)`` calls the
  real ``create_order`` / ``cancel_order`` pmxt methods. Requires exchange
  instances created via ``exchanges.create_authenticated_exchanges()``.
* **Immediate market-order unwind on leg-2 failure.** If leg 2 fails (or is
  rejected), we immediately unwind leg 1 with a market sell. Tests inject this
  path via ``simulate_leg2_failure=True``.
* **CLI-only trigger.** No HTTP execution endpoint and no scan-loop
  auto-trigger — execution is always initiated interactively by an operator
  via ``arbscanner execute <id> [--live]``.
* **Hard $100 per-trade cap by default.** Raised via ``--max-trade-usd``.
  Every plan re-checks the cap after sizing against available liquidity.

The execution is persisted to the ``execution_log`` SQLite table.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from arbscanner.config import DB_PATH, kalshi_fee, poly_fee
from arbscanner.exchanges import fetch_order_book_safe
from arbscanner.models import ArbOpportunity, MatchedPair

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Per-trade USD notional cap while Phase A is in bring-up. Every plan is
#: capped to this amount before simulated placement. Overridable per call
#: but intentionally low while the pipeline is maturing.
DEFAULT_MAX_TRADE_USD: float = 100.0

#: Default execution mode. Imported by legacy tests; new code should use the
#: ``dry_run`` parameter on :func:`execute_plan` directly.
EXECUTION_MODE: str = "dry_run"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SimulatedOrder:
    """The dry-run analog of a ``pmxt.Order`` response.

    Every field mirrors the pmxt ``Order`` shape so that a future Phase A.2
    can drop in real orders at this layer with minimal fanout.
    """

    exchange: str  # "polymarket" | "kalshi"
    market_id: str
    outcome_id: str
    side: str  # "buy" | "sell"
    order_type: str  # "limit" | "market"
    amount: float
    price: float
    status: str  # "filled" | "rejected" | "partial"
    filled: float
    remaining: float
    fee: float
    dry_run: bool = True

    @property
    def notional(self) -> float:
        """Net USD notional spent on this leg (price × filled + fee)."""
        return self.price * self.filled + self.fee


@dataclass
class ExecutionPlan:
    """Everything we need to know before sending the first leg.

    Built by :func:`plan_execution` from a logged opportunity and a fresh
    re-fetch of both exchanges' order books. Contains all safety-check
    outcomes; callers inspect ``status`` to decide whether to proceed.
    """

    opportunity_id: int | None
    opportunity_timestamp: str
    poly_title: str
    kalshi_title: str
    poly_market_id: str
    kalshi_market_id: str
    direction: str  # "poly_yes_kalshi_no" | "poly_no_kalshi_yes"

    # Current (re-fetched) best-ask prices on each leg
    current_poly_price: float
    current_kalshi_price: float

    # Final sized plan
    size: float
    per_contract_cost: float
    per_contract_fees: float
    per_contract_net: float
    total_cost_usd: float
    total_fees_usd: float
    expected_net_profit: float
    max_trade_usd: float

    # Outcome IDs for the legs (poly_yes_outcome_id or poly_no_outcome_id
    # depending on direction, same for kalshi)
    poly_outcome_id: str
    kalshi_outcome_id: str

    # One of: "ready" | "stale" | "insufficient_liquidity" | "missing_outcome_ids"
    status: str = "ready"
    rejection_reason: str = ""


@dataclass
class ExecutionResult:
    """The outcome of running an :class:`ExecutionPlan` through the pipeline."""

    plan: ExecutionPlan
    leg1: SimulatedOrder | None = None
    leg2: SimulatedOrder | None = None
    unwind: SimulatedOrder | None = None
    unwind_triggered: bool = False

    # Terminal status: "success" | "stale" | "insufficient_liquidity" |
    # "missing_outcome_ids" | "partial_unwind" | "rejected" | "error"
    result: str = "success"

    #: Realized net PnL in dollars. For ``success`` this is the locked-in
    #: arb edge (``per_contract_net × size``). For ``partial_unwind`` this
    #: is leg 1's fill proceeds minus the market unwind cost minus fees.
    final_net_pnl: float = 0.0

    error_message: str = ""

    #: False when real orders were placed via pmxt (Phase A.2).
    dry_run: bool = True

    @property
    def timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()


@dataclass
class PlanRejection:
    """Returned by :func:`plan_execution` when a safety check blocks the trade."""

    opportunity_id: int | None
    status: str  # same vocabulary as ExecutionPlan.status (non-ready)
    reason: str
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_EXECUTION_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    opportunity_id INTEGER,
    dry_run INTEGER NOT NULL,
    direction TEXT NOT NULL,
    poly_market_id TEXT NOT NULL,
    kalshi_market_id TEXT NOT NULL,
    market_title TEXT NOT NULL,

    planned_size REAL NOT NULL,
    planned_poly_price REAL NOT NULL,
    planned_kalshi_price REAL NOT NULL,
    planned_cost_usd REAL NOT NULL,
    planned_fees_usd REAL NOT NULL,
    planned_net_profit REAL NOT NULL,
    max_trade_usd REAL NOT NULL,

    leg1_exchange TEXT,
    leg1_status TEXT,
    leg1_filled REAL,
    leg1_fee REAL,
    leg2_exchange TEXT,
    leg2_status TEXT,
    leg2_filled REAL,
    leg2_fee REAL,

    unwind_triggered INTEGER NOT NULL DEFAULT 0,
    unwind_realized_pnl REAL,

    result TEXT NOT NULL,
    final_net_pnl REAL,
    error_message TEXT
);
"""

_EXECUTION_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_execution_log_timestamp ON execution_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_execution_log_opportunity_id ON execution_log(opportunity_id)",
    "CREATE INDEX IF NOT EXISTS idx_execution_log_result ON execution_log(result)",
]


def ensure_execution_log_schema(conn: sqlite3.Connection) -> None:
    """Create the ``execution_log`` table + indexes idempotently."""
    conn.execute(_EXECUTION_LOG_SCHEMA)
    for idx in _EXECUTION_LOG_INDEXES:
        conn.execute(idx)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _best_ask(order_book) -> dict | None:
    """Extract the best (lowest) ask as ``{price, amount}`` or ``None``."""
    if order_book is None or not getattr(order_book, "asks", None):
        return None
    best = order_book.asks[0]
    return {"price": best.price, "amount": best.amount}


def _best_bid(order_book) -> dict | None:
    """Extract the best (highest) bid as ``{price, amount}`` or ``None``."""
    if order_book is None or not getattr(order_book, "bids", None):
        return None
    best = order_book.bids[0]
    return {"price": best.price, "amount": best.amount}


def _outcome_ids_for_direction(
    pair: MatchedPair | None, opp: ArbOpportunity, direction: str
) -> tuple[str, str]:
    """Return ``(poly_outcome_id, kalshi_outcome_id)`` for the direction.

    When we have the underlying ``MatchedPair`` we use the explicit outcome
    IDs recorded by the matcher; otherwise we fall back to pair-less lookups
    which the caller must supply via ``opp``-level fields (not stored today).
    """
    if pair is None:
        return "", ""
    if direction == "poly_yes_kalshi_no":
        return pair.poly_yes_outcome_id, pair.kalshi_no_outcome_id
    if direction == "poly_no_kalshi_yes":
        return pair.poly_no_outcome_id, pair.kalshi_yes_outcome_id
    return "", ""


def _per_contract_fees(poly_price: float, kalshi_price: float) -> float:
    """Total taker fees per contract across both legs."""
    return poly_fee(poly_price) + kalshi_fee(kalshi_price)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_execution(
    opp: ArbOpportunity,
    poly_exchange,
    kalshi_exchange,
    pair: MatchedPair | None,
    *,
    max_trade_usd: float = DEFAULT_MAX_TRADE_USD,
    opportunity_id: int | None = None,
) -> ExecutionPlan | PlanRejection:
    """Build an :class:`ExecutionPlan` from a logged opportunity.

    Re-fetches both sides' order books (the logged prices are stale by
    construction — operator-triggered executions run minutes or hours
    after the scanner logged the row) and runs every safety check:

    1. Both outcome IDs must be known (from the matched-pair cache).
    2. Current best-ask must still yield a positive net edge.
    3. Available size must be > 0 on both legs.
    4. Size is capped to ``min(liquidity, max_trade_usd / per_contract_cost)``.

    Any failed check short-circuits to a :class:`PlanRejection` with a
    precise status string the CLI can surface.
    """
    poly_outcome_id, kalshi_outcome_id = _outcome_ids_for_direction(
        pair, opp, opp.direction
    )
    if not poly_outcome_id or not kalshi_outcome_id:
        return PlanRejection(
            opportunity_id=opportunity_id,
            status="missing_outcome_ids",
            reason=(
                "Matched-pair cache has no outcome IDs for this opportunity — "
                "rerun `arbscanner match` to refresh the cache."
            ),
            details={
                "poly_outcome_id": poly_outcome_id,
                "kalshi_outcome_id": kalshi_outcome_id,
            },
        )

    # Re-fetch CURRENT order books. We don't trust the logged prices.
    poly_book = fetch_order_book_safe(poly_exchange, poly_outcome_id)
    kalshi_book = fetch_order_book_safe(kalshi_exchange, kalshi_outcome_id)
    poly_ask = _best_ask(poly_book)
    kalshi_ask = _best_ask(kalshi_book)

    if poly_ask is None or kalshi_ask is None:
        return PlanRejection(
            opportunity_id=opportunity_id,
            status="insufficient_liquidity",
            reason="One or both exchanges returned an empty ask side at execution time",
            details={
                "poly_ask": poly_ask,
                "kalshi_ask": kalshi_ask,
            },
        )

    poly_price = float(poly_ask["price"])
    kalshi_price = float(kalshi_ask["price"])
    per_contract_cost = poly_price + kalshi_price
    fees = _per_contract_fees(poly_price, kalshi_price)
    per_contract_net = 1.0 - per_contract_cost - fees

    if per_contract_net <= 0:
        return PlanRejection(
            opportunity_id=opportunity_id,
            status="stale",
            reason=(
                f"Arb has decayed since log time: net edge "
                f"{per_contract_net:.4f} ≤ 0 at current prices "
                f"(poly={poly_price:.3f}, kalshi={kalshi_price:.3f})"
            ),
            details={
                "per_contract_cost": per_contract_cost,
                "per_contract_fees": fees,
                "per_contract_net": per_contract_net,
            },
        )

    liquidity = min(float(poly_ask["amount"]), float(kalshi_ask["amount"]))
    if liquidity <= 0:
        return PlanRejection(
            opportunity_id=opportunity_id,
            status="insufficient_liquidity",
            reason="Both sides have non-positive top-of-book size",
            details={
                "poly_size": poly_ask["amount"],
                "kalshi_size": kalshi_ask["amount"],
            },
        )

    # Size capped by whichever is smaller: available liquidity or USD cap.
    # Kalshi trades in whole contracts, so we floor the result — this is the
    # right default for cross-platform arbs where Kalshi is the bottleneck.
    max_affordable = max_trade_usd / per_contract_cost if per_contract_cost > 0 else 0.0
    size = float(math.floor(min(liquidity, max_affordable)))

    if size <= 0:
        return PlanRejection(
            opportunity_id=opportunity_id,
            status="insufficient_liquidity",
            reason=(
                f"Per-trade cap ${max_trade_usd:.2f} too small to purchase a "
                f"single contract at per-contract cost ${per_contract_cost:.4f}"
            ),
            details={
                "max_trade_usd": max_trade_usd,
                "per_contract_cost": per_contract_cost,
            },
        )

    return ExecutionPlan(
        opportunity_id=opportunity_id,
        opportunity_timestamp=opp.timestamp.isoformat(),
        poly_title=opp.poly_title,
        kalshi_title=opp.kalshi_title,
        poly_market_id=opp.poly_market_id,
        kalshi_market_id=opp.kalshi_market_id,
        direction=opp.direction,
        current_poly_price=poly_price,
        current_kalshi_price=kalshi_price,
        size=size,
        per_contract_cost=per_contract_cost,
        per_contract_fees=fees,
        per_contract_net=per_contract_net,
        total_cost_usd=size * per_contract_cost,
        total_fees_usd=size * fees,
        expected_net_profit=size * per_contract_net,
        max_trade_usd=max_trade_usd,
        poly_outcome_id=poly_outcome_id,
        kalshi_outcome_id=kalshi_outcome_id,
        status="ready",
    )


# ---------------------------------------------------------------------------
# Dry-run simulation
# ---------------------------------------------------------------------------


def _simulate_place_order(
    *,
    exchange: str,
    market_id: str,
    outcome_id: str,
    side: str,
    order_type: str,
    amount: float,
    price: float,
    fee_rate_fn,
    force_rejection: bool = False,
) -> SimulatedOrder:
    """Return a :class:`SimulatedOrder` for a dry-run placement.

    ``force_rejection=True`` returns a rejected order with 0 filled amount —
    used by :func:`execute_plan` to inject leg-2 failures for tests and
    exercised via the CLI's ``--simulate-leg2-failure`` flag.
    """
    if force_rejection:
        return SimulatedOrder(
            exchange=exchange,
            market_id=market_id,
            outcome_id=outcome_id,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            status="rejected",
            filled=0.0,
            remaining=amount,
            fee=0.0,
        )

    fee = fee_rate_fn(price) * amount
    logger.info(
        "[DRY RUN] Would place %s %s %s %.2f @ %.4f on %s (%s); fee=%.4f",
        order_type.upper(),
        side.upper(),
        market_id,
        amount,
        price,
        exchange,
        outcome_id,
        fee,
    )
    return SimulatedOrder(
        exchange=exchange,
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        order_type=order_type,
        amount=amount,
        price=price,
        status="filled",
        filled=amount,
        remaining=0.0,
        fee=fee,
    )


def _place_live_order(
    *,
    exchange,
    exchange_name: str,
    market_id: str,
    outcome_id: str,
    side: str,
    order_type: str,
    amount: float,
    price: float,
    fee_rate_fn,
) -> SimulatedOrder:
    """Place a real order via pmxt and wrap the response as a :class:`SimulatedOrder`.

    The ``SimulatedOrder`` shape was deliberately designed to mirror pmxt's
    ``Order`` model, so the rest of the pipeline is exchange-agnostic.
    """
    logger.info(
        "[LIVE] Placing %s %s %s %.2f @ %.4f on %s (%s)",
        order_type.upper(),
        side.upper(),
        market_id,
        amount,
        price,
        exchange_name,
        outcome_id,
    )
    order = exchange.create_order(
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        type=order_type,
        amount=amount,
        price=price,
    )
    fee = order.fee if order.fee is not None else fee_rate_fn(price) * order.filled
    return SimulatedOrder(
        exchange=exchange_name,
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        order_type=order_type,
        amount=amount,
        price=price,
        status=order.status,
        filled=float(order.filled),
        remaining=float(order.remaining),
        fee=float(fee),
        dry_run=False,
    )


def _dispatch_order(
    *,
    dry_run: bool,
    exchange,
    exchange_name: str,
    market_id: str,
    outcome_id: str,
    side: str,
    order_type: str,
    amount: float,
    price: float,
    fee_rate_fn,
    force_rejection: bool = False,
) -> SimulatedOrder:
    """Route to simulation or live placement based on ``dry_run``."""
    if dry_run:
        return _simulate_place_order(
            exchange=exchange_name,
            market_id=market_id,
            outcome_id=outcome_id,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            fee_rate_fn=fee_rate_fn,
            force_rejection=force_rejection,
        )
    return _place_live_order(
        exchange=exchange,
        exchange_name=exchange_name,
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        order_type=order_type,
        amount=amount,
        price=price,
        fee_rate_fn=fee_rate_fn,
    )


def execute_plan(
    plan: ExecutionPlan,
    *,
    poly_exchange=None,
    kalshi_exchange=None,
    simulate_leg2_failure: bool = False,
    dry_run: bool = True,
) -> ExecutionResult:
    """Run an :class:`ExecutionPlan` through the placement pipeline.

    When ``dry_run=True`` (default), every order is simulated — no real
    ``create_order`` calls are made and no credentials are needed.

    When ``dry_run=False``, real orders are placed via the supplied
    ``poly_exchange`` and ``kalshi_exchange`` instances (created via
    :func:`exchanges.create_authenticated_exchanges`). Both must be provided.

    The unwind uses the *current best bid* on leg 1's order book as the
    market-order exit price. If the book is empty, it falls back to one cent
    of slippage off the entry price.
    """
    result = ExecutionResult(plan=plan, dry_run=dry_run)

    if plan.status != "ready":
        result.result = plan.status
        result.error_message = plan.rejection_reason
        return result

    if not dry_run and (poly_exchange is None or kalshi_exchange is None):
        result.result = "error"
        result.error_message = (
            "Live execution requires authenticated exchange instances. "
            "Pass exchanges from create_authenticated_exchanges()."
        )
        return result

    # Sides derived from direction (both legs are buys — complementary outcomes sum to $1).
    if plan.direction in ("poly_yes_kalshi_no", "poly_no_kalshi_yes"):
        poly_side, kalshi_side = "buy", "buy"
    else:
        result.result = "rejected"
        result.error_message = f"Unknown direction {plan.direction!r}"
        return result

    mode_tag = "DRY RUN" if dry_run else "LIVE"

    # --- Leg 1 (Polymarket) ---
    leg1 = _dispatch_order(
        dry_run=dry_run,
        exchange=poly_exchange,
        exchange_name="polymarket",
        market_id=plan.poly_market_id,
        outcome_id=plan.poly_outcome_id,
        side=poly_side,
        order_type="limit",
        amount=plan.size,
        price=plan.current_poly_price,
        fee_rate_fn=poly_fee,
        force_rejection=False,
    )
    result.leg1 = leg1

    # --- Leg 2 (Kalshi) ---
    leg2 = _dispatch_order(
        dry_run=dry_run,
        exchange=kalshi_exchange,
        exchange_name="kalshi",
        market_id=plan.kalshi_market_id,
        outcome_id=plan.kalshi_outcome_id,
        side=kalshi_side,
        order_type="limit",
        amount=plan.size,
        price=plan.current_kalshi_price,
        fee_rate_fn=kalshi_fee,
        force_rejection=simulate_leg2_failure,
    )
    result.leg2 = leg2

    if leg2.status == "filled":
        gross = 1.0 - plan.per_contract_cost
        result.final_net_pnl = (gross - plan.per_contract_fees) * plan.size
        result.result = "success"
        return result

    # --- Unwind path: leg 2 failed — dump leg 1 at market on the bid. ---
    result.unwind_triggered = True
    unwind_exit_price = _determine_unwind_price(
        poly_exchange, plan.poly_outcome_id, plan.current_poly_price
    )
    unwind_amount = leg1.filled if leg1.filled > 0 else plan.size

    unwind = _dispatch_order(
        dry_run=dry_run,
        exchange=poly_exchange,
        exchange_name="polymarket",
        market_id=plan.poly_market_id,
        outcome_id=plan.poly_outcome_id,
        side="sell",
        order_type="market",
        amount=unwind_amount,
        price=unwind_exit_price,
        fee_rate_fn=poly_fee,
        force_rejection=False,
    )
    result.unwind = unwind

    per_contract_loss = (
        (unwind.price - leg1.price)
        - (leg1.fee / plan.size if plan.size else 0.0)
        - (unwind.fee / plan.size if plan.size else 0.0)
    )
    result.final_net_pnl = per_contract_loss * plan.size
    result.result = "partial_unwind"
    logger.warning(
        "[%s] Partial fill → market-unwound leg 1. "
        "Entry=%.4f Exit=%.4f Size=%.2f Realized=%.4f",
        mode_tag,
        leg1.price,
        unwind.price,
        plan.size,
        result.final_net_pnl,
    )
    return result


def _determine_unwind_price(
    poly_exchange, poly_outcome_id: str, fallback_entry_price: float
) -> float:
    """Pick a reasonable market-unwind exit price for leg 1 on Polymarket.

    Uses the current best bid if reachable; otherwise assumes one cent of
    slippage off the entry price as a conservative worst case. Callers may
    pass ``poly_exchange=None`` to skip the re-fetch (tests do this).
    """
    if poly_exchange is not None:
        book = fetch_order_book_safe(poly_exchange, poly_outcome_id)
        bid = _best_bid(book)
        if bid is not None:
            return float(bid["price"])
    return max(0.0, fallback_entry_price - 0.01)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def log_execution(conn: sqlite3.Connection, result: ExecutionResult) -> int:
    """Persist an :class:`ExecutionResult` to the ``execution_log`` table.

    Returns the autoincrement ``id`` assigned by SQLite. Creates the
    schema on first use.
    """
    ensure_execution_log_schema(conn)
    plan = result.plan

    leg1 = result.leg1
    leg2 = result.leg2
    unwind = result.unwind

    cur = conn.execute(
        """
        INSERT INTO execution_log (
            timestamp, opportunity_id, dry_run, direction,
            poly_market_id, kalshi_market_id, market_title,
            planned_size, planned_poly_price, planned_kalshi_price,
            planned_cost_usd, planned_fees_usd, planned_net_profit, max_trade_usd,
            leg1_exchange, leg1_status, leg1_filled, leg1_fee,
            leg2_exchange, leg2_status, leg2_filled, leg2_fee,
            unwind_triggered, unwind_realized_pnl,
            result, final_net_pnl, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?,
                  ?, ?, ?, ?,
                  ?, ?, ?, ?, ?)
        """,
        (
            result.timestamp,
            plan.opportunity_id,
            1 if result.dry_run else 0,
            plan.direction,
            plan.poly_market_id,
            plan.kalshi_market_id,
            plan.poly_title,
            plan.size,
            plan.current_poly_price,
            plan.current_kalshi_price,
            plan.total_cost_usd,
            plan.total_fees_usd,
            plan.expected_net_profit,
            plan.max_trade_usd,
            leg1.exchange if leg1 else None,
            leg1.status if leg1 else None,
            leg1.filled if leg1 else None,
            leg1.fee if leg1 else None,
            leg2.exchange if leg2 else None,
            leg2.status if leg2 else None,
            leg2.filled if leg2 else None,
            leg2.fee if leg2 else None,
            1 if result.unwind_triggered else 0,
            unwind.notional if unwind else None,
            result.result,
            result.final_net_pnl,
            result.error_message,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection and ensure the execution_log schema exists."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False)
    ensure_execution_log_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_execution_report(result: ExecutionResult) -> str:
    """Human-readable multi-line report for the CLI."""
    plan = result.plan
    lines: list[str] = []
    mode = "DRY RUN — no real orders placed" if result.dry_run else "LIVE EXECUTION — real orders placed"
    lines.append(f"Execution report ({mode})")
    lines.append("=" * 60)
    lines.append(f"  Opportunity ID:       {plan.opportunity_id}")
    lines.append(f"  Market:               {plan.poly_title}")
    lines.append(f"  Direction:            {plan.direction}")
    lines.append(f"  Current poly ask:     {plan.current_poly_price:.4f}")
    lines.append(f"  Current kalshi ask:   {plan.current_kalshi_price:.4f}")
    lines.append(f"  Per-contract cost:    {plan.per_contract_cost:.4f}")
    lines.append(f"  Per-contract fees:    {plan.per_contract_fees:.4f}")
    lines.append(f"  Per-contract net:     {plan.per_contract_net:.4f}")
    lines.append(f"  Size:                 {plan.size:.2f} contracts")
    lines.append(f"  Total cost:           ${plan.total_cost_usd:.2f}")
    lines.append(f"  Total fees:           ${plan.total_fees_usd:.2f}")
    lines.append(f"  Expected net profit:  ${plan.expected_net_profit:.2f}")
    lines.append(f"  Per-trade USD cap:    ${plan.max_trade_usd:.2f}")
    lines.append("")

    if result.leg1 is None and result.leg2 is None:
        lines.append(f"  Result: {result.result.upper()}")
        if result.error_message:
            lines.append(f"  Reason: {result.error_message}")
        return "\n".join(lines)

    if result.leg1 is not None:
        lines.append(
            f"  Leg 1 (polymarket):   "
            f"{result.leg1.status.upper()}  "
            f"filled={result.leg1.filled:.2f} "
            f"@ {result.leg1.price:.4f}  fee=${result.leg1.fee:.4f}"
        )
    if result.leg2 is not None:
        lines.append(
            f"  Leg 2 (kalshi):       "
            f"{result.leg2.status.upper()}  "
            f"filled={result.leg2.filled:.2f} "
            f"@ {result.leg2.price:.4f}  fee=${result.leg2.fee:.4f}"
        )
    if result.unwind is not None:
        lines.append(
            f"  Unwind (polymarket):  "
            f"{result.unwind.status.upper()}  "
            f"filled={result.unwind.filled:.2f} "
            f"@ {result.unwind.price:.4f}  fee=${result.unwind.fee:.4f}"
        )

    lines.append("")
    lines.append(f"  Result:               {result.result.upper()}")
    lines.append(f"  Final realized PnL:   ${result.final_net_pnl:.4f}")
    if result.error_message:
        lines.append(f"  Error:                {result.error_message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Expose plan fields for tests
# ---------------------------------------------------------------------------


def plan_to_dict(plan: ExecutionPlan) -> dict:
    """Return a JSON-serializable dict view of a plan (used by tests)."""
    return asdict(plan)
