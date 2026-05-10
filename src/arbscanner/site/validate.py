"""Validation helpers for the static Pages dashboard build."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbscanner.config import PAGES_DATA_PATH, PAGES_DIST_DIR, PAGES_INDEX_PATH


@dataclass
class ValidationResult:
    """Summary of a successful site validation."""

    index_path: Path
    data_path: Path
    generated_at: datetime
    opportunities: int


def validate_pages_site(
    *,
    output_dir: Path | None = None,
    max_data_age_minutes: int = 30,
) -> ValidationResult:
    """Validate that the built Pages site exists and the payload is fresh.

    Raises ``ValueError`` when the build output is missing, malformed, or too
    old to be considered publishable.
    """
    output_dir = output_dir or PAGES_DIST_DIR
    index_path = output_dir / PAGES_INDEX_PATH.name
    data_path = output_dir / PAGES_DATA_PATH.name

    if not index_path.exists():
        raise ValueError(f"missing site index: {index_path}")
    if not data_path.exists():
        raise ValueError(f"missing site data: {data_path}")

    try:
        payload = json.loads(data_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid site data JSON: {data_path}") from exc

    for key in ("generated_at", "matched_pairs", "stats", "diagnostics", "opportunities"):
        if key not in payload:
            raise ValueError(f"site data missing required key: {key}")

    try:
        generated_at = datetime.fromisoformat(
            payload["generated_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("site data has invalid generated_at timestamp") from exc

    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=max_data_age_minutes)
    future_skew = timedelta(minutes=5)

    if generated_at > now + future_skew:
        raise ValueError(
            f"site data generated_at is implausibly in the future: {generated_at.isoformat()}"
        )
    if now - generated_at > max_age:
        raise ValueError(
            "site data is stale: "
            f"generated_at={generated_at.isoformat()} exceeds {max_data_age_minutes} minutes"
        )

    opportunities = payload["opportunities"]
    if not isinstance(opportunities, list):
        raise ValueError("site data opportunities must be a list")

    return ValidationResult(
        index_path=index_path,
        data_path=data_path,
        generated_at=generated_at,
        opportunities=len(opportunities),
    )
