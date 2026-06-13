"""
simulate_trades.py
Starts the API server with 10 pre-seeded paper trades injected so the
dashboard shows realistic data.  Replaces the current uvicorn process.

Run:  python3 simulate_trades.py
"""

import sys, os, uvicorn
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone, timedelta
from execution.broker import Broker, Order
from execution.risk   import RiskEngine
from api.server       import app, inject_bot

# ── 10 simulated paper trades ─────────────────────────────────────────
# (market, strategy, symbol, side, entry, sl, tp, pnl, held_min, reason, lot, mins_ago)
TRADES = [
    ("crash_boom", "CB-S4", "BOOM500",   "BUY",  8012.40, 7992.40, 8112.40,  +48.20, 14, "TP",  "single",  85),
    ("crash_boom", "CB-S1", "CRASH500",  "SELL", 3841.20, 3861.20, 3741.20,  +62.50, 22, "TP",  "single",  72),
    ("crash_boom", "CB-S2", "BOOM1000",  "BUY",  1204.80, 1189.80, 1254.80,  +38.90, 18, "TP",  "single",  61),
    ("crash_boom", "CB-S3", "CRASH1000", "SELL", 2987.60, 3012.60, 2887.60,  -19.80, 31, "SL",  "scalper", 55),
    ("crash_boom", "CB-S4", "BOOM500",   "BUY",  8094.10, 8074.10, 8194.10,  +51.30, 11, "TP",  "single",  44),
    ("crash_boom", "CB-S1", "CRASH500",  "SELL", 3792.50, 3812.50, 3692.50,  +44.70, 27, "TP",  "single",  38),
    ("forex",      "FX-S1", "EUR_USD",   "SELL", 1.08420, 1.08580, 1.08060,  +28.40, 41, "TP",  "single",  30),
    ("forex",      "FX-S2", "GBP_USD",   "SELL", 1.27180, 1.27340, 1.26820,  +34.10, 38, "TP",  "runner",  22),
    ("crash_boom", "CB-S2", "BOOM1000",  "BUY",  1198.30, 1183.30, 1248.30,  -18.60, 8,  "SL",  "single",  14),
    ("forex",      "FX-S4", "EUR_JPY",   "BUY",  162.340, 161.940, 163.140,  +26.80, 55, "TP",  "single",   6),
]


class _MockFeed:
    """Minimal feed shim so broker.close() can call get_price without crashing."""
    _prices: dict = {}
    def get_price(self, symbol):   return None
    async def get_mid(self, s):    return None


class SimBot:
    """
    Mimics just enough of TradingBot's interface for the API server
    to build a live snapshot from real data.
    """
    def __init__(self):
        self.mode    = "paper"
        self._running = True

        self.risk   = RiskEngine(initial_balance=10_000.0)
        self.broker = Broker(
            mode        = "paper",
            binance_feed= _MockFeed(),
            deriv_feed  = _MockFeed(),
            oanda_feed  = _MockFeed(),
            risk_engine = self.risk,
        )

        # Shims used by _build_live_snapshot() price lookup
        self.binance = type("B", (), {"_prices": {}})()
        self.deriv   = _MockFeed()
        self.oanda   = _MockFeed()

        self._strategy_stats: dict = {}
        self._seed_trades()

    # ── Seed the 10 trades ────────────────────────────────────────────

    def _seed_trades(self):
        now = datetime.now(timezone.utc)

        for (market, strategy, symbol, side,
             entry, sl, tp, pnl, held_min, reason, lot, mins_ago) in TRADES:

            open_time  = now - timedelta(minutes=mins_ago + held_min)
            close_time = now - timedelta(minutes=mins_ago)

            slip   = 0.0005 if market == "crash_boom" else 0.00010
            fill   = entry * (1 + slip) if side == "BUY" else entry * (1 - slip)
            exit_p = fill + pnl / abs(pnl) * abs(tp - entry) * (1 - slip)

            o = Order(
                market      = market,
                strategy    = strategy,
                symbol      = symbol,
                side        = side,
                quantity    = round(abs(pnl) / max(abs(tp - entry), 1e-9), 6),
                notional_usd= round(abs(pnl) / 0.02, 2),
                entry_price = entry,
                fill_price  = round(fill, 6),
                sl          = sl,
                tp          = tp,
                status      = "CLOSED",
                open_time   = open_time,
                close_time  = close_time,
                pnl_usd     = round(pnl, 2),
                exit_price  = round(exit_p, 6),
                exit_reason = reason,
                lot         = lot,
            )
            self.broker._orders.append(o)

            # Keep risk engine balance in sync
            self.risk.state.balance     += pnl
            self.risk.state.daily_pnl   += pnl
            self.risk.state.total_pnl   += pnl
            self.risk.state.peak_balance = max(
                self.risk.state.peak_balance, self.risk.state.balance
            )

            # Update strategy stats
            s = self._strategy_stats.setdefault(
                strategy, {"trades": 0, "wins": 0, "pnl": 0.0}
            )
            s["trades"] += 1
            if pnl > 0:
                s["wins"] += 1
            s["pnl"] = round(s["pnl"] + pnl, 2)

        print(f"Seeded {len(TRADES)} trades — "
              f"balance ${self.risk.state.balance:,.2f} | "
              f"P&L ${self.risk.state.total_pnl:+.2f}")


# ── Inject and start ──────────────────────────────────────────────────

if __name__ == "__main__":
    bot = SimBot()
    inject_bot(bot)
    print("Mock bot injected — starting API on :8081")
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
