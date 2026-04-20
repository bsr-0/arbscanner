"""Tests for the arbscanner doctor pre-flight command."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run_doctor(extra_args: list[str] | None = None, monkeypatch=None):
    """Import and invoke cmd_doctor with an argparse-like namespace."""
    import argparse

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    if extra_args:
        for k, v in (a.split("=") for a in extra_args):
            setattr(ns, k.lstrip("-").replace("-", "_"), v)

    cmd_doctor(ns)


# ---------------------------------------------------------------------------
# Happy-path: all optional env vars unset, connectivity skipped
# ---------------------------------------------------------------------------


def test_doctor_runs_without_crashing(monkeypatch):
    """Smoke test: doctor must not raise even when nothing is configured."""
    # Wipe optional env vars so we get deterministic WARN output.
    for var in (
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
        "POLY_API_KEY",
        "POLY_API_SECRET",
        "POLY_PASSPHRASE",
        "POLY_PRIVATE_KEY",
        "KALSHI_API_KEY",
        "KALSHI_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # No FAILs expected (node/pmxtjs are FAIL but we check exit code separately).
    # The important invariant is: no Python exception is raised.
    import argparse

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    try:
        cmd_doctor(ns)
    except SystemExit:
        pass  # exit(1) due to FAIL items is expected; the function itself must not crash


# ---------------------------------------------------------------------------
# matched_pairs.json detection
# ---------------------------------------------------------------------------


def test_doctor_detects_missing_matched_pairs(monkeypatch, tmp_path):
    """WARN emitted when matched_pairs.json doesn't exist."""
    import argparse

    from arbscanner import config as cfg

    monkeypatch.setattr(cfg, "MATCHED_PAIRS_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    assert "matched_pairs" in combined.lower()


def test_doctor_detects_populated_matched_pairs(monkeypatch, tmp_path):
    """PASS emitted and pair count shown when matched_pairs.json is present."""
    import argparse
    import json

    from arbscanner import config as cfg

    pairs_path = tmp_path / "matched_pairs.json"
    pairs_path.write_text(
        json.dumps({"pairs": [{"poly_market_id": "a", "kalshi_market_id": "b"}]})
    )
    monkeypatch.setattr(cfg, "MATCHED_PAIRS_PATH", pairs_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    assert "1 confirmed pair" in combined


# ---------------------------------------------------------------------------
# Database detection
# ---------------------------------------------------------------------------


def test_doctor_detects_missing_db(monkeypatch, tmp_path):
    import argparse

    from arbscanner import config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(cfg, "MATCHED_PAIRS_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    assert "arbscanner.db" in combined


def test_doctor_detects_existing_db(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    from arbscanner import config as cfg

    db = tmp_path / "arb.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE opportunities (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(cfg, "DB_PATH", db)
    monkeypatch.setattr(cfg, "MATCHED_PAIRS_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    assert "opportunities" in combined


# ---------------------------------------------------------------------------
# Credential checks
# ---------------------------------------------------------------------------


def test_doctor_warns_missing_anthropic_key(monkeypatch):
    import argparse

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    assert "ANTHROPIC_API_KEY" in combined


def test_doctor_passes_anthropic_key_when_set(monkeypatch):
    import argparse

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-1234")

    from arbscanner.cli import cmd_doctor

    ns = argparse.Namespace(skip_connectivity=True)
    output_lines: list[str] = []

    with patch("arbscanner.cli.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
        try:
            cmd_doctor(ns)
        except SystemExit:
            pass

    combined = " ".join(output_lines)
    # 16 chars
    assert "16 chars" in combined


# ---------------------------------------------------------------------------
# node / pmxtjs detection
# ---------------------------------------------------------------------------


def test_doctor_fails_when_node_missing(monkeypatch):
    import argparse

    with patch("shutil.which", return_value=None):
        from arbscanner.cli import cmd_doctor

        ns = argparse.Namespace(skip_connectivity=True)
        output_lines: list[str] = []

        with patch("arbscanner.cli.console") as mock_console:
            mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
            try:
                cmd_doctor(ns)
            except SystemExit as exc:
                assert exc.code == 1

    combined = " ".join(output_lines)
    assert "node" in combined.lower()


def test_doctor_detects_pmxtjs_when_present(monkeypatch):
    import argparse

    def fake_which(name):
        return f"/usr/local/bin/{name}" if name in ("node", "pmxtjs") else None

    import subprocess

    with (
        patch("shutil.which", side_effect=fake_which),
        patch(
            "subprocess.check_output",
            return_value=b"v20.0.0",
        ),
    ):
        from arbscanner.cli import cmd_doctor

        ns = argparse.Namespace(skip_connectivity=True)
        output_lines: list[str] = []

        with patch("arbscanner.cli.console") as mock_console:
            mock_console.print.side_effect = lambda *a, **kw: output_lines.append(str(a))
            try:
                cmd_doctor(ns)
            except SystemExit:
                pass

    combined = " ".join(output_lines)
    assert "pmxtjs" in combined.lower()
