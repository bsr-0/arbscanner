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
    count = send_alerts([opp], threshold=0.02)
    assert count == 0


def test_send_alerts_no_config():
    """No alerts sent when neither Telegram nor Discord is configured."""
    opp = _make_opp(net_edge=0.10)
    with patch("arbscanner.alerts.settings") as mock_settings:
        mock_settings.telegram_bot_token = ""
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02
        count = send_alerts([opp], threshold=0.02)
    assert count == 0


def test_send_alerts_telegram():
    """Test successful Telegram alert delivery."""
    opp = _make_opp(net_edge=0.10)

    with patch("arbscanner.alerts.settings") as mock_settings, \
         patch("arbscanner.alerts.send_telegram", return_value=True) as mock_tg:
        mock_settings.telegram_bot_token = "fake-token"
        mock_settings.discord_webhook_url = ""
        mock_settings.alert_threshold = 0.02
        count = send_alerts([opp], threshold=0.02)

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
        count = send_alerts([opp], threshold=0.02)

    assert count == 2
