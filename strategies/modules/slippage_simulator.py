"""
Slippage Simulator (real orderbook-based)
==========================================
Calculates realistic execution prices and slippage costs by walking the
real Polymarket orderbook rather than using synthetic random estimates.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class SlippageSimulator:
    """
    Calculates realistic fill prices by consuming orderbook liquidity.

    For a **buy** order (side="buy") we walk the **asks** (ascending price).
    For a **sell** order (side="sell") we walk the **bids** (descending price).

    If the orderbook has insufficient liquidity the position size is reduced
    to what is actually available (partial fill).
    """

    # Fallback base-slippage fractions when no orderbook data is available
    _FALLBACK_SLIPPAGE_BY_SIZE = [
        (2.0, 0.001),   # < $2  → 0.10 %
        (4.0, 0.003),   # < $4  → 0.30 %
        (6.0, 0.006),   # < $6  → 0.60 %
        (float("inf"), 0.010),  # ≥ $6  → 1.00 %
    ]

    def __init__(self) -> None:
        self.total_slippage_cost: float = 0.0
        self.total_filled_size: float = 0.0
        self.total_orders: int = 0
        self.partial_fill_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate_slippage(
        self,
        side: str,
        size_usd: float,
        orderbook: Dict[str, Any],
        token_price: float = 0.5,
    ) -> Tuple[float, float, float]:
        """
        Walk the orderbook to compute the weighted-average fill price.

        Args:
            side:        "buy" or "sell".
            size_usd:    Dollar amount to fill.
            orderbook:   Dict with ``"asks"`` and ``"bids"`` lists; each entry
                         is ``{"price": str, "size": str}``.
            token_price: Fallback reference price (used to convert USD → tokens
                         and also when the orderbook is empty).

        Returns:
            Tuple of (fill_price, slippage_cost, filled_size_usd).

            * fill_price:      Weighted-average execution price (0–1 scale).
            * slippage_cost:   Dollar cost of slippage (fill_price deviation × size).
            * filled_size_usd: Actual filled amount (may be < size_usd if thin book).
        """
        side_lc = side.lower()
        if side_lc in ("buy", "yes"):
            levels = self._parse_levels(orderbook.get("asks", []))
            levels.sort(key=lambda x: x[0])   # ascending price
            ref_price = levels[0][0] if levels else token_price
        else:
            levels = self._parse_levels(orderbook.get("bids", []))
            levels.sort(key=lambda x: x[0], reverse=True)  # descending price
            ref_price = levels[0][0] if levels else token_price

        if not levels:
            # Fall back to synthetic slippage when orderbook is empty
            return self._fallback_slippage(size_usd, token_price, side_lc)

        fill_price, filled_usd = self._walk_book(levels, size_usd, token_price)

        if filled_usd < size_usd:
            self.partial_fill_count += 1
            logger.info(
                "📉 [Slippage] Partial fill: requested $%.2f, filled $%.2f (thin book)",
                size_usd,
                filled_usd,
            )

        slippage_cost = abs(fill_price - ref_price) * filled_usd
        self.total_slippage_cost += slippage_cost
        self.total_filled_size += filled_usd
        self.total_orders += 1

        logger.info(
            "📊 [Slippage] side=%s size=$%.2f → fill=%.4f ref=%.4f "
            "slippage=$%.4f filled=$%.2f",
            side_lc,
            size_usd,
            fill_price,
            ref_price,
            slippage_cost,
            filled_usd,
        )

        return fill_price, slippage_cost, filled_usd

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def get_summary(self) -> Dict[str, Any]:
        """Return cumulative slippage statistics."""
        return {
            "total_slippage_cost": self.total_slippage_cost,
            "total_filled_size": self.total_filled_size,
            "total_orders": self.total_orders,
            "partial_fill_count": self.partial_fill_count,
            "avg_slippage_pct": (
                (self.total_slippage_cost / self.total_filled_size * 100)
                if self.total_filled_size > 0
                else 0.0
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_levels(raw: List[Any]) -> List[Tuple[float, float]]:
        """
        Parse raw orderbook level list into (price, size) tuples.

        Each raw element may be a dict ``{"price": "0.92", "size": "14.5"}``
        or a two-element list/tuple ``["0.92", "14.5"]``.
        """
        levels: List[Tuple[float, float]] = []
        for item in raw:
            try:
                if isinstance(item, dict):
                    p = float(item.get("price") or item.get("p") or 0)
                    s = float(item.get("size") or item.get("s") or 0)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    p, s = float(item[0]), float(item[1])
                else:
                    continue
                if p > 0 and s > 0:
                    levels.append((p, s))
            except (TypeError, ValueError):
                continue
        return levels

    def _walk_book(
        self,
        levels: List[Tuple[float, float]],
        size_usd: float,
        token_price: float,
    ) -> Tuple[float, float]:
        """
        Walk price levels and compute the VWAP fill price.

        Returns (vwap_fill_price, filled_usd).
        """
        remaining_usd = size_usd
        total_tokens = 0.0
        total_usd_spent = 0.0

        for price, token_qty in levels:
            if remaining_usd <= 0:
                break
            level_usd = token_qty * price
            fill_usd = min(remaining_usd, level_usd)
            tokens_filled = fill_usd / price

            total_tokens += tokens_filled
            total_usd_spent += fill_usd
            remaining_usd -= fill_usd

        if total_tokens == 0:
            return token_price, 0.0

        vwap = total_usd_spent / total_tokens
        return vwap, total_usd_spent

    def _fallback_slippage(
        self,
        size_usd: float,
        token_price: float,
        side: str,
    ) -> Tuple[float, float, float]:
        """Return a synthetic slippage estimate when no orderbook data is available."""
        slippage_frac = 0.005  # default 0.5 %
        for threshold, frac in self._FALLBACK_SLIPPAGE_BY_SIZE:
            if size_usd < threshold:
                slippage_frac = frac
                break

        if side in ("buy", "yes"):
            fill_price = min(0.99, token_price * (1 + slippage_frac))
        else:
            fill_price = max(0.01, token_price * (1 - slippage_frac))

        slippage_cost = size_usd * slippage_frac
        self.total_slippage_cost += slippage_cost
        self.total_filled_size += size_usd
        self.total_orders += 1

        logger.debug(
            "📊 [Slippage] Fallback estimate: side=%s size=$%.2f → fill=%.4f slippage=$%.4f",
            side,
            size_usd,
            fill_price,
            slippage_cost,
        )
        return fill_price, slippage_cost, size_usd
