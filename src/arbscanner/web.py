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

from arbscanner.calibration import get_calibration_context, get_historical_edge_stats
from arbscanner.config import DB_PATH, TEMPLATES_DIR, settings
from arbscanner.db import get_connection
from arbscanner.health import router as health_router
from arbscanner.matcher import load_cache

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup."""
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
    app.state.db = get_connection()
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
        SELECT timestamp, poly_market_id, kalshi_market_id, market_title,
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
            "timestamp": row[0],
            "poly_market_id": row[1],
            "kalshi_market_id": row[2],
            "market_title": row[3],
            "direction": row[4],
            "gross_edge": row[5],
            "net_edge": row[6],
            "available_size": row[7],
            "expected_profit": row[8],
            "poly_price": row[9],
            "kalshi_price": row[10],
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
