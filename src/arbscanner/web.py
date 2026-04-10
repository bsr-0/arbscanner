"""FastAPI web backend for arbscanner dashboard."""

import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import stripe
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from arbscanner.backtest import compute_backtest_report
from arbscanner.calibration import get_calibration_context, get_historical_edge_stats
from arbscanner.config import DB_PATH, TEMPLATES_DIR, settings
from arbscanner.db import get_connection, get_opportunity_by_id
from arbscanner.health import router as health_router
from arbscanner.matcher import load_cache
from arbscanner.paper_trading import PaperTradingEngine

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup."""
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
    app.state.db = get_connection()
    app.state.paper = PaperTradingEngine()
    app.state.start_time = time.time()
    yield
    app.state.db.close()


app = FastAPI(title="ArbScanner", version="0.2.0", lifespan=lifespan)
app.include_router(health_router)


# --- JSON API endpoints ---


@app.get("/api/opportunities")
def get_opportunities(
    limit: int = Query(50, ge=1, le=500),
    min_edge: float = Query(0.0, ge=0.0),
    hours: int = Query(24, ge=1, le=168),
):
    """Get recent arb opportunities from the database."""
    conn = app.state.db
    # ISO 8601 strings sort lexicographically when timezone-normalized, so a
    # direct string comparison is correct here.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
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


@app.get("/api/calibration")
def get_calibration(
    category: str = Query("politics"),
    days_to_resolution: int | None = Query(None),
    net_edge: float = Query(0.01),
):
    """Get calibration context for a hypothetical opportunity."""
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


# --- Paper trading endpoints ---


class OpenPaperRequest(BaseModel):
    """Payload for POST /api/paper/open."""

    opportunity_id: int
    size: float | None = Field(default=None, ge=0.0)


class ClosePaperRequest(BaseModel):
    """Payload for POST /api/paper/close.

    Exactly one of the two modes must be supplied:

    * ``poly_price`` and ``kalshi_price`` — mark-to-market close.
    * ``yes_won`` — resolve the position at final outcome.
    """

    poly_price: float | None = Field(default=None, ge=0.0, le=1.0)
    kalshi_price: float | None = Field(default=None, ge=0.0, le=1.0)
    yes_won: bool | None = None


def _position_to_dict(position) -> dict:
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


@app.get("/api/paper/summary")
def get_paper_summary():
    """Return the paper trading engine's lightweight account summary."""
    return app.state.paper.summary()


@app.get("/api/paper/positions")
def get_paper_positions(status: str | None = Query(None, pattern="^(open|closed)$")):
    """List paper positions, optionally filtered by status."""
    account = app.state.paper.get_account()
    positions = account.positions
    if status:
        positions = [p for p in positions if p.status == status]
    return {
        "balance": account.balance,
        "total_pnl": account.total_pnl,
        "positions": [_position_to_dict(p) for p in positions],
    }


@app.post("/api/paper/open")
def open_paper_position(payload: OpenPaperRequest):
    """Open a paper position against a logged opportunity."""
    opp = get_opportunity_by_id(app.state.db, payload.opportunity_id)
    if opp is None:
        raise HTTPException(
            status_code=404, detail=f"No opportunity with id={payload.opportunity_id}"
        )
    try:
        position = app.state.paper.open_position(
            opp, size=payload.size, opportunity_id=payload.opportunity_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _position_to_dict(position)


@app.post("/api/paper/close/{position_id}")
def close_paper_position(position_id: int, payload: ClosePaperRequest):
    """Close a paper position mark-to-market or at resolution."""
    engine = app.state.paper
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
