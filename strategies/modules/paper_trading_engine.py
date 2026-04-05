"""
Paper Trading Engine
=====================
Orchestrates real-data paper trading: fetches real Polymarket markets and
orderbook prices, records simulated orders (no real execution), monitors
live price movements, and settles positions using real outcomes.

All market data comes from public Polymarket APIs — no credentials required.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from strategies.modules.polymarket_api_client import PolymarketAPIClient
from strategies.modules.slippage_simulator import SlippageSimulator
from strategies.modules.pnl_tracker import PnLTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRADING_FEE_RATE = 0.002          # 0.2 % taker fee
PRICE_MONITOR_INTERVAL = 5.0     # seconds between price refresh cycles
SETTLEMENT_POLL_INTERVAL = 10.0  # seconds between settlement checks


class PaperTradingEngine:
    """
    Paper Trading Engine — uses real Polymarket data, no real orders executed.

    Responsibilities
    ----------------
    * Fetch real BTC markets from Polymarket (no auth).
    * Fetch real orderbook to simulate fills with realistic slippage.
    * Record virtual positions in the PnLTracker.
    * Continuously poll real prices and update unrealized P&L.
    * Detect market resolution and close positions at the real settlement price.
    """

    def __init__(
        self,
        api_client: Optional[PolymarketAPIClient] = None,
        keyword: str = "BTC",
    ) -> None:
        """
        Args:
            api_client: Shared PolymarketAPIClient (or None → create a new one).
            keyword:    Keyword used to filter markets (e.g. "BTC").
        """
        self.api_client = api_client or PolymarketAPIClient()
        self.keyword = keyword
        self.slippage_sim = SlippageSimulator()
        self.pnl_tracker = PnLTracker()

        # token_id → full market dict (includes "condition_id", "tokens", …)
        self._active_markets: Dict[str, Dict[str, Any]] = {}

        # Background monitoring task handle
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background price-monitoring loop."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("🚀 [Paper Trading] Engine started (keyword=%s)", self.keyword)

    async def stop(self) -> None:
        """Stop the background monitoring loop and close the API session."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await self.api_client.close()
        logger.info("🛑 [Paper Trading] Engine stopped.")
        self.pnl_tracker.log_summary()
        self._log_slippage_summary()

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def refresh_markets(self) -> List[Dict[str, Any]]:
        """
        Fetch current BTC markets from Polymarket and cache them.

        Returns:
            List of active market dicts.
        """
        markets = await self.api_client.fetch_markets(keyword=self.keyword)
        self._active_markets.clear()
        for mkt in markets:
            cid = mkt.get("condition_id") or mkt.get("id") or ""
            if cid:
                self._active_markets[cid] = mkt

        logger.info(
            "🗺️  [Paper Trading] Loaded %d active %s markets",
            len(self._active_markets),
            self.keyword,
        )
        return markets

    def get_active_markets(self) -> List[Dict[str, Any]]:
        """Return the last-fetched list of active markets."""
        return list(self._active_markets.values())

    # ------------------------------------------------------------------
    # Order simulation
    # ------------------------------------------------------------------

    async def place_paper_order(
        self,
        market: Dict[str, Any],
        side: str,
        size_usd: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Simulate a paper trading order with realistic slippage from the real
        Polymarket orderbook.

        Args:
            market:   Market dict (as returned by ``fetch_markets``).
            side:     "YES" or "NO" (binary market side).
            size_usd: Dollar amount to fill.

        Returns:
            Simulated order dict, or *None* if the order could not be placed.
        """
        condition_id = market.get("condition_id") or market.get("id") or ""
        question = market.get("question", condition_id)

        # Resolve the token ID for the requested side
        token_id = self._get_token_id(market, side)
        if not token_id:
            logger.warning(
                "⚠️  [Paper Trading] Cannot find token_id for %s side=%s — skipping",
                condition_id,
                side,
            )
            return None

        logger.info(
            "📋 [Paper Trading] Placing paper order: %s %s $%.2f on '%s'",
            side,
            "BUY",
            size_usd,
            question[:60],
        )

        # Fetch real orderbook and midpoint
        orderbook = await self.api_client.fetch_orderbook(token_id)
        midpoint = await self.api_client.fetch_midpoint(token_id)
        ref_price = midpoint or 0.5

        # Calculate slippage-adjusted fill price using real orderbook depth
        buy_side = "buy" if side.upper() == "YES" else "sell"
        fill_price, slippage_cost, filled_usd = self.slippage_sim.calculate_slippage(
            side=buy_side,
            size_usd=size_usd,
            orderbook=orderbook,
            token_price=ref_price,
        )

        # Entry fees
        entry_fees = filled_usd * TRADING_FEE_RATE

        # Register in P&L tracker
        self.pnl_tracker.open_position(
            token_id=token_id,
            side=side.upper(),
            size_usd=filled_usd,
            fill_price=fill_price,
            fees=entry_fees,
            market_id=condition_id,
            question=question,
        )

        order = {
            "condition_id": condition_id,
            "token_id": token_id,
            "side": side.upper(),
            "requested_size_usd": size_usd,
            "filled_size_usd": filled_usd,
            "ref_price": ref_price,
            "fill_price": fill_price,
            "slippage_cost": slippage_cost,
            "entry_fees": entry_fees,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": question,
        }

        logger.info(
            "✅ [Paper Trading] Order SIMULATED | side=%s filled=$%.2f "
            "ref=%.4f fill=%.4f slippage=$%.4f fees=$%.4f",
            side,
            filled_usd,
            ref_price,
            fill_price,
            slippage_cost,
            entry_fees,
        )
        return order

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def settle_paper_position(
        self,
        token_id: str,
        condition_id: str,
    ) -> Optional[float]:
        """
        Attempt to settle an open paper position using the real settlement outcome.

        If the market is not yet resolved, updates unrealized P&L with the
        current midpoint instead.

        Args:
            token_id:     Token position identifier.
            condition_id: Parent market condition ID.

        Returns:
            Net realised P&L in USD if settled, or *None* if still open.
        """
        settlement_info = await self.api_client.fetch_settlement(condition_id)

        if settlement_info is None or not settlement_info.get("resolved"):
            # Market still open: mark to market
            midpoint = await self.api_client.fetch_midpoint(token_id)
            if midpoint is not None:
                unrealized = self.pnl_tracker.update_unrealized(token_id, midpoint)
                logger.info(
                    "⏳ [Paper Trading] Market not yet resolved | token=%s "
                    "midpoint=%.4f unrealized_pnl=%+.4f",
                    token_id,
                    midpoint,
                    unrealized or 0.0,
                )
            return None

        settlement_price = settlement_info.get("settlement_price", 0.5)
        exit_fees = self.pnl_tracker.get_position_size(token_id) * TRADING_FEE_RATE
        net_pnl = self.pnl_tracker.close_position(
            token_id=token_id,
            settlement_price=settlement_price,
            fees=exit_fees,
            reason="settlement",
        )

        logger.info(
            "🏁 [Paper Trading] Settlement complete | token=%s settle=%.4f net_pnl=%+.4f",
            token_id,
            settlement_price,
            net_pnl or 0.0,
        )
        return net_pnl

    # ------------------------------------------------------------------
    # Background monitor
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """
        Background loop that periodically refreshes prices and checks for
        market resolution for all currently tracked open positions.
        """
        logger.info("👁️  [Paper Trading] Price monitor loop started.")
        while self._running:
            await self._refresh_open_positions()
            await asyncio.sleep(PRICE_MONITOR_INTERVAL)
        logger.info("👁️  [Paper Trading] Price monitor loop stopped.")

    async def _refresh_open_positions(self) -> None:
        """Update unrealized P&L and check for settlement on all open positions."""
        positions = self.pnl_tracker.get_open_positions()
        if not positions:
            return

        for pos in positions:
            token_id = pos["token_id"]
            condition_id = pos.get("market_id", token_id)

            # Try to settle first
            settled = await self.settle_paper_position(token_id, condition_id)
            if settled is not None:
                continue  # position was closed

            # Not settled yet — refresh unrealized P&L
            midpoint = await self.api_client.fetch_midpoint(token_id)
            if midpoint is not None:
                unrealized = self.pnl_tracker.update_unrealized(token_id, midpoint)
                logger.info(
                    "📊 [Paper Trading] Mark-to-market | token=%s price=%.4f uPnL=%+.4f",
                    token_id,
                    midpoint,
                    unrealized or 0.0,
                )

        # Log a brief P&L snapshot after each refresh cycle
        summary = self.pnl_tracker.get_summary()
        logger.info(
            "💹 [Paper Trading] P&L snapshot | open=%d realized=%+.4f unrealized=%+.4f total=%+.4f",
            summary["open_positions"],
            summary["total_realized_pnl"],
            summary["total_unrealized_pnl"],
            summary["total_pnl"],
        )

    async def get_token_prices(self, market: Dict[str, Any]) -> Dict[str, float]:
        """
        Fetch current Up and Down token prices from the Polymarket CLOB orderbook.

        Args:
            market: Market dict with a ``"tokens"`` list.

        Returns:
            ``{"up": float, "down": float}`` — prices in [0, 1].
            Falls back to ``{"up": 0.5, "down": 0.5}`` if prices cannot be fetched.
        """
        default = {"up": 0.5, "down": 0.5}
        tokens: List[Dict] = market.get("tokens", [])
        outcomes = market.get("outcomes", [])

        if not tokens:
            logger.debug("get_token_prices: no tokens in market")
            return default

        # Build a mapping from outcome label → token_id
        outcome_to_token: Dict[str, str] = {}
        for i, tok in enumerate(tokens):
            # tokens may be plain strings (token IDs) or dicts with "token_id"/"outcome"
            if isinstance(tok, dict):
                token_id = tok.get("token_id", "")
                outcome = tok.get("outcome", "")
            else:
                token_id = str(tok)
                outcome = outcomes[i] if i < len(outcomes) else ""
            outcome_to_token[outcome.upper()] = token_id

        # Resolve Up and Down token IDs
        up_token_id: Optional[str] = None
        down_token_id: Optional[str] = None
        for label, token_id in outcome_to_token.items():
            if label in ("UP", "YES", "HIGHER"):
                up_token_id = token_id
            elif label in ("DOWN", "NO", "LOWER"):
                down_token_id = token_id

        # Fallback: if labels aren't matched, assume first=Up, second=Down
        if up_token_id is None and len(tokens) >= 1:
            tok = tokens[0]
            up_token_id = tok.get("token_id") if isinstance(tok, dict) else str(tok)
        if down_token_id is None and len(tokens) >= 2:
            tok = tokens[1]
            down_token_id = tok.get("token_id") if isinstance(tok, dict) else str(tok)

        # Fetch midpoint prices concurrently
        up_price: float = 0.5
        down_price: float = 0.5
        try:
            coros = []
            fetch_up = up_token_id is not None
            fetch_down = down_token_id is not None
            if fetch_up:
                coros.append(self.api_client.fetch_midpoint(up_token_id))
            if fetch_down:
                coros.append(self.api_client.fetch_midpoint(down_token_id))

            results = await asyncio.gather(*coros) if coros else []

            idx = 0
            if fetch_up:
                mid = results[idx]
                if mid is not None:
                    up_price = float(mid)
                idx += 1
            if fetch_down:
                mid = results[idx]
                if mid is not None:
                    down_price = float(mid)
        except Exception as exc:
            logger.warning("⚠️  get_token_prices failed: %s — using defaults", exc)
            return default

        logger.info(
            "💲 Token prices: Up=%.3f Down=%.3f",
            up_price,
            down_price,
        )
        return {"up": up_price, "down": down_price}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_token_id(market: Dict[str, Any], side: str) -> Optional[str]:
        """
        Resolve the token ID for the requested binary-market side.

        Polymarket markets contain a ``"tokens"`` list with entries like::

            [{"token_id": "0xabc...", "outcome": "Yes"},
             {"token_id": "0xdef...", "outcome": "No"}]

        Falls back to ``condition_id`` if no tokens are listed.
        """
        tokens: List[Dict] = market.get("tokens", [])
        side_upper = side.upper()
        for tok in tokens:
            outcome = tok.get("outcome", "").upper()
            if outcome == side_upper or (side_upper == "YES" and outcome in ("YES", "Y")) or (
                side_upper == "NO" and outcome in ("NO", "N")
            ):
                return tok.get("token_id")

        # Fallback: return condition_id as a generic token identifier
        return market.get("condition_id") or market.get("id")

    def _log_slippage_summary(self) -> None:
        """Emit cumulative slippage statistics at INFO level."""
        s = self.slippage_sim.get_summary()
        logger.info("=" * 50)
        logger.info("  Slippage Summary")
        logger.info("  Total orders:         %d", s["total_orders"])
        logger.info("  Partial fills:        %d", s["partial_fill_count"])
        logger.info("  Total slippage cost:  $%.4f", s["total_slippage_cost"])
        logger.info("  Avg slippage:         %.2f%%", s["avg_slippage_pct"])
        logger.info("=" * 50)
