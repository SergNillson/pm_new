"""
Volatility Monitor Module
Monitor BTC price volatility using the Binance public REST API via ccxt.
No API key is required for public market data.
"""

import asyncio
import logging
import statistics
from typing import List

logger = logging.getLogger(__name__)


class VolatilityMonitor:
    """
    Tracks BTC price volatility using Binance OHLCV data.
    Uses the public ccxt Binance interface — no API credentials required.
    """

    def __init__(self):
        """Initialise Binance client via ccxt (public API, no key needed)."""
        try:
            import ccxt  # noqa: PLC0415

            self.exchange = ccxt.binance({"enableRateLimit": True})
        except ImportError as exc:
            raise ImportError(
                "ccxt is required for VolatilityMonitor.  "
                "Install with: pip install ccxt"
            ) from exc

        self._last_volatility: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_current_volatility(self, period_hours: int = 1) -> float:
        """
        Calculate BTC/USDT coefficient-of-variation over the last *period_hours*.

        The formula is:  std_dev(close_prices) / mean(close_prices)

        Args:
            period_hours: Look-back window in hours (default 1).

        Returns:
            Volatility as a decimal fraction (e.g. 0.12 means 12 %).
        """
        prices = await self._fetch_close_prices(period_hours)
        if len(prices) < 2:
            logger.warning("Not enough price data to compute volatility; returning 0")
            return 0.0

        mean_price = statistics.mean(prices)
        if mean_price == 0:
            return 0.0

        vol = statistics.stdev(prices) / mean_price
        self._last_volatility = vol
        logger.debug("BTC volatility (last %dh): %.4f", period_hours, vol)
        return vol

    def is_low_volatility(self, threshold: float = 0.15) -> bool:
        """
        Check whether the most recently computed volatility is below *threshold*.

        Args:
            threshold: Maximum acceptable volatility (default 0.15 = 15 %).

        Returns:
            True if volatility is below threshold.
        """
        return self._last_volatility < threshold

    async def get_volatility_multiplier(self, current_vol: float) -> float:
        """
        Map current volatility to a position-sizing multiplier.

        Lower volatility → higher confidence → larger position multiplier.

        Multiplier table:
            vol < 0.10  →  1.5  (very calm market)
            vol < 0.12  →  1.3
            vol < 0.15  →  1.1
            vol < 0.20  →  1.0
            vol ≥ 0.20  →  0.7  (high vol — reduce size)

        Args:
            current_vol: Volatility fraction (e.g. 0.12).

        Returns:
            Multiplier between 0.7 and 1.5.
        """
        if current_vol < 0.10:
            return 1.5
        if current_vol < 0.12:
            return 1.3
        if current_vol < 0.15:
            return 1.1
        if current_vol < 0.20:
            return 1.0
        return 0.7

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_close_prices(self, period_hours: int) -> List[float]:
        """
        Fetch 1-minute OHLCV candles for BTC/USDT from Binance and
        return a list of close prices covering the last *period_hours*.
        """
        limit = period_hours * 60  # one candle per minute
        symbol = "BTC/USDT"
        try:
            loop = asyncio.get_event_loop()
            ohlcv = await loop.run_in_executor(
                None,
                lambda: self.exchange.fetch_ohlcv(symbol, timeframe="1m", limit=limit),
            )
            return [candle[4] for candle in ohlcv]  # index 4 = close price
        except Exception as exc:
            logger.warning("Failed to fetch OHLCV from Binance: %s", exc)
            return []
