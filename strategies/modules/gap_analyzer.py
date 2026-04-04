"""
Gap Analyzer Module
Calculate and analyze BTC price gaps between current market price
and Polymarket settlement reference price.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GapAnalyzer:
    """
    Analyzes BTC price gaps between the current spot price
    and the Polymarket market reference price.
    """

    def __init__(self, clob_client=None):
        """
        Initialize with an optional CLOB client for market data.

        Args:
            clob_client: py_clob_client ClobClient instance (can be None in dry-run).
        """
        self.clob_client = clob_client
        self._price_cache: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_gap(self, market_id: str, current_btc_price: float) -> float:
        """
        Calculate gap between current BTC price and market reference price.

        Args:
            market_id: Polymarket market identifier.
            current_btc_price: Latest BTC spot price (e.g. from Binance).

        Returns:
            Gap in dollars.  Positive → price is ABOVE reference (UP gap).
            Negative → price is BELOW reference (DOWN gap).
        """
        reference_price = self._get_reference_price(market_id, current_btc_price)
        gap = current_btc_price - reference_price
        logger.debug(
            "Market %s | BTC=%.2f | ref=%.2f | gap=%.2f",
            market_id,
            current_btc_price,
            reference_price,
            gap,
        )
        return gap

    def check_multi_timeframe_alignment(
        self,
        gap_5m: float,
        gap_15m: Optional[float] = None,
        gap_1h: Optional[float] = None,
    ) -> bool:
        """
        Verify that the gap direction is consistent across multiple timeframes.

        Args:
            gap_5m:  Gap computed on the 5-minute market.
            gap_15m: Gap computed on the 15-minute market (None → skip).
            gap_1h:  Gap computed on the 1-hour market (None → skip).

        Returns:
            True if all provided gaps point in the same direction, False otherwise.
        """
        if gap_5m == 0:
            return False

        direction_5m = gap_5m > 0

        for label, other_gap in [("15m", gap_15m), ("1h", gap_1h)]:
            if other_gap is None:
                continue
            if other_gap == 0:
                logger.debug("Multi-TF alignment failed: %s gap is zero", label)
                return False
            if (other_gap > 0) != direction_5m:
                logger.debug(
                    "Multi-TF alignment failed: 5m direction=%s but %s direction=%s",
                    "UP" if direction_5m else "DOWN",
                    label,
                    "UP" if other_gap > 0 else "DOWN",
                )
                return False

        logger.debug("Multi-TF alignment OK (5m gap=%.2f)", gap_5m)
        return True

    def get_gap_category(self, gap: float) -> str:
        """
        Categorise gap magnitude for use in position-sizing multipliers.

        Args:
            gap: Absolute gap in dollars.

        Returns:
            "small"  → |gap| < $10
            "medium" → $10 ≤ |gap| < $15
            "large"  → $15 ≤ |gap| < $20
            "xlarge" → |gap| ≥ $20
        """
        abs_gap = abs(gap)
        if abs_gap < 10:
            return "small"
        if abs_gap < 15:
            return "medium"
        if abs_gap < 20:
            return "large"
        return "xlarge"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_reference_price(self, market_id: str, current_btc_price: float) -> float:
        """
        Retrieve the market's reference (strike) price.

        In live mode the CLOB client is used to fetch the market metadata.
        In dry-run mode (no client) the market_id is expected to encode the
        strike price as the last numeric segment, e.g. ``BTC-5MIN-95000``.
        Falls back to ``current_btc_price`` if parsing fails.
        """
        if self.clob_client is not None:
            return self._fetch_reference_from_clob(market_id)

        # Dry-run: parse strike from market id
        return self._parse_strike_from_id(market_id, current_btc_price)

    def _fetch_reference_from_clob(self, market_id: str) -> float:
        """Fetch reference price from Polymarket CLOB."""
        try:
            market = self.clob_client.get_market(market_id)
            # The CLOB API returns the strike / reference as 'question_id' data
            # embedded in market outcomes.  We look for a numeric outcome label.
            for outcome in market.get("outcomes", []):
                label = outcome.get("label", "")
                try:
                    return float(label.replace(",", "").replace("$", ""))
                except ValueError:
                    continue
            logger.warning("Could not parse reference price from CLOB for %s", market_id)
            return self._price_cache.get(market_id, 0.0)
        except Exception as exc:
            logger.warning("CLOB fetch failed for %s: %s", market_id, exc)
            return self._price_cache.get(market_id, 0.0)

    @staticmethod
    def _parse_strike_from_id(market_id: str, fallback: float) -> float:
        """Parse a numeric strike price from a market-ID string."""
        parts = market_id.replace("-", "_").split("_")
        for part in reversed(parts):
            try:
                return float(part)
            except ValueError:
                continue
        logger.debug("Could not parse strike from market_id '%s'; using %.2f", market_id, fallback)
        return fallback
