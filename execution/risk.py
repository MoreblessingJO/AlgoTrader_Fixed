"""
execution/risk.py
Risk engine: Kelly sizing, drawdown circuit breaker,
correlation filter, daily loss limit.
All position sizing flows through here.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import RISK, MODE

log = logging.getLogger("RiskEngine")


@dataclass
class RiskState:
    balance: float
    initial_balance: float
    daily_start_balance: float
    peak_balance: float
    open_positions: list = field(default_factory=list)
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskEngine:

    def __init__(self, initial_balance: float):
        self.state = RiskState(
            balance=initial_balance,
            initial_balance=initial_balance,
            daily_start_balance=initial_balance,
            peak_balance=initial_balance,
        )
        self._today = datetime.now(timezone.utc).date()

    # ── Daily reset ───────────────────────────────────────────────

    def _check_day_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self.state.daily_start_balance = self.state.balance
            self.state.daily_pnl = 0.0
            self._today = today
            log.info(f"Daily reset — new balance: ${self.state.balance:,.2f}")

    # ── Circuit breakers ──────────────────────────────────────────

    def check_circuit_breakers(self) -> tuple[bool, str]:
        """Returns (can_trade, reason_if_not)."""
        self._check_day_reset()

        if self.state.halted:
            return False, self.state.halt_reason

        # Daily loss limit
        daily_loss_pct = (self.state.daily_start_balance - self.state.balance) / self.state.daily_start_balance
        if daily_loss_pct >= RISK.daily_loss_limit_pct:
            reason = f"Daily loss limit hit: -{daily_loss_pct*100:.1f}% (limit {RISK.daily_loss_limit_pct*100:.0f}%)"
            self._halt(reason)
            return False, reason

        # Max drawdown
        dd_pct = (self.state.peak_balance - self.state.balance) / self.state.peak_balance
        if dd_pct >= RISK.max_drawdown_pct:
            reason = f"Max drawdown hit: -{dd_pct*100:.1f}% (limit {RISK.max_drawdown_pct*100:.0f}%)"
            self._halt(reason)
            return False, reason

        # Open position cap
        if len(self.state.open_positions) >= RISK.max_open_total:
            return False, f"Max open positions ({RISK.max_open_total}) reached"

        return True, ""

    def _halt(self, reason: str):
        self.state.halted = True
        self.state.halt_reason = reason
        log.critical(f"TRADING HALTED: {reason}")

    def resume(self):
        self.state.halted = False
        self.state.halt_reason = ""
        log.info("Trading resumed")

    # ── Position sizing ───────────────────────────────────────────

    def size_position(
        self,
        entry_price: float,
        stop_loss: float,
        market: str = "crypto",
    ) -> tuple[float, float]:
        """
        Returns (quantity, notional_usd).
        Uses fixed fractional (risk_per_trade_pct of balance).
        """
        risk_usd = self.state.balance * RISK.risk_per_trade_pct
        sl_dist  = abs(entry_price - stop_loss)

        if sl_dist < 1e-9:
            log.warning("SL distance too small — skipping")
            return 0.0, 0.0

        slip  = RISK.slippage.get(market, 0.0003)
        effective_entry = entry_price * (1 + slip)
        quantity  = risk_usd / sl_dist
        notional  = quantity * effective_entry

        # Hard notional cap — prevents tiny ATR stops from creating oversized positions
        max_notional = RISK.max_notional_usd.get(market, 500)
        if notional > max_notional:
            quantity = max_notional / effective_entry
            notional = max_notional
            log.debug(f"Notional capped at ${max_notional} for {market}")

        log.debug(
            f"Size: risk=${risk_usd:.2f} | sl_dist={sl_dist:.6f} "
            f"| qty={quantity:.6f} | notional=${notional:.2f}"
        )
        return round(quantity, 6), round(notional, 2)

    def fractional_kelly(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        fraction: float = 0.25,
    ) -> float:
        """
        Kelly fraction for position sizing.
        fraction=0.25 = quarter-Kelly (conservative, recommended).
        Returns recommended risk fraction of balance.
        """
        if avg_loss == 0:
            return RISK.risk_per_trade_pct
        b = avg_win / avg_loss
        q = 1 - win_rate
        kelly = (win_rate * b - q) / b
        kelly = max(0, kelly)
        return min(kelly * fraction, RISK.risk_per_trade_pct * 2)

    # ── RR filter ─────────────────────────────────────────────────

    def passes_rr_filter(self, entry: float, sl: float, tp: float, side: str) -> bool:
        """Reject trades with insufficient reward:risk."""
        if side == "BUY":
            risk   = abs(entry - sl)
            reward = abs(tp - entry)
        else:
            risk   = abs(sl - entry)
            reward = abs(entry - tp)

        if risk < 1e-9:
            return False
        rr = reward / risk
        if rr < RISK.min_rr_ratio:
            log.debug(f"RR filter: {rr:.2f} < {RISK.min_rr_ratio} — rejected")
            return False
        return True

    # ── Correlation filter ────────────────────────────────────────

    def passes_correlation_filter(self, new_symbol: str, new_side: str) -> bool:
        """
        Block a new trade if it's highly correlated with an existing open position.
        Simplified: block same symbol + same direction.
        Production: compute actual return correlation across positions.
        """
        for pos in self.state.open_positions:
            if pos.get("symbol") == new_symbol and pos.get("side") == new_side:
                log.debug(f"Correlation filter: already in {new_symbol} {new_side}")
                return False
        return True

    # ── Balance updates ───────────────────────────────────────────

    def record_trade_open(self, trade_info: dict):
        self.state.open_positions.append(trade_info)

    def record_trade_close(self, trade_id: str, pnl: float):
        self.state.open_positions = [
            p for p in self.state.open_positions if p.get("id") != trade_id
        ]
        self.state.balance     += pnl
        self.state.daily_pnl   += pnl
        self.state.total_pnl   += pnl
        self.state.peak_balance = max(self.state.peak_balance, self.state.balance)

    # ── Stats ──────────────────────────────────────────────────────

    @property
    def drawdown_pct(self) -> float:
        return (self.state.peak_balance - self.state.balance) / self.state.peak_balance * 100

    @property
    def daily_pnl_pct(self) -> float:
        return self.state.daily_pnl / self.state.daily_start_balance * 100

    @property
    def total_return_pct(self) -> float:
        return (self.state.balance - self.state.initial_balance) / self.state.initial_balance * 100

    def summary(self) -> dict:
        return {
            "balance":          round(self.state.balance, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "daily_pnl_pct":    round(self.daily_pnl_pct, 2),
            "drawdown_pct":     round(self.drawdown_pct, 2),
            "open_positions":   len(self.state.open_positions),
            "halted":           self.state.halted,
            "halt_reason":      self.state.halt_reason,
        }
