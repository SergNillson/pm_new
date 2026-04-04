"""
P&L Tracker
============
Tracks unrealized, realized, and settlement P&L for paper trading positions.

All prices are on the binary-market 0–1 scale.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PnLTracker:
    """
    Maintains a ledger of open and closed paper trading positions and
    computes unrealized / realized / settlement P&L.

    Position lifecycle
    ------------------
    1. ``open_position()``        — record entry after a simulated fill.
    2. ``update_unrealized()``    — mark-to-market with the latest real price.
    3. ``close_position()``       — realise P&L using a real settlement price
                                    (or an early-exit price).

    All monetary values are in USD.
    """

    def __init__(self) -> None:
        # token_id → open position dict
        self._open_positions: Dict[str, Dict[str, Any]] = {}
        # Closed trade records
        self._closed_trades: List[Dict[str, Any]] = []

        # Running totals
        self.total_realized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0

    # ------------------------------------------------------------------
    # Open a position
    # ------------------------------------------------------------------

    def open_position(
        self,
        token_id: str,
        side: str,
        size_usd: float,
        fill_price: float,
        fees: float = 0.0,
        market_id: Optional[str] = None,
        question: Optional[str] = None,
    ) -> None:
        """
        Record a new paper trading position after simulated fill.

        Args:
            token_id:   Token identifier.
            side:       "YES" or "NO".
            size_usd:   Dollar amount filled.
            fill_price: Actual fill price (with slippage applied, 0–1 scale).
            fees:       Trading fees deducted at entry (USD).
            market_id:  Optional parent market ID for logging.
            question:   Optional market question for readability.
        """
        if token_id in self._open_positions:
            logger.warning(
                "⚠️  [P&L] Position already open for token %s — overwriting.", token_id
            )

        position = {
            "token_id": token_id,
            "market_id": market_id or token_id,
            "question": question or "",
            "side": side.upper(),
            "size_usd": size_usd,
            "fill_price": fill_price,
            "fees": fees,
            "open_time": time.time(),
            "open_ts": datetime.now(timezone.utc).isoformat(),
            # Running mark-to-market fields
            "current_price": fill_price,
            "unrealized_pnl": 0.0,
        }
        self._open_positions[token_id] = position

        logger.info(
            "📂 [P&L] Position OPENED | token=%s side=%s size=$%.2f fill=%.4f fees=$%.4f",
            token_id,
            side,
            size_usd,
            fill_price,
            fees,
        )
        self.total_fees_paid += fees

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def update_unrealized(self, token_id: str, current_price: float) -> Optional[float]:
        """
        Update the unrealized P&L of an open position given the latest market price.

        For a YES position:  unrealized_pnl = size × (current_price − fill_price)
        For a NO position:   unrealized_pnl = size × (fill_price  − current_price)
          (buying NO at fill_price means we profit when price drops)

        Args:
            token_id:      Position token identifier.
            current_price: Current mid-price from the real orderbook (0–1 scale).

        Returns:
            Unrealized P&L in USD, or *None* if no position is found.
        """
        pos = self._open_positions.get(token_id)
        if pos is None:
            return None

        if pos["side"] == "YES":
            pnl = pos["size_usd"] * (current_price - pos["fill_price"])
        else:
            pnl = pos["size_usd"] * (pos["fill_price"] - current_price)

        pos["current_price"] = current_price
        pos["unrealized_pnl"] = pnl

        logger.debug(
            "📈 [P&L] Unrealized | token=%s side=%s price=%.4f → pnl=%+.4f",
            token_id,
            pos["side"],
            current_price,
            pnl,
        )
        return pnl

    # ------------------------------------------------------------------
    # Close a position
    # ------------------------------------------------------------------

    def close_position(
        self,
        token_id: str,
        settlement_price: float,
        fees: float = 0.0,
        reason: str = "settlement",
    ) -> Optional[float]:
        """
        Realise P&L and move the position to the closed trades ledger.

        Args:
            token_id:         Position token identifier.
            settlement_price: Final price at which the position is closed (0–1 scale).
                              For binary settlement: 1.0 = YES wins, 0.0 = NO wins.
            fees:             Any exit fees (USD).
            reason:           Textual reason for closure (for logging).

        Returns:
            Net realised P&L in USD, or *None* if no matching open position.
        """
        pos = self._open_positions.pop(token_id, None)
        if pos is None:
            logger.warning(
                "⚠️  [P&L] close_position: no open position for token %s", token_id
            )
            return None

        fill_price = pos["fill_price"]
        size_usd = pos["size_usd"]
        side = pos["side"]

        if side == "YES":
            gross_pnl = size_usd * (settlement_price - fill_price)
        else:
            gross_pnl = size_usd * (fill_price - settlement_price)

        total_fees = pos["fees"] + fees
        net_pnl = gross_pnl - total_fees

        self.total_realized_pnl += net_pnl
        self.total_fees_paid += fees

        record = {
            **pos,
            "settlement_price": settlement_price,
            "gross_pnl": gross_pnl,
            "exit_fees": fees,
            "net_pnl": net_pnl,
            "reason": reason,
            "close_time": time.time(),
            "close_ts": datetime.now(timezone.utc).isoformat(),
        }
        self._closed_trades.append(record)

        emoji = "✅" if net_pnl >= 0 else "❌"
        logger.info(
            "📒 [P&L] Position CLOSED %s | token=%s side=%s "
            "fill=%.4f settle=%.4f gross=%+.4f fees=$%.4f net=%+.4f [%s]",
            emoji,
            token_id,
            side,
            fill_price,
            settlement_price,
            gross_pnl,
            total_fees,
            net_pnl,
            reason,
        )
        return net_pnl

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_unrealized_pnl(self) -> float:
        """Return total current unrealized P&L across all open positions."""
        return sum(p["unrealized_pnl"] for p in self._open_positions.values())

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Return a snapshot of all open positions."""
        return list(self._open_positions.values())

    def get_position_size(self, token_id: str) -> float:
        """Return the filled size (USD) of an open position, or 0.0 if not found."""
        return self._open_positions.get(token_id, {}).get("size_usd", 0.0)

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        """Return the full history of closed trades."""
        return list(self._closed_trades)

    def get_summary(self) -> Dict[str, Any]:
        """Return a complete P&L summary."""
        closed = self._closed_trades
        wins = [t for t in closed if t["net_pnl"] >= 0]
        losses = [t for t in closed if t["net_pnl"] < 0]

        total_unrealized = self.get_unrealized_pnl()
        total_pnl = self.total_realized_pnl + total_unrealized

        return {
            "total_trades": len(closed),
            "open_positions": len(self._open_positions),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_realized_pnl": self.total_realized_pnl,
            "total_unrealized_pnl": total_unrealized,
            "total_pnl": total_pnl,
            "total_fees_paid": self.total_fees_paid,
            "avg_win": sum(t["net_pnl"] for t in wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(t["net_pnl"] for t in losses) / len(losses) if losses else 0.0,
        }

    def log_summary(self) -> None:
        """Emit the current P&L summary to the logger at INFO level."""
        s = self.get_summary()
        logger.info("=" * 50)
        logger.info("  P&L Tracker Summary")
        logger.info("  Trades (closed):   %d  (open: %d)", s["total_trades"], s["open_positions"])
        logger.info("  Win / Loss:        %d / %d  (%.1f%%)", s["wins"], s["losses"], s["win_rate"] * 100)
        logger.info("  Realized P&L:      %+.4f", s["total_realized_pnl"])
        logger.info("  Unrealized P&L:    %+.4f", s["total_unrealized_pnl"])
        logger.info("  Total P&L:         %+.4f", s["total_pnl"])
        logger.info("  Fees paid:         %.4f", s["total_fees_paid"])
        logger.info("  Avg win:           %+.4f", s["avg_win"])
        logger.info("  Avg loss:          %+.4f", s["avg_loss"])
        logger.info("=" * 50)
