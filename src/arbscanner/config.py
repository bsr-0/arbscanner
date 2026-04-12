"""Settings and constants for arbscanner."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = PROJECT_ROOT / "arbscanner.db"
MATCHED_PAIRS_PATH = DATA_DIR / "matched_pairs.json"
CALIBRATION_DATA_DIR = DATA_DIR / "calibration"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class Settings:
    refresh_interval: int = 30
    edge_threshold: float = 0.01
    poly_fee_rate: float = 0.001
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_threshold: float = 0.7
    llm_confirm_low: float = 0.7
    llm_confirm_high: float = 0.9
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # Data pipeline tuning
    max_workers: int = 8  # ThreadPoolExecutor size for parallel scanning
    rate_limit_per_sec: float = 10.0  # Shared pmxt call rate limit
    retry_attempts: int = 3  # Retry count for transient exchange failures
    retry_base_delay: float = 0.5  # Initial retry backoff in seconds

    # Alerts
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    discord_webhook_url: str = field(
        default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", "")
    )
    alert_threshold: float = 0.02  # net edge threshold for sending alerts

    # Stripe
    stripe_secret_key: str = field(default_factory=lambda: os.getenv("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(
        default_factory=lambda: os.getenv("STRIPE_WEBHOOK_SECRET", "")
    )
    stripe_price_id: str = field(default_factory=lambda: os.getenv("STRIPE_PRICE_ID", ""))

    # Web
    secret_key: str = field(
        default_factory=lambda: os.getenv("ARBSCANNER_SECRET_KEY", "dev-secret-key")
    )

    # Free/Pro tier gating (CLAUDE.md Day 10 — Free: top 3 opps, 5-min
    # delayed alerts; Pro: real-time, full table, calibration context,
    # Telegram alerts). Default is "pro" so a fresh self-hosted checkout
    # sees everything; set ARBSCANNER_TIER=free on demo deployments to
    # enforce the free-tier caps described on the landing page.
    tier: str = field(default_factory=lambda: os.getenv("ARBSCANNER_TIER", "pro").lower())


# Free-tier constants from the CLAUDE.md Day 10 spec.
FREE_MAX_OPPORTUNITIES: int = 3
FREE_ALERT_DELAY_SECONDS: int = 300  # 5-minute lag on what free users can see


# Kalshi exact bracket-based fee schedule (per contract, symmetric around 50c).
# Each entry: (price_low, price_high, fee_cents).
# Price is in [0.0, 1.0]. Fee is absolute cents per contract.
KALSHI_FEE_BRACKETS: list[tuple[float, float, float]] = [
    (0.00, 0.10, 0.015),
    (0.10, 0.25, 0.025),
    (0.25, 0.50, 0.035),
    (0.50, 0.75, 0.035),
    (0.75, 0.90, 0.025),
    (0.90, 1.00, 0.015),
]


def kalshi_fee(price: float) -> float:
    """Return the Kalshi taker fee for a contract at the given price (0.0-1.0)."""
    for low, high, fee in KALSHI_FEE_BRACKETS:
        if low <= price < high:
            return fee
    return 0.035  # fallback to max bracket


def poly_fee(price: float, fee_rate: float = 0.001) -> float:
    """Return the Polymarket taker fee for a contract at the given price."""
    return price * fee_rate


settings = Settings()
