"""Tests for the alert system."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from arbscanner.alerts import format_alert, send_alerts
from arbscanner.models import ArbOpportunity


def _make_opp(**kwargs) -> ArbOpportunity:
    defaults = dict(
        poly_title="Will X happen?",
        kalshi_title="KX-EVENT",
        poly_market_id="poly_1",
        kalshi_market_id="kalshi_1",
        direction="poly_yes_kalshi_no",
        poly_price=0.40,
        kalshi_price=0.45,
        gross_edge=0.15,
        net_edge=0.10,
        available_size=50,
        expected_profit=5.0,
        timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return ArbOpportunity(**defaults)


def test_format_alert():
    opp = _make_opp()
    msg = format_alert(opp)
    assert "Will X happen?" in msg
    assert "P.Yes + K.No" in msg
    assert "0.400" in msg
    assert "0.450" in msg
    assert "$5.00" in msg


def test_format_alert_direction_2():
    opp = _make_opp(direction="poly_no_kalshi_yes")
    msg = format_alert(opp)
    assert "P.No + K.Yes" in msg


def test_send_alerts_below_threshold():
    """No alerts sent when all opportunities are below threshold."""
    opp = _make_opp(net_edge=0.005)
    count = send_alerts([opp], threshold=0.02, dedup=False)
    assert count == 0


def test_send_alerts_no_config():
    """No alerts sent when neither Telegram nor Discord is configured."""
    opp = _make_opp(net_edge=0.10)
    with patch("arbscanner.alerts.settings") as mock_settings:
        mock_settings.telegram_bot_token = ""
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02
        count = send_alerts([opp], threshold=0.02, dedup=False)
    assert count == 0


def test_send_alerts_telegram():
    """Test successful Telegram alert delivery."""
    opp = _make_opp(net_edge=0.10)

    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", return_value=True) as mock_tg:
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02
        count = send_alerts([opp], threshold=0.02, dedup=False)

    assert count == 1
    mock_tg.assert_called_once()


def test_send_alerts_both_channels():
    """Test sending to both Telegram and Discord."""
    opp = _make_opp(net_edge=0.10)

    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", return_value=True), \
         patch("arbscanner.alerts.send_discord", return_value=True):
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = "https://discord.com/webhook"
        mock_settings.alert_threshold = 0.02
        count = send_alerts([opp], threshold=0.02, dedup=False)

    assert count == 2


def test_send_alerts_dedup_suppresses_repeat():
    """With dedup enabled, repeat alerts within TTL should be suppressed."""
    from arbscanner.alerts import _deduper

    # Reset deduper state to avoid cross-test contamination
    _deduper._entries.clear()  # type: ignore[attr-defined]

    opp = _make_opp(net_edge=0.10, poly_market_id="unique_p", kalshi_market_id="unique_k")

    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", return_value=True):
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02

        first = send_alerts([opp], threshold=0.02, dedup=True)
        second = send_alerts([opp], threshold=0.02, dedup=True)

    assert first == 1
    assert second == 0  # deduped


def test_send_alerts_free_tier_skipped():
    """Free tier skips Telegram/Discord delivery entirely (CLAUDE.md Day 10)."""
    opp = _make_opp(net_edge=0.10, poly_market_id="free_p", kalshi_market_id="free_k")
    mock_tg = MagicMock(return_value=True)
    mock_dc = MagicMock(return_value=True)

    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", mock_tg), \
         patch("arbscanner.alerts.send_discord", mock_dc):
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = "https://discord.com/webhook"
        mock_settings.alert_threshold = 0.02
        mock_settings.tier = "free"

        count = send_alerts([opp], threshold=0.02, dedup=False)

    assert count == 0
    mock_tg.assert_not_called()
    mock_dc.assert_not_called()


def test_send_alerts_pro_tier_delivers():
    """Pro tier (explicit param) still delivers."""
    from arbscanner.alerts import _deduper

    _deduper._entries.clear()  # type: ignore[attr-defined]

    opp = _make_opp(net_edge=0.10, poly_market_id="pro_p", kalshi_market_id="pro_k")
    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", return_value=True) as mock_tg:
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02
        mock_settings.tier = "free"  # global default is free…

        # …but the explicit `tier="pro"` override wins for this call.
        count = send_alerts([opp], threshold=0.02, dedup=False, tier="pro")

    assert count == 1
    mock_tg.assert_called_once()
