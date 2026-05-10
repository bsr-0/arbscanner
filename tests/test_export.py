"""Tests for GitHub Pages dashboard export."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from arbscanner.db import get_connection, log_opportunities
from arbscanner.export import export_dashboard_data
from arbscanner.models import ArbOpportunity, MatchedPair, MatchedPairsCache


def _opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Poly Title",
        kalshi_title="Kalshi Title",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.42,
        kalshi_price=0.43,
        gross_edge=0.15,
        net_edge=0.12,
        available_size=10,
        expected_profit=1.2,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def test_export_dashboard_data_dedupes_rows_and_joins_pair_titles(monkeypatch):
    """Static export should keep one row per pair-direction and expose both sides."""
    from arbscanner import export as export_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "export.db"
        out_path = Path(tmpdir) / "data.json"
        conn = get_connection(db_path)
        log_opportunities(
            conn,
            [
                _opp(expected_profit=5.0, timestamp=datetime.now(timezone.utc)),
                _opp(expected_profit=3.0, timestamp=datetime.now(timezone.utc)),
                _opp(
                    poly_market_id="poly_2",
                    kalshi_market_id="kalshi_2",
                    poly_title="Second Poly",
                    kalshi_title="Second Kalshi",
                    direction="poly_no_kalshi_yes",
                    expected_profit=2.0,
                ),
            ],
        )
        conn.close()

        cache = MatchedPairsCache(
            pairs=[
                MatchedPair(
                    poly_market_id="poly_1",
                    poly_title="Poly Title",
                    kalshi_market_id="kalshi_1",
                    kalshi_title="Kalshi Title",
                    confidence=0.93,
                    source="embedding+llm",
                    matched_at="2026-04-10T00:00:00Z",
                ),
                MatchedPair(
                    poly_market_id="poly_2",
                    poly_title="Second Poly",
                    kalshi_market_id="kalshi_2",
                    kalshi_title="Second Kalshi",
                    confidence=0.91,
                    source="embedding",
                    matched_at="2026-04-10T00:00:00Z",
                ),
            ]
        )

        monkeypatch.setattr(export_mod, "get_connection", lambda: get_connection(db_path))
        monkeypatch.setattr(export_mod, "load_cache", lambda: cache)

        export_dashboard_data(output_path=out_path, hours=24, limit=10)
        data = json.loads(out_path.read_text())

        assert data["matched_pairs"] == 2
        assert data["diagnostics"]["raw_opportunities"] == 3
        assert data["diagnostics"]["duplicate_rows_removed"] == 1
        assert data["stats"]["total_opportunities"] == 2

        first = data["opportunities"][0]
        assert first["poly_title"] == "Poly Title"
        assert first["kalshi_title"] == "Kalshi Title"
        assert first["match_source"] == "embedding+llm"
        assert first["expected_profit"] == 5.0
