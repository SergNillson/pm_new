"""
Adaptive Position Sizer Module
Dynamically adjusts position size based on gap magnitude, volatility,
win/loss streak, and current bankroll relative to starting capital.
"""

import logging

logger = logging.getLogger(__name__)

# Position size bounds (as fraction of bankroll)
MIN_SIZE_PCT = 0.02   # 2% floor
MAX_SIZE_PCT = 0.12   # 12% ceiling

# Streak multipliers
LOSS_STREAK_2_MULTIPLIER = 0.5   # Halve size after two or more consecutive losses
LOSS_STREAK_1_MULTIPLIER = 0.8
WIN_STREAK_MULTIPLIER = 1.2      # Small boost after 5+ consecutive wins

# Bankroll health multipliers
SURVIVAL_MODE_MULTIPLIER = 0.5   # Bankroll < 70% of starting capital
REDUCED_MODE_MULTIPLIER = 0.7    # Bankroll < 85% of starting capital


class AdaptivePositionSizer:
    """
    Calculates optimal position sizes for each trade, adapting to:
    - Gap size        (larger gap → more confidence → larger size)
    - Volatility      (lower vol  → more confidence → larger size)
    - Streak          (consecutive losses → reduce size for protection)
    - Bankroll ratio  (drawdown → enter survival mode)
    """

    def __init__(self, capital: float, base_pct: float = 0.05):
        """
        Args:
            capital:  Starting capital in dollars.
            base_pct: Base position size as a fraction of bankroll (default 5 %).
        """
        if capital <= 0:
            raise ValueError("capital must be a positive number")
        if not (0 < base_pct <= 1):
            raise ValueError("base_pct must be between 0 (exclusive) and 1 (inclusive)")

        self.capital = capital
        self.bankroll = capital
        self.base_size_pct = base_pct

        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.total_trades = 0
        self.total_pnl = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate_size(
        self,
        gap: float,
        volatility: float,
        time_left: int,  # seconds until settlement
    ) -> float:
        """
        Calculate the optimal position size for one trade entry.

        Factors applied (multiplicative):
        1. Base size      = bankroll × base_pct
        2. Gap multiplier (larger gap → larger position)
        3. Volatility multiplier (lower vol → larger position)
        4. Streak multiplier (losses → cut size)
        5. Bankroll multiplier (drawdown → survival mode)

        Result is clamped to [2 %, 12 %] of current bankroll.

        Args:
            gap:        Absolute gap in dollars (sign is ignored).
            volatility: Current BTC volatility as a fraction.
            time_left:  Seconds until settlement (informational — not used in
                        sizing math but available for future extensions).

        Returns:
            Position size in dollars.
        """
        abs_gap = abs(gap)

        base = self.bankroll * self.base_size_pct

        gap_mult = self._gap_multiplier(abs_gap)
        vol_mult = self._volatility_multiplier(volatility)
        streak_mult = self._streak_multiplier()
        bankroll_mult = self._bankroll_multiplier()

        raw_size = base * gap_mult * vol_mult * streak_mult * bankroll_mult

        min_size = self.bankroll * MIN_SIZE_PCT
        max_size = self.bankroll * MAX_SIZE_PCT
        final_size = max(min_size, min(raw_size, max_size))

        logger.debug(
            "Sizing: base=%.2f gap×%.2f vol×%.2f streak×%.2f bankroll×%.2f → %.2f",
            base,
            gap_mult,
            vol_mult,
            streak_mult,
            bankroll_mult,
            final_size,
        )
        return final_size

    def update_after_trade(self, pnl: float, won: bool) -> None:
        """
        Update internal state after a trade settles.

        Args:
            pnl: Realised profit / loss in dollars (negative = loss).
            won: True if the trade was a win, False otherwise.
        """
        self.bankroll += pnl
        self.total_pnl += pnl
        self.total_trades += 1

        if won:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        logger.debug(
            "After trade: bankroll=%.2f pnl=%+.2f streak W%d/L%d",
            self.bankroll,
            pnl,
            self.consecutive_wins,
            self.consecutive_losses,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown as a fraction of starting capital (0 → no loss)."""
        if self.capital == 0:
            return 0.0
        return max(0.0, 1.0 - self.bankroll / self.capital)

    # ------------------------------------------------------------------
    # Internal multiplier helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _gap_multiplier(abs_gap: float) -> float:
        """Map absolute gap to a size multiplier."""
        if abs_gap >= 25:
            return 1.8
        if abs_gap >= 20:
            return 1.5
        if abs_gap >= 15:
            return 1.2
        return 1.0

    @staticmethod
    def _volatility_multiplier(volatility: float) -> float:
        """Map volatility to a size multiplier (lower vol → larger size)."""
        if volatility < 0.10:
            return 1.3
        if volatility < 0.15:
            return 1.1
        return 1.0

    def _streak_multiplier(self) -> float:
        """Map consecutive win/loss streak to a size multiplier."""
        if self.consecutive_losses >= 2:
            return LOSS_STREAK_2_MULTIPLIER
        if self.consecutive_losses == 1:
            return LOSS_STREAK_1_MULTIPLIER
        if self.consecutive_wins >= 5:
            return WIN_STREAK_MULTIPLIER
        return 1.0

    def _bankroll_multiplier(self) -> float:
        """Reduce size when bankroll has declined significantly."""
        ratio = self.bankroll / self.capital
        if ratio < 0.70:
            return SURVIVAL_MODE_MULTIPLIER
        if ratio < 0.85:
            return REDUCED_MODE_MULTIPLIER
        return 1.0
