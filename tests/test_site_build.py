"""Tests for the static Pages site builder."""

import tempfile
from pathlib import Path

from arbscanner.site.build import build_pages_site


def test_build_pages_site_copies_template_and_writes_data(monkeypatch):
    """Site build should copy the template and regenerate data.json."""
    from arbscanner.site import build as build_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        template = tmp / "template.html"
        template.write_text("<!doctype html><title>template</title>")

        called = {}

        def _fake_export_dashboard_data(*, hours, min_edge, limit, output_path):
            called.update(
                {
                    "hours": hours,
                    "min_edge": min_edge,
                    "limit": limit,
                    "output_path": output_path,
                }
            )
            output_path.write_text('{"ok": true}')
            return output_path

        monkeypatch.setattr(build_mod, "export_dashboard_data", _fake_export_dashboard_data)

        result = build_pages_site(
            hours=12,
            min_edge=0.02,
            limit=7,
            output_dir=tmp / "dist",
            template_path=template,
        )

        assert result.index_path.read_text() == template.read_text()
        assert result.data_path.read_text() == '{"ok": true}'
        assert called == {
            "hours": 12,
            "min_edge": 0.02,
            "limit": 7,
            "output_path": result.data_path,
        }
