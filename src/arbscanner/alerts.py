"""Telegram and Discord alert delivery for arb opportunities."""

import logging

import httpx

from arbscanner.alerts_dedup import AlertDeduper
from arbscanner.config import settings
from arbscanner.models import ArbOpportunity

logger = logging.getLogger(__name__)

# Module-level deduper: suppresses repeat alerts for the same opportunity
# fingerprint within a 5-minute window, unless the edge changes by >0.5pt.
_deduper = AlertDeduper(ttl_seconds=300, edge_delta=0.005)


def format_alert(opp: ArbOpportunity) -> str:
    """Format an arb opportunity as a human-readable alert message."""
    direction = "P.Yes + K.No" if opp.direction == "poly_yes_kalshi_no" else "P.No + K.Yes"
    return (
        f"🔔 Arb Alert: {opp.poly_title}\n"
        f"Direction: {direction}\n"
        f"Poly: {opp.poly_price:.3f} | Kalshi: {opp.kalshi_price:.3f}\n"
        f"Gross Edge: {opp.gross_edge:.1%} | Net Edge: {opp.net_edge:.1%}\n"
        f"Size: {opp.available_size:.0f} contracts | Profit: ${opp.expected_profit:.2f}"
    )


def send_telegram(message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram message")
        return False


def send_discord(message: str) -> bool:
    """Send a message via Discord webhook. Returns True on success."""
    webhook_url = settings.discord_webhook_url
    if not webhook_url:
        return False

    try:
        resp = httpx.post(webhook_url, json={"content": message}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Discord message")
        return False


def send_alerts(
    opportunities: list[ArbOpportunity],
    threshold: float | None = None,
    dedup: bool = True,
    tier: str | None = None,
) -> int:
    """Send alerts for opportunities above the alert threshold.

    Deduplicates repeat alerts via the module-level AlertDeduper unless
    `dedup=False` is passed (useful for tests).

    Tier gating (CLAUDE.md Day 10): Telegram / Discord alerts are an
    explicit Pro-tier feature. Under the ``free`` tier we skip delivery
    entirely and return ``0`` — the free tier's 5-minute delayed view on
    ``/api/opportunities`` is their only alert surface.

    Returns the number of alerts successfully sent.
    """
    if tier is None:
        tier = settings.tier
    if tier.lower() == "free":
        logger.debug("Skipping %d alert(s): free tier does not receive push alerts", len(opportunities))
        return 0

    if threshold is None:
        threshold = settings.alert_threshold

    alertable = [o for o in opportunities if o.net_edge >= threshold]
    if not alertable:
        return 0

    if dedup:
        filtered = _deduper.filter(alertable)
        if len(filtered) < len(alertable):
            logger.info(
                "Deduped %d repeat alerts, %d new",
                len(alertable) - len(filtered),
                len(filtered),
            )
        alertable = filtered

    if not alertable:
        return 0

    sent = 0
    for opp in alertable:
        message = format_alert(opp)
        if settings.telegram_bot_token:
            if send_telegram(message):
                sent += 1
        if settings.discord_webhook_url:
            if send_discord(message):
                sent += 1

    logger.info("Sent %d alerts for %d opportunities", sent, len(alertable))
    return sent
