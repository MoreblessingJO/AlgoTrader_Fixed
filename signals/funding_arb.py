"""
Funding Rate Arbitrage Strategy
================================
Markets: Crypto perpetual futures (Binance, Bybit)
Logic:   When funding rate is extreme, short perp + long spot (or vice versa)
         to collect funding while staying delta-neutral.

Dependencies:
    pip install ccxt pandas numpy scipy ta-lib requests python-dotenv

Environment variables (.env):
    BINANCE_API_KEY=...
    BINANCE_SECRET=...
    BYBIT_API_KEY=...
    BYBIT_SECRET=...
"""

import os
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

import ccxt
import numpy as np
import pandas as pd

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("funding_arb.log")],
)
log = logging.getLogger("FundingArb")


# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

@dataclass
class Config:
    # Which exchange to trade on
    exchange_id: str = "binance"           # "binance" | "bybit"

    # Universe of symbols to scan (perp symbols)
    symbols: list = field(default_factory=lambda: [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "BNB/USDT:USDT", "XRP/USDT:USDT",
    ])

    # ── Entry thresholds ──────────────────────
    # Funding rate per 8h to open a SHORT perp position (collecting positive funding)
    long_funding_threshold: float = 0.001     # +0.1% per 8h → short perp, long spot
    # Funding rate per 8h to open a LONG perp position (collecting negative funding)
    short_funding_threshold: float = -0.0005  # -0.05% per 8h → long perp, short spot

    # Z-score of funding (vs 30-day rolling) required to enter
    z_score_threshold: float = 2.0

    # ── Exit thresholds ───────────────────────
    # Close when funding normalises back toward zero
    exit_z_score: float = 0.5
    # Or close after max hours regardless
    max_hold_hours: int = 72

    # ── Risk / sizing ─────────────────────────
    account_risk_pct: float = 0.02           # 2% of account per trade
    max_open_positions: int = 3
    min_notional_usdt: float = 20.0          # Minimum order size

    # ── Polling intervals ─────────────────────
    scan_interval_seconds: int = 300         # Scan for new signals every 5 min
    monitor_interval_seconds: int = 60       # Monitor open positions every 1 min

    # ── Funding history window ────────────────
    funding_history_periods: int = 90        # 90 × 8h = 30 days of history


CFG = Config()


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

@dataclass
class FundingSignal:
    symbol: str
    current_rate: float          # per 8h
    annualised_rate: float       # current_rate × 3 × 365
    z_score: float               # vs 30d rolling mean/std
    rolling_mean: float
    rolling_std: float
    direction: str               # "SHORT_PERP" | "LONG_PERP"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self):
        d = "▲ SHORT perp" if self.direction == "SHORT_PERP" else "▼ LONG perp"
        return (
            f"{self.symbol} | {d} | rate={self.current_rate*100:.4f}%/8h "
            f"({self.annualised_rate*100:.1f}% APR) | Z={self.z_score:.2f}"
        )


@dataclass
class Position:
    symbol: str
    direction: str               # "SHORT_PERP" | "LONG_PERP"
    perp_size: float             # contracts (negative = short)
    spot_size: float             # base asset held (positive)
    entry_funding_rate: float
    entry_time: datetime
    entry_price: float
    notional_usdt: float
    funding_collected: float = 0.0
    status: str = "OPEN"

    def hold_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 3600

    def funding_apr(self) -> float:
        hrs = max(self.hold_hours(), 0.001)
        return (self.funding_collected / self.notional_usdt) * (8760 / hrs)


# ─────────────────────────────────────────────
#  Exchange connector
# ─────────────────────────────────────────────

class ExchangeConnector:
    def __init__(self, exchange_id: str):
        creds = {
            "binance": {
                "apiKey": os.getenv("BINANCE_API_KEY", ""),
                "secret": os.getenv("BINANCE_SECRET", ""),
                "options": {"defaultType": "future"},
            },
            "bybit": {
                "apiKey": os.getenv("BYBIT_API_KEY", ""),
                "secret": os.getenv("BYBIT_SECRET", ""),
                "options": {"defaultType": "linear"},
            },
        }
        cls = getattr(ccxt, exchange_id)
        self.ex = cls(creds[exchange_id])
        self.ex.load_markets()
        log.info(f"Connected to {exchange_id.upper()}")

    # ── Funding data ──────────────────────────

    def fetch_current_funding(self, symbol: str) -> Optional[float]:
        """Fetch the current funding rate for a perp symbol."""
        try:
            info = self.ex.fetch_funding_rate(symbol)
            return info["fundingRate"]
        except Exception as e:
            log.warning(f"fetch_current_funding({symbol}): {e}")
            return None

    def fetch_funding_history(self, symbol: str, periods: int = 90) -> pd.Series:
        """
        Fetch historical 8h funding rates.
        Returns a pd.Series indexed by UTC timestamp.
        """
        try:
            since = int(time.time() * 1000) - periods * 8 * 3600 * 1000
            rows = self.ex.fetch_funding_rate_history(symbol, since=since, limit=periods)
            df = pd.DataFrame(rows)[["timestamp", "fundingRate"]]
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp").sort_index()
            return df["fundingRate"]
        except Exception as e:
            log.warning(f"fetch_funding_history({symbol}): {e}")
            return pd.Series(dtype=float)

    # ── Prices & balances ─────────────────────

    def fetch_price(self, symbol: str) -> float:
        ticker = self.ex.fetch_ticker(symbol)
        return ticker["last"]

    def fetch_usdt_balance(self) -> float:
        bal = self.ex.fetch_balance()
        return bal.get("USDT", {}).get("free", 0.0)

    # ── Order execution ───────────────────────

    def place_market_order(
        self, symbol: str, side: str, amount: float, reduce_only: bool = False
    ) -> dict:
        """
        Place a market order.
        side: "buy" | "sell"
        amount: base asset quantity (positive)
        """
        params = {}
        if reduce_only:
            params["reduceOnly"] = True

        log.info(f"ORDER → {side.upper()} {amount:.6f} {symbol} {'(reduce-only)' if reduce_only else ''}")
        order = self.ex.create_market_order(symbol, side, amount, params=params)
        log.info(f"ORDER FILLED: {order.get('id')} avg={order.get('average')}")
        return order

    def fetch_position(self, symbol: str) -> Optional[dict]:
        """Return the current open position for a symbol, or None."""
        try:
            positions = self.ex.fetch_positions([symbol])
            for p in positions:
                if abs(p.get("contracts", 0)) > 0:
                    return p
        except Exception as e:
            log.warning(f"fetch_position({symbol}): {e}")
        return None


# ─────────────────────────────────────────────
#  Signal generator
# ─────────────────────────────────────────────

class SignalGenerator:
    def __init__(self, connector: ExchangeConnector):
        self.cx = connector

    def compute_z_score(self, series: pd.Series, current: float) -> tuple[float, float, float]:
        """Return (z_score, rolling_mean, rolling_std)."""
        if len(series) < 10:
            return 0.0, 0.0, 1.0
        mu = series.mean()
        sigma = series.std()
        if sigma == 0:
            return 0.0, mu, sigma
        return (current - mu) / sigma, mu, sigma

    def scan(self) -> list[FundingSignal]:
        """Scan all configured symbols for entry signals."""
        signals = []
        for symbol in CFG.symbols:
            rate = self.cx.fetch_current_funding(symbol)
            if rate is None:
                continue

            history = self.cx.fetch_funding_history(symbol, CFG.funding_history_periods)
            z, mu, sigma = self.compute_z_score(history, rate)
            apr = rate * 3 * 365  # 3 periods per day × 365 days

            # ── Entry condition: SHORT perp (collect positive funding) ──
            if rate >= CFG.long_funding_threshold and z >= CFG.z_score_threshold:
                signals.append(FundingSignal(
                    symbol=symbol,
                    current_rate=rate,
                    annualised_rate=apr,
                    z_score=z,
                    rolling_mean=mu,
                    rolling_std=sigma,
                    direction="SHORT_PERP",
                ))

            # ── Entry condition: LONG perp (collect negative funding) ──
            elif rate <= CFG.short_funding_threshold and z <= -CFG.z_score_threshold:
                signals.append(FundingSignal(
                    symbol=symbol,
                    current_rate=rate,
                    annualised_rate=apr,
                    z_score=z,
                    rolling_mean=mu,
                    rolling_std=sigma,
                    direction="LONG_PERP",
                ))

        signals.sort(key=lambda s: abs(s.z_score), reverse=True)
        return signals


# ─────────────────────────────────────────────
#  Position sizer
# ─────────────────────────────────────────────

class PositionSizer:
    def __init__(self, connector: ExchangeConnector):
        self.cx = connector

    def compute_size(self, symbol: str, price: float) -> float:
        """
        Returns the base asset quantity to trade.
        Sizes by CFG.account_risk_pct of free USDT balance.
        """
        balance = self.cx.fetch_usdt_balance()
        notional = balance * CFG.account_risk_pct
        notional = max(notional, CFG.min_notional_usdt)
        qty = notional / price

        # Round to exchange precision
        market = self.cx.ex.market(symbol)
        precision = market.get("precision", {}).get("amount", 8)
        qty = float(self.cx.ex.amount_to_precision(symbol, qty))
        log.info(f"Size: notional=${notional:.2f} | qty={qty} {symbol} @ ${price:.2f}")
        return qty


# ─────────────────────────────────────────────
#  Exit logic
# ─────────────────────────────────────────────

class ExitEvaluator:
    def __init__(self, connector: ExchangeConnector, signal_gen: SignalGenerator):
        self.cx = connector
        self.sg = signal_gen

    def should_exit(self, pos: Position) -> tuple[bool, str]:
        """
        Returns (should_exit: bool, reason: str).

        Exit conditions:
        1. Funding rate Z-score reverts below exit threshold
        2. Position held longer than max_hold_hours
        3. Funding rate flips sign (now paying instead of collecting)
        """
        # ── 1. Time stop ─────────────────────
        if pos.hold_hours() >= CFG.max_hold_hours:
            return True, f"Max hold time reached ({CFG.max_hold_hours}h)"

        # ── 2. Fetch current funding ──────────
        current_rate = self.cx.fetch_current_funding(pos.symbol)
        if current_rate is None:
            return False, "Could not fetch funding rate"

        history = self.cx.fetch_funding_history(pos.symbol, CFG.funding_history_periods)
        z, _, _ = self.sg.compute_z_score(history, current_rate)

        # ── 3. Funding sign flip ──────────────
        if pos.direction == "SHORT_PERP" and current_rate < 0:
            return True, f"Funding flipped negative ({current_rate*100:.4f}%)"
        if pos.direction == "LONG_PERP" and current_rate > 0:
            return True, f"Funding flipped positive ({current_rate*100:.4f}%)"

        # ── 4. Z-score normalised ─────────────
        if pos.direction == "SHORT_PERP" and z <= CFG.exit_z_score:
            return True, f"Funding Z-score normalised ({z:.2f} ≤ {CFG.exit_z_score})"
        if pos.direction == "LONG_PERP" and z >= -CFG.exit_z_score:
            return True, f"Funding Z-score normalised ({z:.2f} ≥ -{CFG.exit_z_score})"

        return False, ""


# ─────────────────────────────────────────────
#  Trade executor
# ─────────────────────────────────────────────

class TradeExecutor:
    def __init__(self, connector: ExchangeConnector, sizer: PositionSizer):
        self.cx = connector
        self.sz = sizer

    def open_position(self, signal: FundingSignal) -> Optional[Position]:
        """
        Opens a delta-neutral position:
          SHORT_PERP → sell perp + buy spot
          LONG_PERP  → buy perp + sell spot (or skip spot leg if no spot balance)
        """
        price = self.cx.fetch_price(signal.symbol)
        qty = self.sz.compute_size(signal.symbol, price)

        if qty <= 0:
            log.warning(f"Computed qty=0 for {signal.symbol}, skipping")
            return None

        notional = qty * price

        try:
            if signal.direction == "SHORT_PERP":
                # Leg 1: Short the perpetual
                self.cx.place_market_order(signal.symbol, "sell", qty)
                # Leg 2: Buy spot to hedge (delta neutral)
                spot_symbol = signal.symbol.split(":")[0]  # BTC/USDT:USDT → BTC/USDT
                self.cx.place_market_order(spot_symbol, "buy", qty)
                perp_size = -qty
                spot_size = qty

            else:  # LONG_PERP
                # Leg 1: Long the perpetual
                self.cx.place_market_order(signal.symbol, "buy", qty)
                # Leg 2: Sell spot hedge (requires existing spot balance)
                spot_symbol = signal.symbol.split(":")[0]
                self.cx.place_market_order(spot_symbol, "sell", qty)
                perp_size = qty
                spot_size = -qty

        except ccxt.BaseError as e:
            log.error(f"Order failed for {signal.symbol}: {e}")
            return None

        pos = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            perp_size=perp_size,
            spot_size=spot_size,
            entry_funding_rate=signal.current_rate,
            entry_time=datetime.now(timezone.utc),
            entry_price=price,
            notional_usdt=notional,
        )
        log.info(
            f"OPENED {pos.direction} | {pos.symbol} | qty={qty:.6f} "
            f"| notional=${notional:.2f} | funding={signal.current_rate*100:.4f}%/8h"
        )
        return pos

    def close_position(self, pos: Position, reason: str) -> None:
        """Close both legs of the position."""
        qty = abs(pos.perp_size)
        spot_symbol = pos.symbol.split(":")[0]

        try:
            # Close perp leg
            close_side = "buy" if pos.direction == "SHORT_PERP" else "sell"
            self.cx.place_market_order(pos.symbol, close_side, qty, reduce_only=True)

            # Close spot leg
            spot_side = "sell" if pos.direction == "SHORT_PERP" else "buy"
            self.cx.place_market_order(spot_symbol, spot_side, qty)

        except ccxt.BaseError as e:
            log.error(f"Close order failed for {pos.symbol}: {e}")
            return

        pos.status = "CLOSED"
        log.info(
            f"CLOSED {pos.symbol} | reason={reason} | "
            f"held={pos.hold_hours():.1f}h | "
            f"funding collected=${pos.funding_collected:.4f} | "
            f"apr={pos.funding_apr()*100:.1f}%"
        )


# ─────────────────────────────────────────────
#  Funding tracker (accrual accounting)
# ─────────────────────────────────────────────

class FundingTracker:
    """
    Estimates funding collected since entry.
    Actual funding is settled by the exchange every 8h —
    this is a real-time estimate between settlements.
    """

    def update(self, pos: Position, current_rate: float) -> None:
        hours_held = pos.hold_hours()
        periods_elapsed = hours_held / 8.0
        # Funding collected = notional × rate × periods (for SHORT_PERP, rate is positive income)
        sign = 1 if pos.direction == "SHORT_PERP" else -1
        pos.funding_collected = sign * pos.notional_usdt * current_rate * periods_elapsed


# ─────────────────────────────────────────────
#  Main bot
# ─────────────────────────────────────────────

class FundingArbBot:
    def __init__(self):
        self.cx = ExchangeConnector(CFG.exchange_id)
        self.sg = SignalGenerator(self.cx)
        self.sz = PositionSizer(self.cx)
        self.ex = ExitEvaluator(self.cx, self.sg)
        self.te = TradeExecutor(self.cx, self.sz)
        self.ft = FundingTracker()
        self.positions: list[Position] = []
        self._last_scan = 0.0
        self._last_monitor = 0.0

    # ── Main loop ─────────────────────────────

    def run(self):
        log.info("=" * 60)
        log.info("Funding Rate Arbitrage Bot starting")
        log.info(f"Exchange: {CFG.exchange_id.upper()}")
        log.info(f"Symbols:  {CFG.symbols}")
        log.info(f"Thresholds: long≥{CFG.long_funding_threshold*100:.3f}%  short≤{CFG.short_funding_threshold*100:.3f}%")
        log.info(f"Z-score entry={CFG.z_score_threshold}  exit={CFG.exit_z_score}")
        log.info("=" * 60)

        while True:
            now = time.time()

            # ── Scan for new signals ──────────
            if now - self._last_scan >= CFG.scan_interval_seconds:
                self._scan_and_enter()
                self._last_scan = now

            # ── Monitor open positions ────────
            if now - self._last_monitor >= CFG.monitor_interval_seconds:
                self._monitor_positions()
                self._last_monitor = now

            time.sleep(10)

    def _scan_and_enter(self):
        open_count = sum(1 for p in self.positions if p.status == "OPEN")
        if open_count >= CFG.max_open_positions:
            log.info(f"Max positions ({CFG.max_open_positions}) reached, skip scan")
            return

        log.info("Scanning for funding signals...")
        signals = self.sg.scan()

        if not signals:
            log.info("No signals found")
            return

        for sig in signals:
            log.info(f"Signal: {sig}")

        # Already in this symbol?
        open_symbols = {p.symbol for p in self.positions if p.status == "OPEN"}

        for sig in signals:
            if open_count >= CFG.max_open_positions:
                break
            if sig.symbol in open_symbols:
                log.info(f"Already in {sig.symbol}, skipping")
                continue

            pos = self.te.open_position(sig)
            if pos:
                self.positions.append(pos)
                open_symbols.add(pos.symbol)
                open_count += 1

    def _monitor_positions(self):
        open_positions = [p for p in self.positions if p.status == "OPEN"]
        if not open_positions:
            return

        log.info(f"Monitoring {len(open_positions)} open position(s)")
        for pos in open_positions:
            # Update funding accrual estimate
            rate = self.cx.fetch_current_funding(pos.symbol)
            if rate:
                self.ft.update(pos, rate)

            # Evaluate exit
            should_exit, reason = self.ex.should_exit(pos)
            if should_exit:
                self.te.close_position(pos, reason)
            else:
                log.info(
                    f"  {pos.symbol} | {pos.direction} | "
                    f"held={pos.hold_hours():.1f}h | "
                    f"funding≈${pos.funding_collected:.4f}"
                )

    # ── Reporting ─────────────────────────────

    def summary(self) -> pd.DataFrame:
        rows = []
        for p in self.positions:
            rows.append({
                "symbol": p.symbol,
                "direction": p.direction,
                "status": p.status,
                "entry_rate_%_8h": round(p.entry_funding_rate * 100, 4),
                "notional_usdt": round(p.notional_usdt, 2),
                "hold_hours": round(p.hold_hours(), 1),
                "funding_collected_usdt": round(p.funding_collected, 4),
                "est_apr_%": round(p.funding_apr() * 100, 1),
            })
        return pd.DataFrame(rows)


# ─────────────────────────────────────────────
#  Backtester (paper mode)
# ─────────────────────────────────────────────

class Backtester:
    """
    Offline backtest using historical funding rate data.
    Does NOT execute real orders — useful for strategy validation.
    """

    def __init__(self, connector: ExchangeConnector):
        self.cx = connector
        self.sg = SignalGenerator(connector)

    def run(self, symbol: str, lookback_periods: int = 180) -> pd.DataFrame:
        """
        Simulate the strategy on historical funding rate data.
        Returns a DataFrame of all simulated trades.
        """
        log.info(f"Backtesting {symbol} over {lookback_periods} funding periods...")
        history = self.cx.fetch_funding_history(symbol, lookback_periods)

        if history.empty:
            log.warning("No history returned")
            return pd.DataFrame()

        trades = []
        i = 30  # Minimum window for rolling stats

        while i < len(history):
            window = history.iloc[:i]
            current = history.iloc[i]
            mu = window.mean()
            sigma = window.std()
            z = (current - mu) / sigma if sigma > 0 else 0.0
            ts = history.index[i]

            direction = None
            if current >= CFG.long_funding_threshold and z >= CFG.z_score_threshold:
                direction = "SHORT_PERP"
            elif current <= CFG.short_funding_threshold and z <= -CFG.z_score_threshold:
                direction = "LONG_PERP"

            if direction:
                # Simulate hold until exit condition met (max 9 periods = 72h)
                funding_collected = 0.0
                j = i
                exit_reason = "max_hold"

                while j < min(i + CFG.max_hold_hours // 8, len(history) - 1):
                    j += 1
                    future_rate = history.iloc[j]
                    future_window = history.iloc[:j]
                    fz = (future_rate - future_window.mean()) / (future_window.std() or 1.0)

                    sign = 1 if direction == "SHORT_PERP" else -1
                    funding_collected += sign * future_rate  # per period

                    if direction == "SHORT_PERP" and (future_rate < 0 or fz <= CFG.exit_z_score):
                        exit_reason = "z_normalised" if fz <= CFG.exit_z_score else "rate_flip"
                        break
                    if direction == "LONG_PERP" and (future_rate > 0 or fz >= -CFG.exit_z_score):
                        exit_reason = "z_normalised" if fz >= -CFG.exit_z_score else "rate_flip"
                        break

                trades.append({
                    "entry_time": ts,
                    "symbol": symbol,
                    "direction": direction,
                    "entry_rate_%": round(current * 100, 4),
                    "entry_z": round(z, 2),
                    "periods_held": j - i,
                    "hours_held": (j - i) * 8,
                    "funding_collected_%": round(funding_collected * 100, 4),
                    "exit_reason": exit_reason,
                    "profitable": funding_collected > 0,
                })
                i = j + 1  # Skip to after this trade
            else:
                i += 1

        df = pd.DataFrame(trades)
        if not df.empty:
            log.info(f"\n{'='*50}")
            log.info(f"Backtest results for {symbol}")
            log.info(f"Total trades:    {len(df)}")
            log.info(f"Win rate:        {df['profitable'].mean()*100:.1f}%")
            log.info(f"Avg funding/trade: {df['funding_collected_%'].mean():.4f}%")
            log.info(f"Total funding:   {df['funding_collected_%'].sum():.4f}%")
            log.info(f"Avg hold:        {df['hours_held'].mean():.1f}h")
        return df


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "live"

    if mode == "backtest":
        # ── Paper backtest mode ──────────────
        # Usage: python funding_rate_arb.py backtest
        cx = ExchangeConnector(CFG.exchange_id)
        bt = Backtester(cx)
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            df = bt.run(sym, lookback_periods=180)
            if not df.empty:
                print(df.to_string(index=False))
                print()

    elif mode == "scan":
        # ── One-shot signal scan ─────────────
        # Usage: python funding_rate_arb.py scan
        cx = ExchangeConnector(CFG.exchange_id)
        sg = SignalGenerator(cx)
        signals = sg.scan()
        if signals:
            print("\nActive funding signals:")
            for s in signals:
                print(f"  {s}")
        else:
            print("No signals above threshold right now.")

    else:
        # ── Live trading mode ────────────────
        # Usage: python funding_rate_arb.py live
        bot = FundingArbBot()
        try:
            bot.run()
        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            print("\n" + bot.summary().to_string(index=False))
