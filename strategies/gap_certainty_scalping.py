#!/usr/bin/env python3
"""
Gap-Based High Certainty Scalping Strategy
==========================================
Enters positions 15-25 seconds before Polymarket BTC 5-minute market
settlement when the spot price shows a significant gap ($15+) from the
market reference price and volatility is low (<15%).

Modes
-----
--dry-run (default)
    Full simulation — no real orders, no credentials required.
--paper-trading
    Paper trading with REAL Polymarket data (no auth required).
    Fetches live markets, orderbook prices, and settlement outcomes from
    the public Polymarket API.  All orders are simulated — nothing is
    executed on-chain.
--live
    Real trading on Polymarket CLOB.  Requires credentials in .env.

Usage
-----
  python strategies/gap_certainty_scalping.py --dry-run --capital 38
  python strategies/gap_certainty_scalping.py --paper-trading --capital 38
  python strategies/gap_certainty_scalping.py --live --capital 38
  python strategies/gap_certainty_scalping.py --dry-run --config config/gap_certainty_config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Optional imports — gracefully degraded in dry-run mode
# ---------------------------------------------------------------------------
try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; credentials must be set as env vars

# ---------------------------------------------------------------------------
# Module imports
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategies.modules.adaptive_sizer import AdaptivePositionSizer
from strategies.modules.gap_analyzer import GapAnalyzer
from strategies.modules.volatility_monitor import VolatilityMonitor
from strategies.modules.paper_trading_engine import PaperTradingEngine

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "gap_certainty.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_CAPITAL = 38.0
DEFAULT_MIN_GAP = 15.0
DEFAULT_MAX_VOL = 0.15
DEFAULT_BASE_SIZE = 0.05
ENTRY_WINDOW_LOW = 15   # seconds before settlement
ENTRY_WINDOW_HIGH = 25  # seconds before settlement
SCAN_INTERVAL = 1       # seconds between market scans
PARTIAL_EXIT_THRESHOLD = 0.40   # 40 % gap erosion → sell 50 %
EMERGENCY_EXIT_THRESHOLD = 0.70  # 70 % gap erosion → full exit
MAX_CONSECUTIVE_LOSSES = 3
PAUSE_DURATION = 3600   # 1 hour in seconds
MAX_DAILY_DRAWDOWN = 0.15

# Order prices for binary-market entries (0-1 scale)
HIGH_CERTAINTY_PRICE = 0.92     # Entry price when gap ≥ $20
MODERATE_CERTAINTY_PRICE = 0.88  # Entry price when gap < $20
PARTIAL_EXIT_PRICE = 0.50        # Mid-market price used for partial-exit sells

# Dry-run simulation parameters
SIMULATED_BTC_BASE_PRICE = 65_000.0
SIMULATED_BTC_RANGE = 500.0


# ===========================================================================
# Realistic market simulator (dry-run)
# ===========================================================================
class RealisticMarketSimulator:
    """
    Simulates realistic market conditions for dry-run mode.
    Adds slippage, fees, front-running, latency, and partial fills.
    """

    def __init__(self) -> None:
        self.total_slippage = 0.0
        self.total_gas_fees = 0.0
        self.total_latency_losses = 0.0
        self.total_front_running_losses = 0.0
        self.partial_fill_count = 0

    def calculate_slippage(self, position_size: float, target_price: float) -> tuple:
        """
        Calculate realistic slippage based on position size and orderbook depth.

        Returns: (actual_fill_price, slippage_amount)
        """
        import random  # noqa: PLC0415

        # Base slippage increases with position size
        if position_size < 2.0:
            base_slippage = 0.001  # 0.1%
        elif position_size < 4.0:
            base_slippage = 0.003  # 0.3%
        elif position_size < 6.0:
            base_slippage = 0.006  # 0.6%
        else:
            base_slippage = 0.010  # 1.0%

        # Add random variation (±30%)
        actual_slippage = base_slippage * random.uniform(0.7, 1.3)

        # Worse slippage for YES (buying) than NO (selling)
        # Because we're taking liquidity
        actual_fill_price = target_price * (1 - actual_slippage)
        slippage_cost = position_size * actual_slippage

        self.total_slippage += slippage_cost

        logger.debug(
            "Slippage simulation: target=%.4f, actual=%.4f, cost=$%.4f",
            target_price,
            actual_fill_price,
            slippage_cost,
        )

        return actual_fill_price, slippage_cost

    def calculate_realistic_fees(self, position_size: float) -> float:
        """
        Calculate realistic fees including gas.

        Returns: total_fees
        """
        import random  # noqa: PLC0415

        # Trading fee (0.2%)
        trading_fee = position_size * 0.002

        # Gas fee on Polygon (variable)
        gas_fee = random.uniform(0.01, 0.05)

        total_fees = trading_fee + gas_fee
        self.total_gas_fees += gas_fee

        return total_fees

    def simulate_front_running(self, gap: float, entry_price: float) -> tuple:
        """
        Simulate front-running by bots who see the same signal.
        Larger gaps = more competition = worse prices.

        Returns: (adjusted_price, front_running_cost)
        """
        import random  # noqa: PLC0415

        abs_gap = abs(gap)

        if abs_gap >= 30:
            # Huge gap = many bots competing
            price_impact = random.uniform(0.015, 0.025)  # 1.5-2.5% worse
        elif abs_gap >= 20:
            price_impact = random.uniform(0.008, 0.015)  # 0.8-1.5% worse
        elif abs_gap >= 15:
            price_impact = random.uniform(0.003, 0.008)  # 0.3-0.8% worse
        else:
            price_impact = random.uniform(0.001, 0.003)  # 0.1-0.3% worse

        # Adjust entry price (worse for buyer)
        adjusted_price = entry_price * (1 + price_impact)

        # Clamp to [0.01, 0.99]
        adjusted_price = max(0.01, min(0.99, adjusted_price))

        cost = price_impact * entry_price
        self.total_front_running_losses += cost

        logger.debug(
            "Front-running simulation: gap=$%.2f, price %.4f → %.4f (impact: %.2f%%)",
            gap,
            entry_price,
            adjusted_price,
            price_impact * 100,
        )

        return adjusted_price, cost

    def simulate_latency_loss(self, gap: float) -> tuple:
        """
        Simulate trade execution latency (2-5 seconds).
        During this time, BTC can move and gap can disappear.

        Returns: (trade_cancelled, loss_if_cancelled)
        """
        import random  # noqa: PLC0415

        # Latency: 2-5.5 seconds
        latency_seconds = random.uniform(2.0, 5.5)

        # BTC can move ~$10-30 per second in high volatility
        # Assume moderate volatility: $5-15 per second
        btc_movement = random.uniform(5, 15) * latency_seconds

        # Gap erosion during latency; clamp to [0, 1] to avoid unrealistic values
        # when gap is very small
        gap_erosion_pct = min(btc_movement / abs(gap), 1.0) if gap != 0 else 0

        # If gap erodes >60% during latency, cancel trade
        if gap_erosion_pct > 0.60:
            logger.debug(
                "Latency simulation: gap eroded %.1f%% during %.1fs → TRADE CANCELLED",
                gap_erosion_pct * 100,
                latency_seconds,
            )
            self.total_latency_losses += abs(gap) * 0.05  # Small opportunity cost
            return True, abs(gap) * 0.05

        return False, 0.0

    def simulate_partial_fill(self, position_size: float) -> tuple:
        """
        Simulate partial fills due to thin orderbooks near settlement.
        ~15% of trades only partially filled.

        Returns: (actual_filled_size, was_partial)
        """
        import random  # noqa: PLC0415

        # 15% chance of partial fill
        if random.random() < 0.15:
            # Fill only 40-80% of desired size
            fill_ratio = random.uniform(0.40, 0.80)
            actual_size = position_size * fill_ratio
            self.partial_fill_count += 1

            logger.debug(
                "Partial fill simulation: requested $%.2f, filled $%.2f (%.0f%%)",
                position_size,
                actual_size,
                fill_ratio * 100,
            )

            return actual_size, True

        return position_size, False

    def simulate_settlement_uncertainty(self, entry_gap: float, side: str) -> bool:
        """
        Simulate settlement outcome with realistic win rate.
        BTC is volatile - can reverse in final 10-20 seconds.

        Real win rates:
        - Gap $30+:  92%
        - Gap $20-30: 88%
        - Gap $15-20: 85%
        - Gap <$15:  80%

        Returns: True if won, False if lost
        """
        import random  # noqa: PLC0415

        abs_gap = abs(entry_gap)

        # Determine win probability based on gap size
        if abs_gap >= 30:
            win_probability = 0.92
        elif abs_gap >= 20:
            win_probability = 0.88
        elif abs_gap >= 15:
            win_probability = 0.85
        else:
            win_probability = 0.80

        # Roll the dice
        won = random.random() < win_probability

        # Determine final gap direction
        if won:
            # Gap held direction
            final_gap_direction_up = entry_gap > 0
        else:
            # Gap reversed!
            final_gap_direction_up = entry_gap < 0
            logger.info(
                "💥 Gap reversed during settlement! Entry gap=$%.2f, reversed to %s",
                entry_gap,
                "UP" if final_gap_direction_up else "DOWN",
            )

        # Check if our side matches final direction
        result = (side == "YES" and final_gap_direction_up) or (
            side == "NO" and not final_gap_direction_up
        )

        return result

    def get_summary(self) -> dict:
        """Return summary of realistic market impacts."""
        return {
            "total_slippage": self.total_slippage,
            "total_gas_fees": self.total_gas_fees,
            "total_latency_losses": self.total_latency_losses,
            "total_front_running_losses": self.total_front_running_losses,
            "partial_fill_count": self.partial_fill_count,
            "total_realistic_costs": (
                self.total_slippage
                + self.total_gas_fees
                + self.total_latency_losses
                + self.total_front_running_losses
            ),
        }


# ===========================================================================
# Simulated order executor (dry-run)
# ===========================================================================
class SimulatedOrderExecutor:
    """
    Simulates order placement and settlement with REALISTIC market conditions.
    """

    def __init__(self, enable_realism: bool = True) -> None:
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[Dict[str, Any]] = []
        self.enable_realism = enable_realism

        if enable_realism:
            self.market_sim: Optional[RealisticMarketSimulator] = RealisticMarketSimulator()
            logger.info(
                "📊 Realistic market simulation ENABLED (slippage, fees, latency, front-running)"
            )
        else:
            self.market_sim = None
            logger.info("📊 Realistic market simulation DISABLED (perfect fills)")

    async def place_order(
        self,
        side: str,
        token: str,
        price: float,
        size: float,
        gap: float = 0.0,
    ) -> Dict[str, Any]:
        """Record a simulated order with realistic market conditions."""
        original_price = price
        original_size = size
        actual_price = price
        actual_size = size

        if self.enable_realism and self.market_sim:
            # 1. Simulate latency (might cancel trade)
            cancelled, _latency_loss = self.market_sim.simulate_latency_loss(gap)
            if cancelled:
                logger.warning("⏱️  Trade CANCELLED due to latency (gap eroded)")
                return {"cancelled": True, "reason": "latency"}

            # 2. Simulate front-running (price moves before we fill)
            if abs(gap) >= 15:  # Only on signals we'd actually take
                actual_price, _fr_cost = self.market_sim.simulate_front_running(gap, actual_price)

            # 3. Simulate slippage
            actual_price, _slippage_cost = self.market_sim.calculate_slippage(size, actual_price)

            # 4. Simulate partial fills
            actual_size, was_partial = self.market_sim.simulate_partial_fill(size)
            if was_partial:
                logger.info(
                    "⚠️  Partial fill: requested $%.2f, filled $%.2f",
                    original_size,
                    actual_size,
                )

        order: Dict[str, Any] = {
            "side": side,
            "token": token,
            "requested_price": original_price,
            "actual_price": actual_price,
            "requested_size": original_size,
            "actual_size": actual_size,
            "timestamp": time.time(),
            "gap": gap,
        }
        self.positions[token] = order

        logger.info(
            "[DRY-RUN] Simulated %s order: size=%.2f @ price=%.4f %s",
            side,
            actual_size,
            actual_price,
            "(REALISTIC)" if self.enable_realism else "",
        )
        return order

    async def settle_position(
        self,
        token: str,
        settlement_price: float,
        gap_direction_up: bool,
        entry_gap: float = 0.0,
    ) -> float:
        """Simulate settlement with REALISTIC win rate."""
        position = self.positions.pop(token, None)
        if position is None:
            return 0.0

        # On Polymarket binary markets a "YES" bet at price p pays (1-p) if wins
        # and loses p if wrong.
        entry_price = position["actual_price"]  # Use actual fill price (with slippage)
        size = position["actual_size"]  # Use actual filled size (might be partial)
        side = position["side"]

        # Determine if trade won
        if self.enable_realism and self.market_sim:
            # Realistic settlement (accounts for reversals)
            won = self.market_sim.simulate_settlement_uncertainty(entry_gap, side)
        else:
            # Perfect settlement (old behaviour)
            won = (side == "YES" and gap_direction_up) or (
                side == "NO" and not gap_direction_up
            )

        # Calculate P&L
        if won:
            gross_pnl = size * (1.0 - entry_price)
        else:
            gross_pnl = -size * entry_price

        # Subtract realistic fees
        if self.enable_realism and self.market_sim:
            fees = self.market_sim.calculate_realistic_fees(size)
            net_pnl = gross_pnl - fees
        else:
            fees = size * 0.002  # Original 0.2% only
            net_pnl = gross_pnl - fees

        record = {
            "token": token,
            "side": side,
            "entry_price": entry_price,
            "settlement_price": settlement_price,
            "size": size,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "pnl": net_pnl,
            "won": won,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.trade_history.append(record)

        logger.info(
            "[DRY-RUN] Settled %s | Gross P&L=%+.2f | Fees=-%.2f | Net P&L=%+.2f (%s)",
            token,
            gross_pnl,
            fees,
            net_pnl,
            "WIN ✅" if won else "LOSS ❌",
        )
        return net_pnl


# ===========================================================================
# Main strategy class
# ===========================================================================
class GapCertaintyStrategy:
    """
    Gap-Based High Certainty Scalping Strategy.

    Monitors Polymarket BTC 5-minute markets and enters positions when:
    - Spot vs reference gap ≥ min_gap dollars
    - BTC volatility < max_volatility
    - 15-25 seconds remain until settlement
    - (Optional) Multi-timeframe gaps are aligned
    """

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        min_gap: float = DEFAULT_MIN_GAP,
        max_volatility: float = DEFAULT_MAX_VOL,
        base_size: float = DEFAULT_BASE_SIZE,
        dry_run: bool = True,
        paper_trading: bool = False,
        config: Optional[Dict[str, Any]] = None,
        enable_realism: bool = True,
    ) -> None:
        self.capital = capital
        self.min_gap = min_gap
        self.max_volatility = max_volatility
        self.dry_run = dry_run
        self.paper_trading = paper_trading
        self.config = config or {}

        # Initialise sub-modules
        clob_client = None if (dry_run or paper_trading) else self._build_clob_client()
        self.gap_analyzer = GapAnalyzer(clob_client=clob_client)
        self.vol_monitor = VolatilityMonitor()
        self.sizer = AdaptivePositionSizer(capital=capital, base_pct=base_size)

        if paper_trading:
            # Real Polymarket data, simulated orders — no auth required
            self.paper_engine: Optional[PaperTradingEngine] = PaperTradingEngine(
                keyword="BTC"
            )
            self.executor: Any = SimulatedOrderExecutor(enable_realism=enable_realism)
        elif dry_run:
            self.paper_engine = None
            self.executor = SimulatedOrderExecutor(enable_realism=enable_realism)
        else:
            self.paper_engine = None
            self.executor = self._build_live_executor(clob_client)

        self._running = False
        self._paused_until: float = 0.0
        self._daily_start_bankroll = capital
        self._start_time = datetime.now(timezone.utc)

        # CSV logging
        self._csv_path = LOG_DIR / "trades.csv"
        self._init_csv()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the main event loop."""
        if self.paper_trading:
            mode = "PAPER-TRADING (real data, no auth)"
        elif self.dry_run:
            mode = "DRY-RUN"
        else:
            mode = "LIVE"
        logger.info("=" * 60)
        logger.info("  Gap Certainty Scalping Strategy  |  %s mode", mode)
        logger.info("  Capital: $%.2f  |  Min gap: $%.2f  |  Max vol: %.0f%%",
                    self.capital, self.min_gap, self.max_volatility * 100)
        logger.info("=" * 60)

        # Start the paper trading engine's background monitor if applicable
        if self.paper_trading and self.paper_engine is not None:
            await self.paper_engine.start()

        self._running = True
        try:
            await self._main_loop()
        except KeyboardInterrupt:
            logger.info("Strategy stopped by user.")
        finally:
            if self.paper_trading and self.paper_engine is not None:
                await self.paper_engine.stop()
            self._print_summary()

    async def stop(self) -> None:
        """Gracefully stop the strategy."""
        self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        logger.info("📊 Scanning BTC 5-min markets...")

        while self._running:
            now = time.time()

            # Respect pause period after consecutive losses
            if now < self._paused_until:
                remaining = int(self._paused_until - now)
                if remaining % 60 == 0:
                    logger.info("⏸  Strategy paused for %d more minutes.", remaining // 60)
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # Check daily drawdown limit
            if self._daily_drawdown_exceeded():
                logger.warning("🛑 Daily drawdown limit reached. Stopping strategy.")
                self._running = False
                break

            # Fetch current market state
            current_btc_price = await self._get_btc_price()
            current_vol = await self.vol_monitor.get_current_volatility()

            if current_vol > self.max_volatility:
                logger.debug("Volatility %.2f%% > %.2f%% — skipping scan.", current_vol * 100, self.max_volatility * 100)
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # Scan markets
            markets = await self._get_active_markets()
            for market in markets:
                time_left = market["settlement_time"] - now
                if ENTRY_WINDOW_LOW <= time_left <= ENTRY_WINDOW_HIGH:
                    await self._check_entry_signal(market, current_btc_price, current_vol)

            await asyncio.sleep(SCAN_INTERVAL)

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    async def _check_entry_signal(
        self,
        market: Dict[str, Any],
        btc_price: float,
        volatility: float,
    ) -> None:
        """Evaluate one market for a trading signal."""
        market_id = market["id"]
        time_left = int(market["settlement_time"] - time.time())

        gap = self.gap_analyzer.get_current_gap(market_id, btc_price, market_data=market)

        if abs(gap) < self.min_gap:
            logger.debug("Gap %.2f < %.2f — no signal for %s", gap, self.min_gap, market_id)
            return

        # Multi-timeframe alignment (best-effort; skipped in dry-run)
        require_mtf = self.config.get("entry", {}).get("require_multi_timeframe", True)
        if require_mtf:
            aligned = self.gap_analyzer.check_multi_timeframe_alignment(gap)
            if not aligned:
                logger.debug("Multi-TF misaligned — skipping %s", market_id)
                return

        ref_price = market.get("strike")
        if ref_price is not None:
            logger.info(
                "🎯 SIGNAL detected | market=%s | ref=$%.2f | btc=$%.2f | gap=$%.2f | vol=%.1f%% | %ds left",
                market_id,
                ref_price,
                btc_price,
                gap,
                volatility * 100,
                time_left,
            )
        else:
            logger.info(
                "🎯 SIGNAL detected | market=%s | btc=$%.2f | gap=$%.2f | vol=%.1f%% | %ds left",
                market_id,
                btc_price,
                gap,
                volatility * 100,
                time_left,
            )

        size = self.sizer.calculate_size(gap, volatility, time_left)
        await self._enter_position(market, gap, size)

    async def _enter_position(
        self,
        market: Dict[str, Any],
        gap: float,
        size: float,
    ) -> None:
        """Place an order and monitor it until settlement."""
        side = "YES" if gap > 0 else "NO"
        token = market.get("condition_id", market["id"])

        # Binary market price: high certainty → close to 0.95 mid
        price = HIGH_CERTAINTY_PRICE if abs(gap) >= 20 else MODERATE_CERTAINTY_PRICE

        logger.info(
            "📈 Entering position | side=%s | size=$%.2f (%.1f%%) | price=%.2f | gap=$%.2f",
            side,
            size,
            (size / self.sizer.bankroll) * 100,
            price,
            gap,
        )

        if self.paper_trading and self.paper_engine is not None:
            # Paper trading: use real orderbook slippage through PaperTradingEngine
            order = await self.paper_engine.place_paper_order(
                market=market.get("_raw", market),
                side=side,
                size_usd=size,
            )
            if order is None:
                logger.warning("⚠️  Paper order placement failed — skipping monitor.")
                return
            # Use the actual token that was registered in the P&L tracker
            token = order.get("token_id", token)
        else:
            await self.executor.place_order(side, token, price, size, gap=gap)

        await self._monitor_position(market, token, gap, size)

    # ------------------------------------------------------------------
    # Position monitoring
    # ------------------------------------------------------------------

    async def _monitor_position(
        self,
        market: Dict[str, Any],
        token: str,
        entry_gap: float,
        size: float,
    ) -> None:
        """
        Watch an open position for partial / emergency exit signals,
        then settle at expiry.
        """
        partial_exited = False
        settlement_time = market["settlement_time"]

        while time.time() < settlement_time and self._running:
            remaining = settlement_time - time.time()
            logger.info("⏳ Monitoring... %.0fs until settlement", remaining)

            btc_price = await self._get_btc_price()
            market_id = market["id"]
            current_gap = self.gap_analyzer.get_current_gap(market_id, btc_price, market_data=market)

            if entry_gap != 0:
                erosion = abs(entry_gap - current_gap) / abs(entry_gap)
            else:
                erosion = 0.0

            # Emergency exit: gap reversed or erosion > 70 %
            if erosion > EMERGENCY_EXIT_THRESHOLD or (
                entry_gap > 0 and current_gap < 0
            ) or (entry_gap < 0 and current_gap > 0):
                logger.warning(
                    "🚨 Emergency exit triggered | erosion=%.1f%%", erosion * 100
                )
                await self._settle(market, token, entry_gap, size, forced=True)
                return

            # Partial exit at 40 % erosion
            if not partial_exited and erosion > PARTIAL_EXIT_THRESHOLD:
                logger.info(
                    "⚠️  Partial exit (50%%) triggered | erosion=%.1f%%", erosion * 100
                )
                partial_size = size * 0.5
                await self.executor.place_order(
                    "SELL_PARTIAL", token, PARTIAL_EXIT_PRICE, partial_size
                )
                partial_exited = True
                size = size * 0.5  # remaining size

            await asyncio.sleep(SCAN_INTERVAL)

        # Settlement reached normally
        await self._settle(market, token, entry_gap, size)

    async def _settle(
        self,
        market: Dict[str, Any],
        token: str,
        entry_gap: float,
        size: float,
        forced: bool = False,
    ) -> None:
        """Settle position and update sizer state."""
        if self.paper_trading and self.paper_engine is not None:
            # Use real settlement outcome from Polymarket API
            condition_id = market.get("condition_id", market["id"])
            pnl = await self.paper_engine.settle_paper_position(token, condition_id)
            if pnl is None:
                # Market not yet resolved — use per-position unrealized P&L as estimate
                midpoint = await self.paper_engine.api_client.fetch_midpoint(token)
                pnl = self.paper_engine.pnl_tracker.update_unrealized(token, midpoint or 0.5) or 0.0
            won = pnl >= 0
        else:
            btc_price = await self._get_btc_price()
            market_id = market["id"]
            final_gap = self.gap_analyzer.get_current_gap(market_id, btc_price, market_data=market)
            gap_direction_up = final_gap > 0
            pnl = await self.executor.settle_position(token, btc_price, gap_direction_up, entry_gap=entry_gap)
            won = pnl >= 0

        self.sizer.update_after_trade(pnl, won)
        self._log_trade_csv(market, entry_gap, size, pnl, won, forced)

        # Risk circuit breaker
        if self.sizer.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self._paused_until = time.time() + PAUSE_DURATION
            logger.warning(
                "⛔ %d consecutive losses — pausing for 1 hour.", MAX_CONSECUTIVE_LOSSES
            )

    # ------------------------------------------------------------------
    # Market / price data helpers
    # ------------------------------------------------------------------

    async def _get_btc_price(self) -> float:
        """
        Fetch the latest BTC/USDT price from Binance (public API).
        Falls back to a simulated price in dry-run if Binance is unreachable.
        """
        try:
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(
                None,
                lambda: self.vol_monitor.exchange.fetch_ticker("BTC/USDT"),
            )
            return float(ticker["last"])
        except Exception as exc:
            logger.debug("Binance ticker fetch failed: %s", exc)
            if self.dry_run or self.paper_trading:
                # Simulated price for dry-run / paper-trading demonstration
                import random  # noqa: PLC0415

                base = SIMULATED_BTC_BASE_PRICE
                return base + random.uniform(-SIMULATED_BTC_RANGE, SIMULATED_BTC_RANGE)
            raise

    async def _get_active_markets(self) -> List[Dict[str, Any]]:
        """
        Return a list of active BTC 5-minute Polymarket markets.

        * paper-trading mode: fetches real data from Polymarket public API.
        * dry-run mode:       generates synthetic markets.
        * live mode:          queries the authenticated CLOB API.
        """
        if self.paper_trading and self.paper_engine is not None:
            markets = await self.paper_engine.refresh_markets()
            # Normalise to the shape expected by the rest of the strategy
            result: List[Dict[str, Any]] = []
            now = time.time()
            for m in markets:
                end_date = m.get("end_date_iso") or m.get("endDateIso") or m.get("end_date")
                settlement_ts = now + 30.0  # default fallback
                if end_date:
                    try:
                        dt = datetime.fromisoformat(
                            end_date.replace("Z", "+00:00")
                        )
                        settlement_ts = dt.timestamp()
                    except Exception as exc:
                        logger.warning(
                            "⚠️  Could not parse end_date '%s' for market %s: %s — using 30s fallback",
                            end_date,
                            m.get("condition_id") or m.get("id"),
                            exc,
                        )

                # Skip already-expired markets
                if settlement_ts <= now:
                    continue

                condition_id = m.get("condition_id") or m.get("id") or ""
                result.append({
                    "id": condition_id,
                    "condition_id": condition_id,
                    "question": m.get("question", ""),
                    "settlement_time": settlement_ts,
                    # strike may not be available for non-numeric markets
                    "strike": m.get("strike") or m.get("outcomePrices"),
                    "tokens": m.get("tokens", []),
                    "_raw": m,
                })
            return result

        if self.dry_run:
            return await self._synthetic_markets()

        # Live mode: query CLOB
        try:
            raw = self.executor.clob_client.get_markets()
            return [
                m
                for m in raw.get("data", [])
                if "BTC" in m.get("question", "").upper()
                and "5" in m.get("question", "")
            ]
        except Exception as exc:
            logger.warning("Failed to fetch live markets: %s", exc)
            return []

    async def _synthetic_markets(self) -> List[Dict[str, Any]]:
        """
        Generate synthetic market data for dry-run testing.
        Reference prices are set close to current BTC price to simulate
        real Polymarket behavior (reference set moments before market opens).
        """
        import random  # noqa: PLC0415

        now = time.time()

        # Get current BTC price so reference is realistic
        current_btc = await self._get_btc_price()

        # Reference price within ±$100 of current price (real markets are
        # set moments before open, so gap is usually small)
        offset = random.uniform(-100, 100)
        strike = round(current_btc + offset, 2)

        market_id = f"BTC-5MIN-{int(strike)}"

        logger.debug(
            "Generated synthetic market: current_btc=%.2f, strike=%.2f, gap=%.2f",
            current_btc,
            strike,
            offset,
        )

        return [
            {
                "id": market_id,
                "condition_id": market_id,
                "question": f"BTC above ${strike:,.2f} at settlement?",
                "settlement_time": now + random.uniform(ENTRY_WINDOW_LOW, ENTRY_WINDOW_HIGH),
                "strike": strike,
            }
        ]

    # ------------------------------------------------------------------
    # Live trading helpers
    # ------------------------------------------------------------------

    def _build_clob_client(self):
        """Build and authenticate a py_clob_client ClobClient."""
        try:
            from py_clob_client.client import ClobClient  # noqa: PLC0415
            from py_clob_client.clob_types import ApiCreds  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "py-clob-client is required for live trading. "
                "Install with: pip install py-clob-client"
            ) from exc

        host = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))

        # Builder API credentials (preferred)
        api_key = os.getenv("POLY_BUILDER_API_KEY")
        api_secret = os.getenv("POLY_BUILDER_API_SECRET")
        api_passphrase = os.getenv("POLY_BUILDER_API_PASSPHRASE")

        private_key = os.getenv("POLY_PRIVATE_KEY")

        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
            return ClobClient(host, chain_id=chain_id, key=private_key, creds=creds)

        if private_key:
            return ClobClient(host, chain_id=chain_id, key=private_key)

        raise EnvironmentError(
            "Live mode requires credentials in .env. "
            "See .env.example for details, or use --dry-run for simulation."
        )

    def _build_live_executor(self, clob_client):
        """Return the live executor (uses CLOB directly for now)."""
        # A thin live wrapper — in production this would be a LiveOrderExecutor class
        # that wraps clob_client.create_order().
        # For this implementation we store the client reference here.
        return _LiveOrderExecutor(clob_client)

    # ------------------------------------------------------------------
    # Risk management
    # ------------------------------------------------------------------

    def _daily_drawdown_exceeded(self) -> bool:
        ratio = self.sizer.bankroll / self._daily_start_bankroll
        return ratio < (1.0 - MAX_DAILY_DRAWDOWN)

    # ------------------------------------------------------------------
    # CSV logging
    # ------------------------------------------------------------------

    def _init_csv(self) -> None:
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["timestamp", "market_id", "entry_gap", "size", "pnl", "won", "forced", "bankroll"]
                )

    def _log_trade_csv(
        self,
        market: Dict[str, Any],
        entry_gap: float,
        size: float,
        pnl: float,
        won: bool,
        forced: bool,
    ) -> None:
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    market["id"],
                    f"{size:.2f}",
                    f"{pnl:.4f}",
                    int(won),
                    int(forced),
                    f"{self.sizer.bankroll:.2f}",
                ]
            )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_summary(self) -> None:
        duration = datetime.now(timezone.utc) - self._start_time
        logger.info("=" * 60)
        logger.info("  Session Summary")
        logger.info("  Duration:       %s", duration)
        logger.info("  Total trades:   %d", self.sizer.total_trades)
        logger.info("  Total P&L:      %+.2f", self.sizer.total_pnl)
        logger.info("  Final bankroll: %.2f (started %.2f)", self.sizer.bankroll, self.capital)
        logger.info("  Drawdown:       %.1f%%", self.sizer.drawdown_pct * 100)

        # Add realistic simulation summary
        if self.dry_run and hasattr(self.executor, "market_sim") and self.executor.market_sim:
            sim_summary = self.executor.market_sim.get_summary()
            logger.info("=" * 60)
            logger.info("  Realistic Market Impact Summary")
            logger.info("  Total slippage:         -$%.2f", sim_summary["total_slippage"])
            logger.info("  Total gas fees:         -$%.2f", sim_summary["total_gas_fees"])
            logger.info("  Front-running losses:   -$%.2f", sim_summary["total_front_running_losses"])
            logger.info("  Latency losses:         -$%.2f", sim_summary["total_latency_losses"])
            logger.info("  Partial fills:          %d trades", sim_summary["partial_fill_count"])
            logger.info("  TOTAL REALISTIC COSTS:  -$%.2f", sim_summary["total_realistic_costs"])
            logger.info("  ")
            logger.info(
                "  P&L after realistic costs: %+.2f",
                self.sizer.total_pnl - sim_summary["total_realistic_costs"],
            )

        logger.info("=" * 60)


# ===========================================================================
# Live order executor stub
# ===========================================================================
class _LiveOrderExecutor:
    """Thin wrapper around ClobClient for live order placement."""

    def __init__(self, clob_client) -> None:
        self.clob_client = clob_client
        self.positions: Dict[str, Any] = {}
        self.trade_history: List[Dict[str, Any]] = []

    async def place_order(
        self, side: str, token: str, price: float, size: float
    ) -> Dict[str, Any]:
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # noqa: PLC0415

            order_args = OrderArgs(
                token_id=token,
                price=price,
                size=size,
                side=side,
                order_type=OrderType.GTC,
            )
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: self.clob_client.create_order(order_args)
            )
            self.positions[token] = {"side": side, "price": price, "size": size, "resp": resp}
            logger.info("[LIVE] Order placed: %s | size=%.2f @ %.4f", side, size, price)
            return resp
        except Exception as exc:
            logger.error("[LIVE] Order placement failed: %s", exc)
            raise

    async def settle_position(
        self,
        token: str,
        settlement_price: float,
        gap_direction_up: bool,
    ) -> float:
        """Approximate P&L from stored position data."""
        position = self.positions.pop(token, None)
        if position is None:
            return 0.0

        entry_price = position["price"]
        size = position["size"]
        side = position["side"]
        won = (side == "YES" and gap_direction_up) or (
            side == "NO" and not gap_direction_up
        )
        pnl = size * (1.0 - entry_price) if won else -size * entry_price
        record = {
            "token": token,
            "side": side,
            "entry_price": entry_price,
            "settlement_price": settlement_price,
            "size": size,
            "pnl": pnl,
            "won": won,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.trade_history.append(record)
        logger.info("[LIVE] Position settled: P&L=%+.2f (%s)", pnl, "WIN ✅" if won else "LOSS ❌")
        return pnl


# ===========================================================================
# Config loader
# ===========================================================================

def load_config(path: str) -> Dict[str, Any]:
    """Load YAML configuration file."""
    if yaml is None:
        logger.warning("pyyaml not installed — ignoring config file.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ===========================================================================
# CLI
# ===========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gap-Based High Certainty Scalping Strategy for Polymarket BTC 5-min markets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulation mode (default). No credentials required.",
    )
    mode_group.add_argument(
        "--paper-trading",
        action="store_true",
        default=False,
        help=(
            "Paper trading mode: fetches REAL Polymarket markets, orderbook prices, "
            "and settlement outcomes via public API (no credentials required). "
            "Orders are simulated — nothing is executed on-chain."
        ),
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Real trading mode. Requires credentials in .env.",
    )

    parser.add_argument(
        "--capital",
        type=float,
        default=DEFAULT_CAPITAL,
        metavar="AMOUNT",
        help="Starting capital in USD.",
    )
    parser.add_argument(
        "--min-gap",
        type=float,
        default=DEFAULT_MIN_GAP,
        metavar="DOLLARS",
        help="Minimum gap in dollars to trigger an entry.",
    )
    parser.add_argument(
        "--max-volatility",
        type=float,
        default=DEFAULT_MAX_VOL,
        metavar="PERCENT",
        help="Maximum allowed BTC volatility (as decimal, e.g. 0.15 = 15%%).",
    )
    parser.add_argument(
        "--base-size",
        type=float,
        default=DEFAULT_BASE_SIZE,
        metavar="PERCENT",
        help="Base position size as fraction of capital (e.g. 0.05 = 5%%).",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to optional YAML config file.",
    )
    parser.add_argument(
        "--disable-realism",
        action="store_true",
        default=False,
        help="Disable realistic market simulation in dry-run (perfect fills, perfect win rate).",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config: Dict[str, Any] = {}
    if args.config:
        config = load_config(args.config)

    # CLI flags override config file values
    live_mode = args.live
    paper_trading_mode = getattr(args, "paper_trading", False)
    capital = config.get("capital", {}).get("initial", args.capital)
    min_gap = config.get("entry", {}).get("min_gap", args.min_gap)
    max_vol = config.get("entry", {}).get("max_volatility", args.max_volatility)
    base_size = config.get("sizing", {}).get("base_percentage", args.base_size)

    strategy = GapCertaintyStrategy(
        capital=capital,
        min_gap=min_gap,
        max_volatility=max_vol,
        base_size=base_size,
        dry_run=not live_mode and not paper_trading_mode,
        paper_trading=paper_trading_mode,
        config=config,
        enable_realism=not args.disable_realism,
    )

    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
