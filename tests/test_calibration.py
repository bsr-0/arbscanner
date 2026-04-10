"""Tests for the calibration layer."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from arbscanner.calibration import (
    CalibrationContext,
    compute_calibration_curves,
    days_to_bucket,
    get_calibration_context,
    normalize_category,
    _validate_schema,
)


def test_days_to_bucket():
    assert days_to_bucket(0) == "0-7"
    assert days_to_bucket(3) == "0-7"
    assert days_to_bucket(7) == "0-7"
    assert days_to_bucket(8) == "7-30"
    assert days_to_bucket(30) == "7-30"
    assert days_to_bucket(31) == "30-90"
    assert days_to_bucket(90) == "30-90"
    assert days_to_bucket(91) == "90+"
    assert days_to_bucket(365) == "90+"
    assert days_to_bucket(None) == "90+"


def test_normalize_category():
    assert normalize_category("Politics") == "politics"
    assert normalize_category("ECONOMICS") == "economics"
    assert normalize_category("Pop Culture") == "entertainment"
    assert normalize_category("Crypto") == "crypto"
    assert normalize_category("Bitcoin price") == "crypto"
    assert normalize_category("NFL Football") == "sports"
    assert normalize_category(None) == "other"
    assert normalize_category("") == "other"
    assert normalize_category("Weather") == "other"


def test_get_calibration_context_real_edge():
    """Test calibration context for a large edge in an entertainment market."""
    resolution = datetime.now(timezone.utc) + timedelta(days=45)
    ctx = get_calibration_context("entertainment", resolution, net_edge=0.10)

    assert ctx.category == "entertainment"
    assert ctx.time_bucket == "30-90"
    assert ctx.avg_mispricing == 8.0  # default profile value
    assert ctx.edge_likely_real is True
    assert "exceeds typical mispricing" in ctx.confidence_note


def test_get_calibration_context_noise():
    """Test calibration context for a small edge in an efficient politics market."""
    resolution = datetime.now(timezone.utc) + timedelta(days=2)
    ctx = get_calibration_context("politics", resolution, net_edge=0.01)

    assert ctx.category == "politics"
    assert ctx.time_bucket == "0-7"
    assert ctx.avg_mispricing == 1.5
    assert ctx.edge_likely_real is False
    assert "within normal range" in ctx.confidence_note


def test_get_calibration_context_no_resolution_date():
    """Test calibration with unknown resolution date."""
    ctx = get_calibration_context("economics", None, net_edge=0.05)

    assert ctx.days_to_resolution is None
    assert ctx.time_bucket == "90+"
    assert "resolution date unknown" in ctx.confidence_note


def test_get_calibration_context_unknown_category():
    """Test calibration with an unmapped category."""
    resolution = datetime.now(timezone.utc) + timedelta(days=10)
    ctx = get_calibration_context("Weather", resolution, net_edge=0.03)

    assert ctx.category == "other"
    # Should use fallback mispricing of 5.0
    assert ctx.avg_mispricing == 5.0


def test_validate_schema_missing_columns():
    """_validate_schema should raise on missing required columns."""
    df = pd.DataFrame({"category": ["politics"], "final_price": [0.5]})
    with pytest.raises(ValueError, match="missing required columns"):
        _validate_schema(df)


def test_validate_schema_all_present():
    """_validate_schema should pass when all required columns exist."""
    df = pd.DataFrame(
        {
            "category": ["politics"],
            "resolution_date": [datetime(2026, 1, 1)],
            "created_date": [datetime(2025, 12, 1)],
            "final_price": [0.5],
            "resolved_yes": [True],
        }
    )
    _validate_schema(df)  # should not raise


def test_compute_calibration_curves():
    """Compute curves from a synthetic Parquet dataset and verify shape."""
    df = pd.DataFrame(
        {
            "category": ["Politics", "Politics", "Sports", "Sports", "Entertainment"],
            "created_date": [
                datetime(2026, 1, 1),
                datetime(2026, 3, 1),
                datetime(2026, 1, 1),
                datetime(2026, 3, 1),
                datetime(2026, 1, 1),
            ],
            "resolution_date": [
                datetime(2026, 1, 5),  # 4 days  → 0-7
                datetime(2026, 3, 5),  # 4 days  → 0-7
                datetime(2026, 2, 1),  # 31 days → 30-90
                datetime(2026, 4, 1),  # 31 days → 30-90
                datetime(2026, 7, 1),  # 181 days → 90+
            ],
            "final_price": [0.6, 0.4, 0.7, 0.3, 0.5],
            "resolved_yes": [True, False, True, False, True],
        }
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "historical.parquet"
        df.to_parquet(path)

        # Redirect the output path to the temp dir by monkey-patching the module constant
        import arbscanner.calibration as cal_mod

        original = cal_mod.CALIBRATION_DATA_DIR
        cal_mod.CALIBRATION_DATA_DIR = Path(tmpdir) / "calibration"
        try:
            curves = compute_calibration_curves(path)
        finally:
            cal_mod.CALIBRATION_DATA_DIR = original

        # Should have one row per (category, time_bucket) combo
        assert len(curves) == 3
        assert set(curves.columns) >= {"category", "time_bucket", "avg_mispricing", "count"}
        # Politics 0-7 should have 2 entries
        politics_row = curves[
            (curves["category"] == "politics") & (curves["time_bucket"] == "0-7")
        ].iloc[0]
        assert politics_row["count"] == 2
