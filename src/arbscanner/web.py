"""FastAPI web backend for arbscanner dashboard."""

import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import stripe
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from arbscanner.backtest import compute_backtest_report
from arbscanner.calibration import get_calibration_context, get_historical_edge_stats
from arbscanner.config import (
    DB_PATH,
    FREE_ALERT_DELAY_SECONDS,
    FREE_MAX_OPPORTUNITIES,
    TEMPLATES_DIR,
    settings,
)
from arbscanner.db import get_connection, get_opportunity_by_id
from arbscanner.health import router as health_router
from arbscanner.matcher import load_cache
from arbscanner.metrics import MetricsRegistry
from arbscanner.models import MatchedPair
from arbscanner.paper_trading import PaperPosition, PaperTradingEngine

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup."""
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
    app.state.db = get_connection()
    app.state.paper_engine = PaperTradingEngine()
    app.state.start_time = time.time()
    yield
    app.state.db.close()
    app.state.paper_engine.close()


app = FastAPI(title="ArbScanner", version="0.2.0", lifespan=lifespan)
app.include_router(health_router)


# --- JSON API endpoints ---


def _get_tier(request: Request) -> str:
    """Return the requesting tier: 'free' or 'pro'.

    The `X-Arbscanner-Tier` header wins so tests and demo deployments can
    override the global default without restarting the server. Otherwise we
    fall back to the `ARBSCANNER_TIER` env var (stored on `settings.tier`).
    Any value other than ``"free"`` is treated as ``"pro"`` so a typo never
    accidentally locks out a paying user.
    """
    header = request.headers.get("x-arbscanner-tier", "").strip().lower()
    raw = header or settings.tier
    return "free" if raw == "free" else "pro"


def _build_pair_index() -> dict[str, MatchedPair]:
    """Return a {poly_id::kalshi_id -> MatchedPair} index of the current cache.

    This is the join key used to attach per-opportunity calibration context
    from the JSON pair cache without touching the SQLite opportunities schema.
    """
    cache = load_cache()
    return {f"{p.poly_market_id}::{p.kalshi_market_id}": p for p in cache.pairs}


def _calibration_for_row(
    pair_index: dict[str, MatchedPair],
    poly_market_id: str,
    kalshi_market_id: str,
    net_edge: float,
) -> dict | None:
    """Look up calibration context for a logged opportunity row.

    Returns ``None`` when no matching pair is known or the pair lacks
    calibration metadata.
    """
    pair = pair_index.get(f"{poly_market_id}::{kalshi_market_id}")
    if pair is None or (not pair.category and not pair.resolution_date):
        return None

    resolution_date = None
    if pair.resolution_date:
        try:
            resolution_date = datetime.fromisoformat(
                pair.resolution_date.replace("Z", "+00:00")
            )
        except ValueError:
            resolution_date = None

    try:
        ctx = get_calibration_context(
            pair.category or None, resolution_date, net_edge
        )
    except Exception:
        logger.debug(
            "Calibration lookup failed for pair %s/%s",
            poly_market_id,
            kalshi_market_id,
        )
        return None

    return {
        "category": ctx.category,
        "days_to_resolution": ctx.days_to_resolution,
        "time_bucket": ctx.time_bucket,
        "avg_mispricing": ctx.avg_mispricing,
        "edge_likely_real": ctx.edge_likely_real,
        "confidence_note": ctx.confidence_note,
    }


@app.get("/api/opportunities")
def get_opportunities(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    min_edge: float = Query(0.0, ge=0.0),
    hours: int = Query(24, ge=1, le=168),
):
    """Get recent arb opportunities from the database, enriched with calibration.

    Tier gating (CLAUDE.md Day 10):

    * ``free``: cap results to ``FREE_MAX_OPPORTUNITIES`` (top 3 by expected
      profit), apply a ``FREE_ALERT_DELAY_SECONDS`` (5-minute) lag on the
      visible window, and strip calibration context — the landing page sells
      calibration as a Pro feature.
    * ``pro``: no gating. Full table, real-time, calibration included.
    """
    tier = _get_tier(request)
    conn = app.state.db
    now = datetime.now(timezone.utc)
    # ISO 8601 strings sort lexicographically when timezone-normalized, so a
    # direct string comparison is correct here.
    cutoff = (now - timedelta(hours=hours)).isoformat()

    if tier == "free":
        effective_limit = min(limit, FREE_MAX_OPPORTUNITIES)
        latest_allowed = (now - timedelta(seconds=FREE_ALERT_DELAY_SECONDS)).isoformat()
        query = """
            SELECT id, timestamp, poly_market_id, kalshi_market_id, market_title,
                   direction, gross_edge, net_edge, available_size,
                   expected_profit, poly_price, kalshi_price
            FROM opportunities
            WHERE net_edge >= ?
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY expected_profit DESC
            LIMIT ?
        """
        rows = conn.execute(
            query, (min_edge, cutoff, latest_allowed, effective_limit)
        ).fetchall()
    else:
        query = """
            SELECT id, timestamp, poly_market_id, kalshi_market_id, market_title,
                   direction, gross_edge, net_edge, available_size,
                   expected_profit, poly_price, kalshi_price
            FROM opportunities
            WHERE net_edge >= ?
              AND timestamp >= ?
            ORDER BY expected_profit DESC
            LIMIT ?
        """
        rows = conn.execute(query, (min_edge, cutoff, limit)).fetchall()

    pair_index = _build_pair_index() if tier == "pro" else {}
    return [
        {
            "id": row[0],
            "timestamp": row[1],
            "poly_market_id": row[2],
            "kalshi_market_id": row[3],
            "market_title": row[4],
            "direction": row[5],
            "gross_edge": row[6],
            "net_edge": row[7],
            "available_size": row[8],
            "expected_profit": row[9],
            "poly_price": row[10],
            "kalshi_price": row[11],
            "calibration": (
                _calibration_for_row(pair_index, row[2], row[3], row[7])
                if tier == "pro"
                else None
            ),
        }
        for row in rows
    ]


@app.get("/api/pairs")
def get_pairs():
    """Get current matched market pairs."""
    cache = load_cache()
    return {
        "updated_at": cache.updated_at,
        "count": len(cache.pairs),
        "pairs": [
            {
                "poly_market_id": p.poly_market_id,
                "poly_title": p.poly_title,
                "kalshi_market_id": p.kalshi_market_id,
                "kalshi_title": p.kalshi_title,
                "confidence": p.confidence,
                "source": p.source,
            }
            for p in cache.pairs
        ],
    }


@app.get("/api/stats")
def get_stats():
    """Get summary statistics from historical data."""
    stats = get_historical_edge_stats()
    cache = load_cache()
    return {
        "matched_pairs": len(cache.pairs),
        "uptime_seconds": int(time.time() - app.state.start_time),
        **stats,
    }


# Prometheus text exposition format, version 0.0.4. Scrapers look for this
# exact content-type; returning application/json here would make them reject
# the payload. See: prometheus.io/docs/instrumenting/exposition_formats/
_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@app.get("/metrics", response_class=PlainTextResponse)
def get_metrics() -> PlainTextResponse:
    """Prometheus-compatible metrics endpoint.

    Every pre-registered metric in :mod:`arbscanner.metrics` is rendered
    here — scan cycles, order-book fetches, rate-limit waits, alerts,
    and the scan-cycle duration histogram. The engine increments these
    via its own code path; this endpoint only reads.
    """
    body = MetricsRegistry.instance().export_text()
    return PlainTextResponse(content=body, media_type=_PROM_CONTENT_TYPE)


@app.get("/api/calibration")
def get_calibration(
    request: Request,
    category: str = Query("politics"),
    days_to_resolution: int | None = Query(None),
    net_edge: float = Query(0.01),
):
    """Get calibration context for a hypothetical opportunity.

    Gated behind the Pro tier per CLAUDE.md Day 10 (calibration context is
    explicitly listed as a paid-tier feature on the landing page).
    """
    if _get_tier(request) != "pro":
        raise HTTPException(
            status_code=402,
            detail="Calibration context is a Pro feature. Upgrade at /#pricing.",
        )

    resolution_date = None
    if days_to_resolution is not None:
        from datetime import timedelta

        resolution_date = datetime.now(timezone.utc) + timedelta(days=days_to_resolution)

    ctx = get_calibration_context(category, resolution_date, net_edge)
    return {
        "category": ctx.category,
        "days_to_resolution": ctx.days_to_resolution,
        "time_bucket": ctx.time_bucket,
        "avg_mispricing": ctx.avg_mispricing,
        "edge_likely_real": ctx.edge_likely_real,
        "confidence_note": ctx.confidence_note,
    }


# --- Backtest endpoints ---


@app.get("/api/backtest")
def get_backtest(
    hours: int = Query(168, ge=1, le=24 * 365),
    min_edge: float = Query(0.0, ge=0.0),
):
    """Aggregate hypothetical + realized performance across logged opportunities."""
    report = compute_backtest_report(app.state.db, hours=hours, min_edge=min_edge)
    return report.as_dict()


# --- Paper trading ---


class OpenPaperRequest(BaseModel):
    """Payload for POST /api/paper/open."""

    opportunity_id: int
    size: float | None = Field(default=None, ge=0.0)


class ClosePaperRequest(BaseModel):
    """Payload for POST /api/paper/close/{position_id}.

    Exactly one of the two modes must be supplied:

    * ``poly_price`` and ``kalshi_price`` — mark-to-market close.
    * ``yes_won`` — resolve the position at final outcome.
    """

    poly_price: float | None = Field(default=None, ge=0.0, le=1.0)
    kalshi_price: float | None = Field(default=None, ge=0.0, le=1.0)
    yes_won: bool | None = None


def _paper_engine(request: Request) -> PaperTradingEngine:
    engine = getattr(request.app.state, "paper_engine", None)
    if engine is None:
        engine = PaperTradingEngine()
        request.app.state.paper_engine = engine
    return engine


def _serialize_position(position: PaperPosition) -> dict:
    return {
        "id": position.id,
        "opportunity_id": position.opportunity_id,
        "opened_at": position.opened_at.isoformat(),
        "pair_id": position.pair_id,
        "direction": position.direction,
        "poly_side": position.poly_side,
        "kalshi_side": position.kalshi_side,
        "entry_poly_price": position.entry_poly_price,
        "entry_kalshi_price": position.entry_kalshi_price,
        "size": position.size,
        "expected_profit": position.expected_profit,
        "status": position.status,
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        "realized_pnl": position.realized_pnl,
    }


class PaperClosePayload(BaseModel):
    poly_price: float = Field(..., ge=0.0, le=1.0)
    kalshi_price: float = Field(..., ge=0.0, le=1.0)


class PaperResolvePayload(BaseModel):
    yes_won: bool


@app.get("/api/paper/summary")
def paper_summary(request: Request):
    """Return the aggregate paper trading account summary."""
    return _paper_engine(request).summary()


@app.get("/api/paper/positions")
def paper_positions(
    request: Request,
    status: str = Query("all", pattern="^(open|closed|all)$"),
):
    """List paper trading positions, optionally filtered by status."""
    account = _paper_engine(request).get_account()
    if status == "open":
        positions = [p for p in account.positions if p.status == "open"]
    elif status == "closed":
        positions = [p for p in account.positions if p.status == "closed"]
    else:
        positions = account.positions
    return {
        "balance": account.balance,
        "total_pnl": account.total_pnl,
        "count": len(positions),
        "positions": [_serialize_position(p) for p in positions],
    }


@app.post("/api/paper/open")
def open_paper_position(payload: OpenPaperRequest, request: Request):
    """Open a paper position against a logged opportunity."""
    opp = get_opportunity_by_id(request.app.state.db, payload.opportunity_id)
    if opp is None:
        raise HTTPException(
            status_code=404, detail=f"No opportunity with id={payload.opportunity_id}"
        )
    try:
        position = _paper_engine(request).open_position(
            opp, size=payload.size, opportunity_id=payload.opportunity_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_position(position)


@app.post("/api/paper/positions/{position_id}/close")
def paper_close_position(
    position_id: int, payload: PaperClosePayload, request: Request
):
    """Mark-to-market close for an open paper position."""
    engine = _paper_engine(request)
    try:
        pnl = engine.close_position(
            position_id,
            poly_price=payload.poly_price,
            kalshi_price=payload.kalshi_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"position_id": position_id, "realized_pnl": pnl}


@app.post("/api/paper/positions/{position_id}/resolve")
def paper_resolve_position(
    position_id: int, payload: PaperResolvePayload, request: Request
):
    """Close an open paper position at final resolution."""
    engine = _paper_engine(request)
    try:
        pnl = engine.close_resolved_position(position_id, yes_won=payload.yes_won)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"position_id": position_id, "realized_pnl": pnl}


@app.post("/api/paper/close/{position_id}")
def close_paper_position(
    position_id: int, payload: ClosePaperRequest, request: Request
):
    """Close a paper position mark-to-market or at resolution (combined endpoint)."""
    engine = _paper_engine(request)
    try:
        if payload.yes_won is not None:
            realized = engine.close_resolved_position(position_id, payload.yes_won)
        elif payload.poly_price is not None and payload.kalshi_price is not None:
            realized = engine.close_position(
                position_id, payload.poly_price, payload.kalshi_price
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="Must provide either yes_won or (poly_price, kalshi_price)",
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"position_id": position_id, "realized_pnl": realized}


# --- Stripe webhook ---


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events for subscription management."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        logger.info("Checkout completed: customer=%s", session.get("customer_email"))
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        logger.info("Subscription cancelled: %s", sub.get("id"))

    return {"status": "ok"}


@app.get("/api/stripe/checkout")
def create_checkout_session():
    """Create a Stripe Checkout session for the paid plan."""
    if not settings.stripe_secret_key or not settings.stripe_price_id:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url="/?payment=success",
        cancel_url="/?payment=cancelled",
    )
    return {"checkout_url": session.url}


# --- HTML pages ---


@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    """Landing page with pricing and live preview."""
    cache = load_cache()
    stats = get_historical_edge_stats()
    return templates.TemplateResponse(
        request,
        "landing.html",
        context={
            "matched_pairs": len(cache.pairs),
            "stats": stats,
            "stripe_configured": bool(settings.stripe_secret_key),
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """Live arb dashboard (HTML version of the terminal UI)."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    """Backtest + paper trading performance page."""
    return templates.TemplateResponse(request, "backtest.html")
