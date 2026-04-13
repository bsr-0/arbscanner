"""Tests for ``arbscanner doctor`` preflight checks.

These tests patch subprocess / filesystem / import surfaces rather than
actually shelling out, so they run offline and deterministically.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from arbscanner import doctor


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_python_version_ok():
    result = doctor.check_python_version()
    # Tests run on the supported interpreter, so this must always pass.
    assert result.severity == "ok"
    assert "Python" in result.message


def test_python_version_fail_below_312():
    with patch.object(doctor.sys, "version_info", (3, 11, 5, "final", 0)):
        result = doctor.check_python_version()
    assert result.severity == "fail"
    assert "3.11" in result.message


def test_python_version_fail_above_312():
    """3.13+ is pinned out because torch 2.2.2 has no cp313/cp314 wheels."""
    with patch.object(doctor.sys, "version_info", (3, 14, 0, "final", 0)):
        result = doctor.check_python_version()
    assert result.severity == "fail"
    assert "3.14" in result.message
    assert "3.12" in result.message
    assert "uv python install 3.12" in result.fix


def test_python_version_fail_313():
    with patch.object(doctor.sys, "version_info", (3, 13, 1, "final", 0)):
        result = doctor.check_python_version()
    assert result.severity == "fail"
    assert "3.13" in result.message


def test_pmxt_missing():
    with patch.object(doctor.importlib.util, "find_spec", return_value=None):
        result = doctor.check_pmxt()
    assert result.severity == "fail"
    assert "pmxt" in result.message.lower()
    assert "uv sync" in result.fix or "pip install" in result.fix


def test_node_missing():
    with patch.object(doctor.shutil, "which", return_value=None):
        result = doctor.check_node()
    assert result.severity == "fail"
    assert "PATH" in result.fix or "nodejs" in result.fix.lower()


def test_node_too_old(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/node")

    class _FakeOut:
        stdout = "v16.20.0\n"

    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *a, **kw: _FakeOut(),
    )
    result = doctor.check_node()
    assert result.severity == "fail"
    assert "16" in result.message


def test_node_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/node")

    class _FakeOut:
        stdout = "v20.10.0\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: _FakeOut())
    result = doctor.check_node()
    assert result.severity == "ok"
    assert "20.10.0" in result.message


def test_pmxtjs_missing():
    with patch.object(doctor.shutil, "which", return_value=None):
        result = doctor.check_pmxtjs()
    assert result.severity == "fail"
    assert "npm install -g pmxtjs" in result.fix


def test_pmxtjs_ok():
    with patch.object(doctor.shutil, "which", return_value="/usr/local/bin/pmxtjs"):
        result = doctor.check_pmxtjs()
    assert result.severity == "ok"


def test_env_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    result = doctor.check_env_file()
    assert result.severity == "warn"
    assert ".env.example" in result.fix


def test_env_file_present(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("FOO=bar\n")
    result = doctor.check_env_file()
    assert result.severity == "ok"


def test_anthropic_key_missing(monkeypatch):
    monkeypatch.setattr(doctor.settings, "anthropic_api_key", "")
    result = doctor.check_anthropic_key()
    assert result.severity == "warn"
    # Should mention the high-confidence threshold fallback so the user knows
    # what they're giving up without a key.
    assert f"{doctor.settings.llm_confirm_high:.2f}" in result.message


def test_anthropic_key_present(monkeypatch):
    monkeypatch.setattr(doctor.settings, "anthropic_api_key", "sk-ant-redacted")
    result = doctor.check_anthropic_key()
    assert result.severity == "ok"


def test_matched_pairs_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "MATCHED_PAIRS_PATH", tmp_path / "nope.json")
    result = doctor.check_matched_pairs()
    assert result.severity == "warn"
    assert "match" in result.fix.lower()


def test_matched_pairs_empty(tmp_path, monkeypatch):
    path = tmp_path / "matched_pairs.json"
    path.write_text(json.dumps({"pairs": []}))
    monkeypatch.setattr(doctor, "MATCHED_PAIRS_PATH", path)
    result = doctor.check_matched_pairs()
    assert result.severity == "warn"
    assert "0 pairs" in result.message


def test_matched_pairs_populated(tmp_path, monkeypatch):
    path = tmp_path / "matched_pairs.json"
    path.write_text(json.dumps({"pairs": [{"poly_market_id": "p1"}, {"poly_market_id": "p2"}]}))
    monkeypatch.setattr(doctor, "MATCHED_PAIRS_PATH", path)
    result = doctor.check_matched_pairs()
    assert result.severity == "ok"
    assert "2" in result.message


def test_matched_pairs_corrupt(tmp_path, monkeypatch):
    path = tmp_path / "matched_pairs.json"
    path.write_text("{this isn't json")
    monkeypatch.setattr(doctor, "MATCHED_PAIRS_PATH", path)
    result = doctor.check_matched_pairs()
    assert result.severity == "fail"
    assert "unreadable" in result.message.lower() or "Delete" in result.fix


def test_database_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "DB_PATH", tmp_path / "arb.db")
    result = doctor.check_database()
    assert result.severity == "ok"
    assert (tmp_path / "arb.db").exists()


def test_calibration_info_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CALIBRATION_DATA_DIR", tmp_path / "nope")
    result = doctor.check_calibration_data()
    # Calibration is explicitly optional — missing data should be "info"
    # not "fail", so self-hosted deployments aren't blocked by it.
    assert result.severity == "info"


def test_calibration_ok_with_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "cal"
    data_dir.mkdir()
    (data_dir / "profiles.json").write_text("{}")
    monkeypatch.setattr(doctor, "CALIBRATION_DATA_DIR", data_dir)
    result = doctor.check_calibration_data()
    assert result.severity == "ok"


def test_alerts_none_configured(monkeypatch):
    monkeypatch.setattr(doctor.settings, "telegram_bot_token", "")
    monkeypatch.setattr(doctor.settings, "telegram_chat_id", "")
    monkeypatch.setattr(doctor.settings, "discord_webhook_url", "")
    result = doctor.check_alert_sinks()
    assert result.severity == "info"


def test_alerts_configured(monkeypatch):
    monkeypatch.setattr(doctor.settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(doctor.settings, "telegram_chat_id", "chat")
    monkeypatch.setattr(doctor.settings, "discord_webhook_url", "")
    monkeypatch.setattr(doctor.settings, "tier", "pro")
    result = doctor.check_alert_sinks()
    assert result.severity == "ok"
    assert "telegram" in result.message


def test_alerts_warn_when_free_tier_silences(monkeypatch):
    """The free tier drops alerts silently — flag that foot-gun."""
    monkeypatch.setattr(doctor.settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(doctor.settings, "telegram_chat_id", "chat")
    monkeypatch.setattr(doctor.settings, "discord_webhook_url", "")
    monkeypatch.setattr(doctor.settings, "tier", "free")
    result = doctor.check_alert_sinks()
    assert result.severity == "warn"
    assert "free" in result.message.lower()


# ---------------------------------------------------------------------------
# Runner + exit code
# ---------------------------------------------------------------------------


def test_exit_code_ok_only():
    results = [doctor.CheckResult(name="x", severity="ok", message="ok")]
    assert doctor.exit_code(results) == 0


def test_exit_code_warn_is_still_zero():
    results = [doctor.CheckResult(name="x", severity="warn", message="meh")]
    assert doctor.exit_code(results) == 0


def test_exit_code_fail():
    results = [
        doctor.CheckResult(name="x", severity="ok", message="ok"),
        doctor.CheckResult(name="y", severity="fail", message="nope"),
    ]
    assert doctor.exit_code(results) == 1


def test_run_all_checks_offline_does_not_include_network(monkeypatch):
    """Without --network, check_network_pmxt must not be called."""
    called = {"network": False}

    def _boom():
        called["network"] = True
        raise AssertionError("network check ran in offline mode")

    monkeypatch.setattr(doctor, "check_network_pmxt", _boom)
    results = doctor.run_all_checks(include_network=False)
    assert called["network"] is False
    assert len(results) == len(doctor.OFFLINE_CHECKS)


def test_run_all_checks_isolates_raising_checks(monkeypatch):
    """A single check that raises must not abort the whole run."""

    def _boom():
        raise RuntimeError("unexpected")

    # Swap in a raising check at the front so we can observe it gets absorbed.
    monkeypatch.setattr(doctor, "OFFLINE_CHECKS", [_boom, doctor.check_python_version])

    results = doctor.run_all_checks(include_network=False)
    assert len(results) == 2
    assert results[0].severity == "fail"
    assert "unexpected" in results[0].message
    # Subsequent checks still ran.
    assert results[1].name == "python"


def test_render_does_not_crash_on_minimal_results(capsys):
    """Just a smoke test — render() must not blow up on any severity."""
    from rich.console import Console

    results = [
        doctor.CheckResult(name="a", severity="ok", message="ok"),
        doctor.CheckResult(name="b", severity="info", message="info"),
        doctor.CheckResult(name="c", severity="warn", message="warn", fix="do X"),
        doctor.CheckResult(name="d", severity="fail", message="bad", fix="fix Y"),
    ]
    doctor.render(results, console=Console(force_terminal=False, no_color=True))
    captured = capsys.readouterr().out
    assert "a" in captured and "d" in captured
    assert "do X" in captured
    assert "fix Y" in captured
