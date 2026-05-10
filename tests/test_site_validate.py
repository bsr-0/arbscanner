"""Tests for static Pages site validation."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbscanner.site.validate import validate_pages_site


def _write_site(
    root: Path,
    *,
    generated_at: datetime | None = None,
    opportunities: list | None = None,
) -> None:
    (root / "index.html").write_text("<!doctype html>")
    payload = {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "matched_pairs": 1,
        "stats": {"total_opportunities": 0},
        "diagnostics": {"raw_opportunities": 0, "unique_opportunities": 0, "duplicate_rows_removed": 0},
        "opportunities": opportunities if opportunities is not None else [],
    }
    (root / "data.json").write_text(json.dumps(payload))


def test_validate_pages_site_accepts_fresh_build():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_site(root)

        result = validate_pages_site(output_dir=root, max_data_age_minutes=30)

        assert result.index_path == root / "index.html"
        assert result.data_path == root / "data.json"
        assert result.opportunities == 0


def test_validate_pages_site_rejects_stale_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_site(
            root,
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=45),
        )

        with pytest.raises(ValueError, match="site data is stale"):
            validate_pages_site(output_dir=root, max_data_age_minutes=30)


def test_validate_pages_site_rejects_missing_keys():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "index.html").write_text("<!doctype html>")
        (root / "data.json").write_text(json.dumps({"generated_at": "2026-01-01T00:00:00+00:00"}))

        with pytest.raises(ValueError, match="missing required key"):
            validate_pages_site(output_dir=root)
