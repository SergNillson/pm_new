import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from polymarket_btc_5m_monitor import _market_from_event, fetch_active_market


class MonitorParsingTests(unittest.TestCase):
    def test_market_selects_up_and_down_tokens_and_target(self):
        now = datetime.now(timezone.utc)
        event = {
            "slug": "btc-updown-5m-test",
            "conditionId": "0xcondition",
            "startDate": (now - timedelta(seconds=10)).isoformat(),
            "endDate": (now + timedelta(seconds=290)).isoformat(),
            "description": "Price to Beat: $67,123.45",
            "markets": [{
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }],
        }
        market = _market_from_event(event)
        self.assertEqual(market.tokens, {"up": "up-token", "down": "down-token"})
        self.assertEqual(market.target, 67123.45)

    @patch("polymarket_btc_5m_monitor.requests.get")
    def test_fetch_active_market_ignores_future_event(self, get):
        now = datetime.now(timezone.utc)
        event = {
            "slug": "future",
            "startDate": (now + timedelta(seconds=10)).isoformat(),
            "endDate": (now + timedelta(seconds=300)).isoformat(),
            "markets": [{
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up", "down"]',
            }],
        }
        response = Mock(status_code=200)
        response.json.return_value = [event]
        get.return_value = response
        self.assertIsNone(fetch_active_market())
