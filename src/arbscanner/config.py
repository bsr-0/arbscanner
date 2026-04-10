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
