"""
execution/broker.py
Unified order execution for all three markets.
In paper mode: simulates fills with slippage.
In live mode: routes to Binance / Deriv / OANDA.
"""

import asyncio, logging, uuid, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MODE, RISK
from execution.trade_journal import TradeJournal

log = logging.getLogger("Broker")


@dataclass
class Order:
    id: str                     = field(default_factory=lambda: str(uuid.uuid4())[:8])
    market: str                 = ""     # "crypto" | "crash_boom" | "forex"
    strategy: str               = ""
    symbol: str                 = ""
    side: str                   = ""     # "BUY" | "SELL"
    order_type: str             = "MARKET"
    quantity: float             = 0.0
    notional_usd: float         = 0.0
    entry_price: float          = 0.0
    fill_price: float           = 0.0
    sl: float                   = 0.0
    tp: float                   = 0.0
    tp2: float                  = 0.0    # second TP for dual-lot strategies
    trailing_pts: float         = 0.0    # trailing stop in price points
    status: str                 = "PENDING"   # PENDING | OPEN | CLOSED | CANCELLED
    open_time: datetime         = field(default_factory=lambda: datetime.now(timezone.utc))
    close_time: Optional[datetime] = None
    pnl_usd: float              = 0.0
    exit_price: float           = 0.0
    exit_reason: str            = ""
    session: str                = ""
    slippage_cost: float        = 0.0
    lot: str                    = "single"    # "single" | "scalper" | "runner"
    exchange_order_id: str      = ""
    metadata: dict              = field(default_factory=dict)

    def hold_seconds(self) -> float:
        end = self.close_time or datetime.now(timezone.utc)
        return (end - self.open_time).total_seconds()

    def unrealised_pnl(self, current_price: float) -> float:
        if self.status != "OPEN":
            return 0.0
        if self.side == "BUY":
            return (current_price - self.fill_price) * self.quantity
        return (self.fill_price - current_price) * self.quantity

    def check_sl_tp(self, current_price: float) -> Optional[str]:
        """Returns 'SL' | 'TP' | 'TP2' | None"""
        if self.status != "OPEN":
            return None
        if self.side == "BUY":
            if self.sl > 0 and current_price <= self.sl:
                return "SL"
            if self.tp2 > 0 and current_price >= self.tp2:
                return "TP2"
            if self.tp > 0 and current_price >= self.tp:
                return "TP"
        else:
            if self.sl > 0 and current_price >= self.sl:
                return "SL"
            if self.tp2 > 0 and current_price <= self.tp2:
                return "TP2"
            if self.tp > 0 and current_price <= self.tp:
                return "TP"
        return None

    def update_trailing_stop(self, current_price: float):
        """Ratchet trailing stop as price moves in favour."""
        if self.trailing_pts <= 0 or self.status != "OPEN":
            return
        if self.side == "BUY":
            new_sl = current_price - self.trailing_pts
            if new_sl > self.sl:
                self.sl = new_sl
        else:
            new_sl = current_price + self.trailing_pts
            if new_sl < self.sl or self.sl == 0:
                self.sl = new_sl


class Broker:
    """
    Unified broker. Routes to correct exchange in live mode,
    simulates execution in paper mode.
    """

    def __init__(
        self,
        mode: str = None,
        binance_feed=None,
        deriv_feed=None,
        oanda_feed=None,
        risk_engine=None,
    ):
        self.mode        = mode or MODE
        self.binance     = binance_feed
        self.deriv       = deriv_feed
        self.oanda       = oanda_feed
        self.risk        = risk_engine
        self._orders: list[Order] = []
        self.journal     = TradeJournal(mode=self.mode)

    # ── Place order ───────────────────────────────────────────────

    async def place(self, order: Order) -> Order:
        """Main entry point. Routes to paper or live."""
        can_trade, reason = self.risk.check_circuit_breakers() if self.risk else (True, "")
        if not can_trade:
            log.warning(f"Order blocked by risk engine: {reason}")
            order.status = "CANCELLED"
            order.exit_reason = reason
            return order

        if not self.risk.passes_correlation_filter(order.symbol, order.side):
            order.status = "CANCELLED"
            order.exit_reason = "correlation_filter"
            return order

        if self.mode == "paper":
            order = await self._paper_fill(order)
        else:
            order = await self._live_fill(order)

        if order.status == "OPEN":
            self._orders.append(order)
            self.journal.record_open(order)
            if self.risk:
                self.risk.record_trade_open({
                    "id": order.id, "symbol": order.symbol,
                    "side": order.side, "market": order.market,
                    "notional": order.notional_usd,
                })
            log.info(
                f"ORDER OPEN [{order.market.upper()}] {order.strategy} "
                f"{order.side} {order.symbol} @ {order.fill_price:.5f} "
                f"| SL={order.sl:.5f} TP={order.tp:.5f} "
                f"| lot={order.lot} | ${order.notional_usd:.0f}"
            )

        return order

    async def close(self, order: Order, current_price: float, reason: str = "") -> Order:
        """Close an open order."""
        if order.status != "OPEN":
            return order

        slip  = RISK.slippage.get(order.market, 0.0003)
        if order.side == "BUY":
            exit_price = current_price * (1 - slip)
            pnl = (exit_price - order.fill_price) * order.quantity
        else:
            exit_price = current_price * (1 + slip)
            pnl = (order.fill_price - exit_price) * order.quantity

        pnl -= order.slippage_cost

        order.exit_price  = exit_price
        order.pnl_usd     = round(pnl, 4)
        order.exit_reason = reason
        order.close_time  = datetime.now(timezone.utc)
        order.status      = "CLOSED"

        if self.risk:
            self.risk.record_trade_close(order.id, pnl)
        self.journal.record_close(order)

        sign = "+" if pnl >= 0 else ""
        log.info(
            f"ORDER CLOSED [{order.market.upper()}] {order.strategy} "
            f"{order.symbol} @ {exit_price:.5f} "
            f"| PnL={sign}{pnl:.2f} USD | reason={reason} "
            f"| held={order.hold_seconds()/60:.1f}min"
        )
        return order

    # ── Paper fill ────────────────────────────────────────────────

    async def _paper_fill(self, order: Order) -> Order:
        slip  = RISK.slippage.get(order.market, 0.0003)
        if order.side == "BUY":
            fill_price = order.entry_price * (1 + slip)
        else:
            fill_price = order.entry_price * (1 - slip)

        slip_cost = abs(fill_price - order.entry_price) * order.quantity
        order.fill_price    = round(fill_price, 6)
        order.slippage_cost = round(slip_cost, 4)
        order.status        = "OPEN"
        return order

    # ── Live fills ────────────────────────────────────────────────

    async def _live_fill(self, order: Order) -> Order:
        if order.market == "crypto":
            return await self._fill_binance(order)
        elif order.market == "crash_boom":
            return await self._fill_deriv(order)
        elif order.market == "forex":
            return await self._fill_oanda(order)
        return order

    async def _fill_binance(self, order: Order) -> Order:
        try:
            import ccxt.async_support as ccxt
            exchange = ccxt.binance({
                "apiKey": __import__("config").BINANCE_API_KEY,
                "secret": __import__("config").BINANCE_SECRET,
                "options": {"defaultType": "future"},
            })
            if __import__("config").BINANCE_TESTNET:
                exchange.set_sandbox_mode(True)

            side = order.side.lower()
            result = await exchange.create_market_order(
                order.symbol, side, order.quantity
            )
            order.fill_price        = float(result.get("average", order.entry_price))
            order.exchange_order_id = str(result.get("id", ""))
            order.status            = "OPEN"
            await exchange.close()
        except Exception as e:
            log.error(f"Binance fill failed: {e}")
            order.status      = "CANCELLED"
            order.exit_reason = str(e)
        return order

    async def _fill_deriv(self, order: Order) -> Order:
        # Deriv trade execution via API (contract purchase)
        # Full implementation requires Deriv contract API
        # For now falls back to paper fill with live price
        log.warning("Deriv live execution — using paper fill with live price")
        return await self._paper_fill(order)

    async def _fill_oanda(self, order: Order) -> Order:
        try:
            import aiohttp
            from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT
            base = (
                "https://api-fxpractice.oanda.com/v3"
                if OANDA_ENVIRONMENT == "practice"
                else "https://api-fxtrade.oanda.com/v3"
            )
            headers = {
                "Authorization": f"Bearer {OANDA_API_KEY}",
                "Content-Type": "application/json",
            }
            units = int(order.quantity) if order.side == "BUY" else -int(order.quantity)
            body  = {"order": {"type": "MARKET", "instrument": order.symbol, "units": str(units)}}

            async with aiohttp.ClientSession(headers=headers) as s:
                url = f"{base}/accounts/{OANDA_ACCOUNT_ID}/orders"
                async with s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()

            fill = data.get("orderFillTransaction", {})
            order.fill_price        = float(fill.get("price", order.entry_price))
            order.exchange_order_id = fill.get("id", "")
            order.status            = "OPEN"
        except Exception as e:
            log.error(f"OANDA fill failed: {e}")
            order.status      = "CANCELLED"
            order.exit_reason = str(e)
        return order

    # ── Position management ───────────────────────────────────────

    async def monitor_positions(self, price_getter) -> list[Order]:
        """
        Check all open orders for SL/TP/trailing hits.
        price_getter(market, symbol) → float
        Returns list of orders that were closed this cycle.
        """
        closed = []
        for order in [o for o in self._orders if o.status == "OPEN"]:
            try:
                price = await price_getter(order.market, order.symbol)
                if price is None:
                    continue
                order.update_trailing_stop(price)
                hit = order.check_sl_tp(price)
                if hit:
                    await self.close(order, price, hit)
                    closed.append(order)
            except Exception as e:
                log.warning(f"Monitor error ({order.symbol}): {e}")
        return closed

    # ── Query ─────────────────────────────────────────────────────

    @property
    def open_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == "OPEN"]

    @property
    def closed_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == "CLOSED"]

    def open_for_symbol(self, symbol: str) -> list[Order]:
        return [o for o in self.open_orders if o.symbol == symbol]

    def get_stats(self, market: str = None) -> dict:
        orders = self.closed_orders
        if market:
            orders = [o for o in orders if o.market == market]
        if not orders:
            return {"trades": 0, "wr": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        wins = [o for o in orders if o.pnl_usd > 0]
        pnls = [o.pnl_usd for o in orders]
        import numpy as np
        return {
            "trades":    len(orders),
            "wr":        round(len(wins) / len(orders) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl":   round(float(np.mean(pnls)), 2),
        }
