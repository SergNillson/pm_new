"""
Gap Analyzer Module
Calculate and analyze BTC price gaps between current market price
and Polymarket settlement reference price.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
        # Cache of reference prices keyed by market condition_id / id
        self._reference_prices: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_gap(
        self,
        market_id: str,
        current_btc_price: float,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Calculate gap for BTC Up/Down markets.

        UPDATED: Now works with Up/Down markets without explicit strike.

        Args:
            market_id: Polymarket market identifier.
            current_btc_price: Latest BTC spot price (e.g. from Binance).
            market_data: Optional market dict. For Up/Down markets (no ``strike``),
                the reference price is derived from the window start time.

        Returns:
            Gap in dollars (positive → ABOVE reference, negative → BELOW reference).
            Returns 0.0 if the reference price cannot be determined.
        """
        # For Up/Down markets: try window-based reference price first
        if market_data:
            reference_price = self.get_reference_price_for_window(market_data)
            if reference_price is not None:
                gap = current_btc_price - reference_price
                logger.debug(
                    "Gap: BTC $%s - Ref $%s = $%+.2f",
                    f"{current_btc_price:,.2f}",
                    f"{reference_price:,.2f}",
                    gap,
                )
                if abs(gap) > 5000:
                    logger.warning(
                        "⚠️ Suspiciously large gap detected: $%.2f | "
                        "BTC=%.2f, ref=%.2f | "
                        "This might indicate incorrect reference price!",
                        gap,
                        current_btc_price,
                        reference_price,
                    )
                return gap

            # Fallback: old logic for markets with explicit strike
            if market_data.get("strike"):
                try:
                    strike = float(market_data["strike"])
                    gap = current_btc_price - strike
                    logger.debug(
                        "Market %s | BTC=%.2f | strike=%.2f | gap=%.2f",
                        market_id,
                        current_btc_price,
                        strike,
                        gap,
                    )
                    return gap
                except (TypeError, ValueError):
                    pass

        # Cannot calculate gap
        logger.debug("Cannot calculate gap - no reference or strike price")
        return 0.0

    def get_reference_price_for_window(
        self,
        market_data: Dict[str, Any],
    ) -> Optional[float]:
        """
        Get reference price for an Up/Down market window.

        Reference price = BTC price at the start of the 5-minute window.

        The method uses the following priority:
        1. Cached value (``self._reference_prices``) if already stored.
        2. Estimated from the first time we observe the market within the window
           (the current BTC price is stored as the reference when the window begins).
        3. Returns ``None`` if ``endDate`` cannot be parsed or the window has
           not started yet.

        Args:
            market_data: Market dict from Polymarket API.

        Returns:
            Reference price in USD, or ``None`` if not yet determinable.
        """
        market_id = market_data.get("condition_id") or market_data.get("id") or ""
        if not market_id:
            return None

        # 1. Return cached reference price
        if market_id in self._reference_prices:
            return self._reference_prices[market_id]

        # 2. Determine window_start from endDate (endDate - 5 minutes)
        end_date_str = (
            market_data.get("end_date_iso")
            or market_data.get("endDateIso")
            or market_data.get("end_date")
            or market_data.get("endDate")
        )
        if not end_date_str:
            return None

        try:
            end_dt = datetime.fromisoformat(
                str(end_date_str).replace("Z", "+00:00")
            )
            end_ts = end_dt.timestamp()
        except Exception:
            return None

        window_start_ts = end_ts - 5 * 60  # 5-minute window
        now_ts = time.time()

        # Window has not started yet
        if now_ts < window_start_ts:
            logger.debug(
                "Window for market %s has not started yet (starts in %.0fs)",
                market_id,
                window_start_ts - now_ts,
            )
            return None

        # Window started; if we have a settlement_time already in market_data but
        # the market provided a reference_price field, use it directly.
        if market_data.get("reference_price") is not None:
            ref = float(market_data["reference_price"])
            self._reference_prices[market_id] = ref
            logger.info("📍 Reference price for window (from field): $%s", f"{ref:,.2f}")
            return ref

        # No cached value and no explicit field — caller should seed the cache via
        # seed_reference_price() the first time they observe the window start.
        return None

    def seed_reference_price(self, market_data: Dict[str, Any], btc_price: float) -> None:
        """
        Store the current BTC price as the reference price for the given market window.

        This should be called once when a market's window is first observed so that
        subsequent calls to ``get_reference_price_for_window`` return a consistent value.

        Args:
            market_data: Market dict from Polymarket API.
            btc_price:   Current BTC price to record as the window reference.
        """
        market_id = market_data.get("condition_id") or market_data.get("id") or ""
        if not market_id:
            return
        if market_id not in self._reference_prices:
            self._reference_prices[market_id] = btc_price
            logger.info(
                "📍 Reference price seeded for window %s: $%s",
                market_id,
                f"{btc_price:,.2f}",
            )

    def check_momentum_gap_signal(
        self,
        market_data: Dict[str, Any],
        current_btc_price: float,
        up_token_price: float,
        down_token_price: float,
        min_token_price_threshold: float = 0.75,
        min_gap: float = 25.0,
        max_time_left: float = 90.0,
        min_time_left: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Check conditions for a Momentum + Gap entry signal.

        An entry signal is generated when ALL of the following hold:
        1. Up *or* Down token price ≥ ``min_token_price_threshold`` (momentum).
        2. |gap| ≥ ``min_gap`` (significant price displacement).
        3. ``min_time_left`` ≤ time_left ≤ ``max_time_left`` (entry window).
        4. Gap direction and momentum direction are aligned.

        Args:
            market_data: Market dict from Polymarket API.
            current_btc_price: Current BTC spot price from Binance.
            up_token_price: Price of the Up token (0–1).
            down_token_price: Price of the Down token (0–1).
            min_token_price_threshold: Minimum token price to consider (default 0.75).
            min_gap: Minimum price gap in USD (default $25).
            max_time_left: Maximum seconds until settlement to enter (default 90).
            min_time_left: Minimum seconds until settlement to enter (default 15).

        Returns:
            Signal dict if all conditions met::

                {
                    "side": "UP" or "DOWN",
                    "confidence": float,          # token price (0-1)
                    "gap": float,                 # dollar gap (signed)
                    "reference_price": float,
                    "current_price": float,       # current BTC price
                    "entry_price": float,         # token price to buy
                    "time_left": float,           # seconds until settlement
                    "expected_profit_pct": float, # % profit if win
                }

            ``None`` if no signal.
        """
        # 1. Get reference price
        reference_price = self.get_reference_price_for_window(market_data)
        if reference_price is None:
            logger.debug("No reference price available — cannot check momentum+gap signal")
            return None

        # 2. Calculate gap
        gap = current_btc_price - reference_price
        logger.info(
            "📊 Gap analysis: $%+.2f | Up: %.3f Down: %.3f",
            gap,
            up_token_price,
            down_token_price,
        )

        # 3. Calculate time left
        end_date_str = (
            market_data.get("end_date_iso")
            or market_data.get("endDateIso")
            or market_data.get("end_date")
            or market_data.get("endDate")
        )
        settlement_time = market_data.get("settlement_time")
        if settlement_time is not None:
            time_left = float(settlement_time) - time.time()
        elif end_date_str:
            try:
                end_dt = datetime.fromisoformat(
                    str(end_date_str).replace("Z", "+00:00")
                )
                time_left = end_dt.timestamp() - time.time()
            except Exception:
                logger.debug("Cannot parse end_date for time_left calculation")
                return None
        else:
            logger.debug("No settlement time available — cannot check signal")
            return None

        logger.info(
            "⏰ Time check: %.0fs (range: %.0f-%.0fs)",
            time_left,
            min_time_left,
            max_time_left,
        )

        # 4. Time window check
        if not (min_time_left <= time_left <= max_time_left):
            logger.debug(
                "Time left %.0fs outside entry window [%.0f, %.0f]s",
                time_left,
                min_time_left,
                max_time_left,
            )
            return None

        # 5. Check UP signal (gap positive + up token confident)
        if gap >= min_gap and up_token_price >= min_token_price_threshold:
            expected_profit_pct = (1.0 - up_token_price) * 100.0
            signal: Dict[str, Any] = {
                "side": "UP",
                "confidence": up_token_price,
                "gap": gap,
                "reference_price": reference_price,
                "current_price": current_btc_price,
                "entry_price": up_token_price,
                "time_left": time_left,
                "expected_profit_pct": expected_profit_pct,
            }
            logger.info(
                "🎯 SIGNAL FOUND: UP @ %.3f (gap $%+.2f)",
                up_token_price,
                gap,
            )
            return signal

        # 6. Check DOWN signal (gap negative + down token confident)
        if gap <= -min_gap and down_token_price >= min_token_price_threshold:
            expected_profit_pct = (1.0 - down_token_price) * 100.0
            signal = {
                "side": "DOWN",
                "confidence": down_token_price,
                "gap": gap,
                "reference_price": reference_price,
                "current_price": current_btc_price,
                "entry_price": down_token_price,
                "time_left": time_left,
                "expected_profit_pct": expected_profit_pct,
            }
            logger.info(
                "🎯 SIGNAL FOUND: DOWN @ %.3f (gap $%+.2f)",
                down_token_price,
                gap,
            )
            return signal

        logger.debug(
            "No momentum+gap signal: gap=$%+.2f (min $%.2f), up=%.3f, down=%.3f (min %.2f)",
            gap,
            min_gap,
            up_token_price,
            down_token_price,
            min_token_price_threshold,
        )
        return None

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

    def _get_reference_price(
        self,
        market_id: str,
        current_btc_price: float,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Retrieve the market's reference (strike) price.

        Priority:
        1. Live mode: fetch from CLOB client.
        2. Dry-run with ``market_data``: use ``market_data["strike"]``.
        3. Fallback: current BTC price (produces a zero gap; logged as warning).
        """
        if self.clob_client is not None:
            return self._fetch_reference_from_clob(market_id)

        # Dry-run: use strike from market_data if provided
        if market_data and "strike" in market_data:
            return float(market_data["strike"])

        # Fallback: using current BTC price produces a neutral (zero) gap
        logger.warning(
            "No reference price available for %s, using current BTC price as fallback",
            market_id,
        )
        return current_btc_price

    def _fetch_reference_from_clob(self, market_id: str) -> float:
        """Fetch reference price from Polymarket CLOB."""
        try:
            market = self.clob_client.get_market(market_id)

            # Method 1: Parse from question text (most reliable)
            # Matches patterns like "$67,114.77", "$67114", "67114.77"
            question = market.get("question", "")
            match = re.search(r'\$?([\d,]+(?:\.\d+)?)', question)
            if match:
                price_str = match.group(1).replace(",", "")
                reference = float(price_str)
                logger.debug(
                    "Parsed reference $%.2f from question: %s", reference, question
                )
                return reference

            # Method 2: Try outcome labels (original logic)
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
        """
        Parse a numeric strike price from a market-ID string.

        .. deprecated::
            This method is unreliable because the numeric suffix in a market ID
            (e.g. ``BTC-5MIN-65600``) is *not* guaranteed to be the reference
            price.  Pass ``market_data`` to :meth:`get_current_gap` instead.
            This method is kept only for backwards-compatibility and will be
            removed in a future release.
        """
        logger.warning(
            "_parse_strike_from_id() is deprecated and may return an incorrect "
            "reference price for market '%s'. Pass market_data with a 'strike' "
            "field to get_current_gap() instead.",
            market_id,
        )
        parts = market_id.replace("-", "_").split("_")
        for part in reversed(parts):
            try:
                return float(part)
            except ValueError:
                continue
        logger.debug("Could not parse strike from market_id '%s'; using %.2f", market_id, fallback)
        return fallback
