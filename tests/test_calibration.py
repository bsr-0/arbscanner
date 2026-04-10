"""Tests for the calibration layer."""

from datetime import datetime, timedelta, timezone

from arbscanner.calibration import (
    CalibrationContext,
    days_to_bucket,
    get_calibration_context,
    normalize_category,
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
