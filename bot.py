"""
bot.py — Main trading bot orchestrator.

Runs three async market loops simultaneously:
  • Crypto loop  (Binance, 60s cycle)
  • Crash/Boom loop (Deriv, 30s cycle)
  • Forex loop   (OANDA, 60s cycle)

Plus:
  • Position monitor loop (5s cycle)
  • Dashboard server
  • Daily report scheduler

Usage:
    python bot.py          # full run (paper or live based on .env MODE)
    python bot.py --paper  # force paper mode
    python bot.py --live   # force live mode (USE WITH CAUTION)
"""

import asyncio
import logging
import sys
import os
import time
import argparse
from datetime import datetime, timezone
import uvicorn

# ── Path setup ────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from config import (
    MODE, LOG_LEVEL, DASHBOARD_PORT,
    CRYPTO_SYMBOLS, CRASH_BOOM_SYMBOLS, FOREX_PAIRS,
    RISK as RISK_CFG, CB, FX, CR, BACKTEST_WR, MON,
)
from data.binance_feed  import BinanceFeed
from data.deriv_feed    import DerivFeed
from data.oanda_feed    import OANDAFeed
from execution.broker   import Broker, Order
from execution.risk     import RiskEngine
from signals.consensus  import ConsensusEngine, Signal
from monitor.telegram   import (
    alert_trade_open, alert_trade_close,
    alert_circuit_breaker, alert_divergence, send_daily_report
)
from api.server import app as _api_app, inject_bot

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
    ],
)
log = logging.getLogger("Bot")


# ══════════════════════════════════════════════════════════════════════
#  Indicator helpers (inline — no extra import needed)
# ══════════════════════════════════════════════════════════════════════

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(com=period-1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period-1, adjust=False).mean()

def bb_width(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.Series:
    mid = series.rolling(period, min_periods=1).mean()
    std = series.rolling(period, min_periods=1).std().fillna(0)
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return (upper - lower) / mid.replace(0, np.nan)

def compression_ratio(atr_s: pd.Series, bbw: pd.Series, period: int = 20) -> pd.Series:
    def _norm(s):
        mn = s.rolling(period, min_periods=1).min()
        mx = s.rolling(period, min_periods=1).max()
        return (s - mn) / (mx - mn + 1e-9)
    return (_norm(atr_s) + _norm(bbw)) / 2

def rsi_divergence(price: pd.Series, rsi_vals: pd.Series, lookback: int = 20) -> pd.Series:
    n   = len(price)
    out = pd.Series(0.0, index=price.index)
    p   = price.values
    r   = rsi_vals.values
    for i in range(lookback, n):
        wp, wr = p[i-lookback:i+1], r[i-lookback:i+1]
        if np.isnan(wr).any():
            continue
        ph = np.argmax(wp[:-1])
        if wp[-1] > wp[ph] and wr[-1] < wr[ph]:
            out.iloc[i] = -1.0
        pl = np.argmin(wp[:-1])
        if wp[-1] < wp[pl] and wr[-1] > wr[pl]:
            out.iloc[i] = 1.0
    return out

def hurst(ts: np.ndarray) -> float:
    if len(ts) < 20:
        return 0.5
    lags = range(2, min(20, len(ts)//2))
    rs = []
    for lag in lags:
        sub  = ts[:lag]
        mean = sub.mean()
        dev  = np.cumsum(sub - mean)
        r    = dev.max() - dev.min()
        s    = sub.std()
        if s > 0:
            rs.append((lag, r/s))
    if len(rs) < 4:
        return 0.5
    x = np.log([v[0] for v in rs])
    y = np.log([v[1] for v in rs])
    return float(np.polyfit(x, y, 1)[0])

def tssl(tick_change: pd.Series, window: int = 50) -> float:
    """Last TSSL value."""
    if len(tick_change) < window:
        return 0.0
    arr = tick_change.values[-window:]
    dirs = np.sign(arr)
    streak = 0
    strength = 0.0
    cur_dir = dirs[-1]
    for j in range(len(dirs)-1, -1, -1):
        if dirs[j] == cur_dir:
            streak += 1
            strength += abs(arr[j])
        else:
            break
    mean_abs = np.abs(arr).mean()
    return float(cur_dir * (streak/window) * (strength/(mean_abs*window + 1e-9)))


# ══════════════════════════════════════════════════════════════════════
#  Main Bot
# ══════════════════════════════════════════════════════════════════════

class TradingBot:

    def __init__(self, mode: str = None):
        self.mode      = mode or MODE
        log.info(f"Initialising bot — mode={self.mode.upper()}")

        # Feeds
        self.binance   = BinanceFeed()
        self.deriv     = DerivFeed()
        self.oanda     = OANDAFeed()

        # Execution
        self.risk      = RiskEngine(initial_balance=10_000.0)
        self.broker    = Broker(
            mode=self.mode,
            binance_feed=self.binance,
            deriv_feed=self.deriv,
            oanda_feed=self.oanda,
            risk_engine=self.risk,
        )
        self.consensus = ConsensusEngine(broker=self.broker)

        # State
        self._last_daily_report = datetime.now(timezone.utc).date()
        self._strategy_stats: dict[str, dict] = {}
        self._running = False

    # ── Entry point ───────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("=" * 65)
        log.info(f"  TRADING BOT STARTING — {self.mode.upper()} MODE")
        log.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        log.info("=" * 65)

        await asyncio.gather(
            self.binance.stream(CRYPTO_SYMBOLS),
            self.deriv.stream(),
            self.oanda.poll_prices(interval=3),
            self._crypto_loop(),
            self._crash_boom_loop(),
            self._forex_loop(),
            self._monitor_loop(),
            self._daily_report_loop(),
            self._api_server(),
        )

    async def _api_server(self):
        config = uvicorn.Config(
            _api_app, host="0.0.0.0", port=8081,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)
        log.info("API server starting on :8081")
        await server.serve()

    # ══════════════════════════════════════════════════════════════
    #  Market loops
    # ══════════════════════════════════════════════════════════════

    async def _crypto_loop(self):
        log.info("Crypto loop started")
        while self._running:
            try:
                for sym in CRYPTO_SYMBOLS:
                    await self._eval_crypto(sym)
            except Exception as e:
                log.error(f"Crypto loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _crash_boom_loop(self):
        log.info("Crash/Boom loop started")
        while self._running:
            try:
                for sym, avg_ticks, direction in CRASH_BOOM_SYMBOLS:
                    await self._eval_crash_boom(sym, avg_ticks, direction)
            except Exception as e:
                log.error(f"CB loop error: {e}", exc_info=True)
            await asyncio.sleep(30)

    async def _forex_loop(self):
        log.info("Forex loop started")
        while self._running:
            try:
                for pair in FOREX_PAIRS:
                    await self._eval_forex(pair)
            except Exception as e:
                log.error(f"Forex loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _monitor_loop(self):
        log.info("Monitor loop started")
        while self._running:
            try:
                async def get_price(market: str, symbol: str) -> float | None:
                    if market == "crypto":
                        return await self.binance.get_price(symbol)
                    elif market == "crash_boom":
                        return self.deriv.get_price(symbol)
                    elif market == "forex":
                        return await self.oanda.get_mid(symbol)
                    return None

                closed = await self.broker.monitor_positions(get_price)
                for order in closed:
                    self.consensus.register_close(order.symbol, order.strategy)
                    self._update_strategy_stats(order)          # stats first
                    await alert_trade_close(order, self._strategy_stats)
                    await self._check_wr_divergence(order.strategy)

            except Exception as e:
                log.error(f"Monitor loop error: {e}", exc_info=True)
            await asyncio.sleep(5)

    async def _daily_report_loop(self):
        while self._running:
            now = datetime.now(timezone.utc)
            if now.date() != self._last_daily_report and now.hour == 0:
                stats = {
                    "Crypto":     self.broker.get_stats("crypto"),
                    "Crash/Boom": self.broker.get_stats("crash_boom"),
                    "Forex":      self.broker.get_stats("forex"),
                }
                await send_daily_report(stats)
                self._last_daily_report = now.date()
            await asyncio.sleep(300)

    # ══════════════════════════════════════════════════════════════
    #  Signal evaluation — Crypto
    # ══════════════════════════════════════════════════════════════

    async def _eval_crypto(self, symbol: str):
        if len(self.broker.open_for_symbol(symbol)) >= 2:
            return

        # CR-S1: Funding rate arbitrage
        funding = await self.binance.get_funding_rate(symbol)
        hist    = await self.binance.get_funding_history(symbol, 90)
        if not hist.empty:
            mu = hist.mean()
            sigma = hist.std()
            z = (funding - mu) / (sigma + 1e-9)

            price = await self.binance.get_price(symbol)
            if price is None:
                return

            candles_1h = await self.binance.get_candles(symbol, "1h", 50)
            if candles_1h.empty:
                return
            atr_val = atr(candles_1h["high"], candles_1h["low"], candles_1h["close"], 14).iloc[-1]

            # Long funding → short perp (collect funding)
            if funding >= CR.funding_long_threshold and z >= CR.funding_z_score_entry:
                sl = price + atr_val * 2.0
                tp = price - atr_val * 4.0
                await self._fire_signal(Signal(
                    brain="crypto", strategy="CR-S1", market="crypto",
                    symbol=symbol, direction="SELL",
                    confidence=min(z / 4.0, 0.95),
                    entry_price=price, sl=sl, tp=tp,
                    metadata={"funding": funding, "z": round(z, 2)},
                ))

            # Negative funding → long perp
            elif funding <= CR.funding_short_threshold and z <= -CR.funding_z_score_entry:
                sl = price - atr_val * 2.0
                tp = price + atr_val * 4.0
                await self._fire_signal(Signal(
                    brain="crypto", strategy="CR-S1", market="crypto",
                    symbol=symbol, direction="BUY",
                    confidence=min(abs(z) / 4.0, 0.95),
                    entry_price=price, sl=sl, tp=tp,
                    metadata={"funding": funding, "z": round(z, 2)},
                ))

        # CR-S2: Momentum
        candles_5m = await self.binance.get_candles(symbol, "5m", 100)
        if not candles_5m.empty:
            price = float(candles_5m["close"].iloc[-1])
            ema9_v  = ema(candles_5m["close"], 9).iloc[-1]
            ema21_v = ema(candles_5m["close"], 21).iloc[-1]
            rsi_v   = rsi(candles_5m["close"], 14).iloc[-1]
            atr_v   = atr(candles_5m["high"], candles_5m["low"], candles_5m["close"], 14).iloc[-1]

            # Dynamic confidence: strong EMA separation + RSI well inside momentum zone
            _ema_q = min(abs(ema9_v - ema21_v) / max(atr_v, 1e-9), 1.0)
            if ema9_v > ema21_v and rsi_v > 55 and rsi_v < 75:
                _rsi_q    = min((rsi_v - 55) / 20.0, 1.0)
                _cr2_conf = round(min(0.97, 0.75 + 0.22 * (_ema_q * 0.5 + _rsi_q * 0.5)), 3)
                sl = price - atr_v * CR.momentum_sl_atr_mult
                tp = price + atr_v * CR.momentum_tp_atr_mult
                await self._fire_signal(Signal(
                    brain="crypto", strategy="CR-S2", market="crypto",
                    symbol=symbol, direction="BUY",
                    confidence=_cr2_conf, entry_price=price, sl=sl, tp=tp,
                    metadata={"ema_q": round(_ema_q,3), "rsi": round(rsi_v,1)},
                ))
            elif ema9_v < ema21_v and rsi_v < 45 and rsi_v > 25:
                _rsi_q    = min((45 - rsi_v) / 20.0, 1.0)
                _cr2_conf = round(min(0.97, 0.75 + 0.22 * (_ema_q * 0.5 + _rsi_q * 0.5)), 3)
                sl = price + atr_v * CR.momentum_sl_atr_mult
                tp = price - atr_v * CR.momentum_tp_atr_mult
                await self._fire_signal(Signal(
                    brain="crypto", strategy="CR-S2", market="crypto",
                    symbol=symbol, direction="SELL",
                    confidence=_cr2_conf, entry_price=price, sl=sl, tp=tp,
                    metadata={"ema_q": round(_ema_q,3), "rsi": round(rsi_v,1)},
                ))

    # ══════════════════════════════════════════════════════════════
    #  Signal evaluation — Crash/Boom (V4 + V21 + Sniper)
    # ══════════════════════════════════════════════════════════════

    async def _eval_crash_boom(self, symbol: str, avg_ticks: int, direction: str):
        if len(self.broker.open_for_symbol(symbol)) >= 2:
            return

        price = self.deriv.get_price(symbol)
        if price is None:
            return

        ticks_since = self.deriv.get_ticks_since_spike(symbol)

        # Build M1 candles from tick buffer
        candles_m1 = await self.deriv.get_candles_from_memory(symbol, 60, 200)
        if candles_m1.empty or len(candles_m1) < 20:
            return

        close = candles_m1["close"]
        high  = candles_m1["high"]
        low   = candles_m1["low"]

        atr_v    = atr(high, low, close, 14).iloc[-1]
        rsi14    = rsi(close, 14)
        rsi_v    = rsi14.iloc[-1]
        bbw      = bb_width(close, 20)
        atr_s    = atr(high, low, close, 14)
        comp     = compression_ratio(atr_s, bbw).iloc[-1]
        ema9_v   = ema(close, 9).iloc[-1]
        ema21_v  = ema(close, 21).iloc[-1]

        # Tick stream features
        tick_changes = close.diff().dropna()
        tssl_v = tssl(tick_changes, 50)

        # H1 candles (built from M1 by aggregation)
        # Approximate from M1: take last 60 M1 bars
        if len(candles_m1) >= 60:
            h1_slice  = candles_m1.tail(60)
            h1_rsi    = rsi(h1_slice["close"], 14).iloc[-1]
            h1_streak = 0
            bull = h1_slice["close"] > h1_slice["open"]
            for v in reversed(bull.values):
                if v == bull.values[-1]:
                    h1_streak += 1
                else:
                    break
        else:
            h1_rsi    = rsi_v
            h1_streak = 0

        # ── V4 Brain: CB-S1 Apex Compression Spike Hunter ────────
        geo_prob = 1 - (1 - 1/avg_ticks) ** min(ticks_since, avg_ticks * 5)
        v4_lights = [
            geo_prob    >= CB.s1_geometric_prob_threshold,
            comp        <= CB.s1_compression_threshold,
            abs(tssl_v) >= CB.s1_tssl_threshold,
            rsi_v       < 40 if direction == "up" else rsi_v > 60,
        ]

        if all(v4_lights):
            trade_dir = "BUY" if direction == "up" else "SELL"
            sl = price - atr_v * CB.s1_sl_atr_mult if trade_dir == "BUY" else price + atr_v * CB.s1_sl_atr_mult
            tp = price + atr_v * CB.s1_tp_atr_mult if trade_dir == "BUY" else price - atr_v * CB.s1_tp_atr_mult
            await self._fire_signal(Signal(
                brain="v4", strategy="CB-S1", market="crash_boom",
                symbol=symbol, direction=trade_dir,
                confidence=geo_prob, entry_price=price, sl=sl, tp=tp,
                metadata={"geo_prob": round(geo_prob, 3), "compression": round(comp, 3), "tssl": round(tssl_v, 3)},
            ))

        # ── V4 Brain: CB-S2 Compression Trend Rider ──────────────
        if comp < CB.s2_compression_max:
            # Dynamic confidence: tighter compression + wider EMA spread = stronger signal
            _comp_q  = (CB.s2_compression_max - comp) / CB.s2_compression_max
            _ema_q   = min(abs(ema9_v - ema21_v) / max(atr_v, 1e-9), 1.0)
            _s2_conf = round(min(0.97, 0.70 + 0.27 * (_comp_q * 0.55 + _ema_q * 0.45)), 3)
            if ema9_v > ema21_v and direction == "up":
                sl = price - atr_v * CB.s2_sl_atr_mult
                tp = price + atr_v * CB.s2_tp_atr_mult
                await self._fire_signal(Signal(
                    brain="v4", strategy="CB-S2", market="crash_boom",
                    symbol=symbol, direction="BUY",
                    confidence=_s2_conf, entry_price=price, sl=sl, tp=tp,
                ))
            elif ema9_v < ema21_v and direction == "down":
                sl = price + atr_v * CB.s2_sl_atr_mult
                tp = price - atr_v * CB.s2_tp_atr_mult
                await self._fire_signal(Signal(
                    brain="v4", strategy="CB-S2", market="crash_boom",
                    symbol=symbol, direction="SELL",
                    confidence=_s2_conf, entry_price=price, sl=sl, tp=tp,
                ))

        # ── V21 Brain: CB-S3 Kingpin Divergence + Reversal ───────
        div = rsi_divergence(close, rsi14, 20).iloc[-1]
        h4_extreme = h1_rsi < CB.s3_h4_rsi_extreme_low or h1_rsi > CB.s3_h4_rsi_extreme_high

        if div != 0 and h4_extreme:
            trade_dir = "BUY" if div > 0 else "SELL"
            # Dynamic confidence: deeper RSI extreme = stronger reversal conviction
            _rsi_depth = (
                (CB.s3_h4_rsi_extreme_low - h1_rsi) / max(CB.s3_h4_rsi_extreme_low, 1)
                if h1_rsi < 50
                else (h1_rsi - CB.s3_h4_rsi_extreme_high) / max(100 - CB.s3_h4_rsi_extreme_high, 1)
            )
            _rsi_depth = max(0.0, min(1.0, _rsi_depth))
            _s3_conf   = round(min(0.97, 0.78 + 0.18 * _rsi_depth), 3)
            # Scalper lot
            sl_pts = CB.s3_scalper_sl_pts
            sl  = price - sl_pts if trade_dir == "BUY" else price + sl_pts
            tp  = price + CB.s3_scalper_tp_pts if trade_dir == "BUY" else price - CB.s3_scalper_tp_pts
            await self._fire_signal(Signal(
                brain="v21", strategy="CB-S3", market="crash_boom",
                symbol=symbol, direction=trade_dir,
                confidence=_s3_conf, entry_price=price, sl=sl, tp=tp,
                lot="scalper",
                metadata={"divergence": div, "h1_rsi": round(h1_rsi, 1), "conf": _s3_conf},
            ))
            # Runner lot
            tp2 = price + CB.s3_runner_trail_pts if trade_dir == "BUY" else price - CB.s3_runner_trail_pts
            await self._fire_signal(Signal(
                brain="v21", strategy="CB-S3", market="crash_boom",
                symbol=symbol, direction=trade_dir,
                confidence=_s3_conf, entry_price=price, sl=sl, tp=tp2,
                trailing_pts=CB.s3_runner_trail_pts,
                lot="runner",
                metadata={"divergence": div, "conf": _s3_conf},
            ))

        # ── Sniper Brain: CB-S4 M5 Exhaustion ────────────────────
        m5_rsi_v = rsi_v    # using M1 RSI as M5 proxy
        h4_extreme_sniper = h1_rsi < CB.s4_h4_rsi_low or h1_rsi > CB.s4_h4_rsi_high
        h1_streak_ok      = h1_streak >= CB.s4_h1_streak_min
        m5_exhaust        = (
            (m5_rsi_v < CB.s4_m5_rsi_exhaust_low  and direction == "up")  or
            (m5_rsi_v > CB.s4_m5_rsi_exhaust_high and direction == "down")
        )

        if h4_extreme_sniper and h1_streak_ok and m5_exhaust:
            trade_dir = "BUY" if direction == "up" else "SELL"
            sl = price - CB.s4_trail_pts * 2 if trade_dir == "BUY" else price + CB.s4_trail_pts * 2
            tp = price + CB.s4_trail_pts * 5 if trade_dir == "BUY" else price - CB.s4_trail_pts * 5
            await self._fire_signal(Signal(
                brain="sniper", strategy="CB-S4", market="crash_boom",
                symbol=symbol, direction=trade_dir,
                confidence=0.95, entry_price=price, sl=sl, tp=tp,
                trailing_pts=CB.s4_trail_pts,
                metadata={"h1_streak": h1_streak, "m5_rsi": round(m5_rsi_v, 1)},
            ))

    # ══════════════════════════════════════════════════════════════
    #  Signal evaluation — Forex
    # ══════════════════════════════════════════════════════════════

    async def _eval_forex(self, pair: str):
        if len(self.broker.open_for_symbol(pair)) >= 2:
            return

        news_ok   = not OANDAFeed.news_blackout()
        sessions  = OANDAFeed.current_sessions()
        bid, ask  = await self.oanda.get_price(pair)
        mid        = (bid + ask) / 2

        candles   = await self.oanda.get_candles(pair, "M5", 200)
        if candles.empty or len(candles) < 50:
            return

        close = candles["close"]
        high  = candles["high"]
        low   = candles["low"]
        atr_v  = atr(high, low, close, 14).iloc[-1]
        rsi14  = rsi(close, 14)
        rsi_v  = rsi14.iloc[-1]

        # ── FX-S1: London Breakout ────────────────────────────────
        if "London" in sessions and news_ok:
            asian_end   = candles[candles.index.hour < FX.london_open]
            asian_range = asian_end.tail(FX.s1_asian_range_hours * 12)  # 12 M5 bars per hour
            if not asian_range.empty:
                rng_high = asian_range["high"].max()
                rng_low  = asian_range["low"].min()
                buf      = atr_v * FX.s1_breakout_atr_mult

                if close.iloc[-1] > rng_high + buf:
                    # Dynamic: how far price broke beyond the buffer relative to ATR
                    _excess   = close.iloc[-1] - (rng_high + buf)
                    _s1_conf  = round(min(0.97, 0.75 + min(_excess / max(atr_v, 1e-9), 0.80) * 0.25), 3)
                    sl = mid - atr_v * FX.s1_sl_atr_mult
                    tp = mid + atr_v * FX.s1_tp_atr_mult
                    await self._fire_signal(Signal(
                        brain="fx", strategy="FX-S1", market="forex",
                        symbol=pair, direction="BUY",
                        confidence=_s1_conf, entry_price=ask, sl=sl, tp=tp,
                        session="London", metadata={"breakout_atr_pct": round(_excess/max(atr_v,1e-9),2)},
                    ))
                elif close.iloc[-1] < rng_low - buf:
                    _excess   = (rng_low - buf) - close.iloc[-1]
                    _s1_conf  = round(min(0.97, 0.75 + min(_excess / max(atr_v, 1e-9), 0.80) * 0.25), 3)
                    sl = mid + atr_v * FX.s1_sl_atr_mult
                    tp = mid - atr_v * FX.s1_tp_atr_mult
                    await self._fire_signal(Signal(
                        brain="fx", strategy="FX-S1", market="forex",
                        symbol=pair, direction="SELL",
                        confidence=_s1_conf, entry_price=bid, sl=sl, tp=tp,
                        session="London", metadata={"breakout_atr_pct": round(_excess/max(atr_v,1e-9),2)},
                    ))

        # ── FX-S2: London/NY Overlap RSI Divergence ───────────────
        if OANDAFeed.is_overlap() and news_ok:
            div = rsi_divergence(close, rsi14, FX.s2_divergence_lookback).iloc[-1]
            if div != 0:
                trade_dir = "BUY" if div > 0 else "SELL"
                ep  = ask if trade_dir == "BUY" else bid
                sl  = mid - atr_v * FX.s2_sl_atr_mult if trade_dir == "BUY" else mid + atr_v * FX.s2_sl_atr_mult
                tp  = mid + FX.s2_scalper_tp_pips * 0.0001 if trade_dir == "BUY" else mid - FX.s2_scalper_tp_pips * 0.0001
                tp2 = mid + FX.s2_runner_trail_pips * 0.0001 if trade_dir == "BUY" else mid - FX.s2_runner_trail_pips * 0.0001
                # Dynamic: deeper RSI extreme = higher divergence conviction
                _rsi_ext  = abs(rsi_v - 50) / 50.0  # 0 at RSI=50, 1 at RSI=0/100
                _s2_conf  = round(min(0.97, 0.72 + _rsi_ext * 0.25), 3)
                # Scalper
                await self._fire_signal(Signal(
                    brain="fx", strategy="FX-S2", market="forex",
                    symbol=pair, direction=trade_dir,
                    confidence=_s2_conf, entry_price=ep, sl=sl, tp=tp,
                    lot="scalper", session="Overlap", metadata={"rsi": round(rsi_v,1)},
                ))
                # Runner
                await self._fire_signal(Signal(
                    brain="fx", strategy="FX-S2", market="forex",
                    symbol=pair, direction=trade_dir,
                    confidence=_s2_conf, entry_price=ep, sl=sl, tp=tp2,
                    trailing_pts=FX.s2_runner_trail_pips * 0.0001,
                    lot="runner", session="Overlap", metadata={"rsi": round(rsi_v,1)},
                ))

        # ── FX-S3: News Compression Breakout ─────────────────────
        mins_to_news = OANDAFeed.minutes_to_next_news()
        if mins_to_news and FX.s3_pre_news_min_low <= mins_to_news <= FX.s3_pre_news_min_high:
            bbw_v = bb_width(close, 20).iloc[-1]
            bbw_recent = bb_width(close, 5).iloc[-1]
            if bbw_recent < bbw_v * FX.s3_squeeze_ratio:
                # Dynamic: how much tighter than the threshold — marginal squeezes skipped
                _squeeze_depth = 1.0 - (bbw_recent / max(bbw_v * FX.s3_squeeze_ratio, 1e-9))
                _s3_conf = round(min(0.97, 0.82 + 0.15 * max(0.0, _squeeze_depth)), 3)
                trade_dir = "BUY" if rsi_v < 50 else "SELL"
                ep = ask if trade_dir == "BUY" else bid
                sl = mid - atr_v * FX.s3_sl_atr_mult if trade_dir == "BUY" else mid + atr_v * FX.s3_sl_atr_mult
                tp = mid + atr_v * FX.s3_tp_atr_mult if trade_dir == "BUY" else mid - atr_v * FX.s3_tp_atr_mult
                await self._fire_signal(Signal(
                    brain="fx", strategy="FX-S3", market="forex",
                    symbol=pair, direction=trade_dir,
                    confidence=_s3_conf, entry_price=ep, sl=sl, tp=tp,
                    session="News", metadata={"mins_to_news": mins_to_news, "squeeze": round(_squeeze_depth,3)},
                ))

        # ── FX-S4: Asian Session Mean Reversion ───────────────────
        if OANDAFeed.is_asian() and pair in ["EUR_JPY","GBP_JPY"] and news_ok:
            h_exp = hurst(close.values[-50:]) if len(close) >= 50 else 0.5
            if h_exp < FX.s4_hurst_threshold:
                # Dynamic: deeper mean-reversion (lower Hurst) + more extreme RSI
                _hurst_q = max(0.0, (FX.s4_hurst_threshold - h_exp) / max(FX.s4_hurst_threshold, 1e-9))
                if rsi_v < FX.s4_rsi_low:
                    _rsi_q   = max(0.0, (FX.s4_rsi_low - rsi_v) / max(FX.s4_rsi_low, 1e-9))
                    _s4_conf = round(min(0.97, 0.75 + 0.22 * (_hurst_q * 0.5 + _rsi_q * 0.5)), 3)
                    sl = mid - atr_v * FX.s4_sl_atr_mult
                    tp = mid + atr_v * FX.s4_tp_atr_mult
                    await self._fire_signal(Signal(
                        brain="fx", strategy="FX-S4", market="forex",
                        symbol=pair, direction="BUY",
                        confidence=_s4_conf, entry_price=ask, sl=sl, tp=tp,
                        session="Asian", metadata={"hurst": round(h_exp,3), "rsi": round(rsi_v,1)},
                    ))
                elif rsi_v > FX.s4_rsi_high:
                    _rsi_q   = max(0.0, (rsi_v - FX.s4_rsi_high) / max(100 - FX.s4_rsi_high, 1e-9))
                    _s4_conf = round(min(0.97, 0.75 + 0.22 * (_hurst_q * 0.5 + _rsi_q * 0.5)), 3)
                    sl = mid + atr_v * FX.s4_sl_atr_mult
                    tp = mid - atr_v * FX.s4_tp_atr_mult
                    await self._fire_signal(Signal(
                        brain="fx", strategy="FX-S4", market="forex",
                        symbol=pair, direction="SELL",
                        confidence=_s4_conf, entry_price=bid, sl=sl, tp=tp,
                        session="Asian", metadata={"hurst": round(h_exp,3), "rsi": round(rsi_v,1)},
                    ))

    # ══════════════════════════════════════════════════════════════
    #  Fire signal → consensus → broker
    # ══════════════════════════════════════════════════════════════

    async def _fire_signal(self, signal: Signal):
        # Skip if the same strategy+lot is already open on this symbol
        if any(
            o.strategy == signal.strategy and o.lot == signal.lot
            for o in self.broker.open_for_symbol(signal.symbol)
        ):
            return

        approved = self.consensus.submit(signal)
        if not approved:
            return

        if not self.risk.passes_rr_filter(
            signal.entry_price, signal.sl, signal.tp, signal.direction
        ):
            return

        qty, notional = self.risk.size_position(
            signal.entry_price, signal.sl, signal.market
        )
        if qty <= 0:
            return

        order = Order(
            market      = signal.market,
            strategy    = signal.strategy,
            symbol      = signal.symbol,
            side        = signal.direction,
            quantity    = qty,
            notional_usd= notional,
            entry_price = signal.entry_price,
            sl          = signal.sl,
            tp          = signal.tp,
            tp2         = signal.tp2,
            trailing_pts= signal.trailing_pts,
            lot         = signal.lot,
            session     = signal.session,
            metadata    = signal.metadata,
        )

        order = await self.broker.place(order)

        if order.status == "OPEN":
            self.consensus.register_open(signal.symbol, signal.strategy)
            await alert_trade_open(order)

        elif order.status == "CANCELLED" and "circuit" in order.exit_reason.lower():
            await alert_circuit_breaker(order.exit_reason, self.risk.state.balance)

    # ══════════════════════════════════════════════════════════════
    #  Performance monitoring
    # ══════════════════════════════════════════════════════════════

    def _update_strategy_stats(self, order):
        s = self._strategy_stats.setdefault(order.strategy, {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        if order.pnl_usd > 0:
            s["wins"] += 1
        s["pnl"] += order.pnl_usd

    async def _check_wr_divergence(self, strategy: str):
        s = self._strategy_stats.get(strategy, {})
        trades = s.get("trades", 0)
        if trades < MON.min_trades_for_alert:
            return
        live_wr = s["wins"] / trades * 100
        bt_wr   = BACKTEST_WR.get(strategy, 0)
        if bt_wr > 0 and (bt_wr - live_wr) > MON.wr_divergence_alert_pp:
            await alert_divergence(strategy, live_wr, bt_wr, trades)

    def print_status(self):
        risk_sum = self.risk.summary()
        print(f"\n{'─'*55}")
        print(f"  Balance: ${risk_sum['balance']:,.2f}  "
              f"Return: {risk_sum['total_return_pct']:+.1f}%  "
              f"DD: {risk_sum['drawdown_pct']:.1f}%")
        print(f"  Open positions: {risk_sum['open_positions']}  "
              f"Halted: {risk_sum['halted']}")
        for market in ["crypto","crash_boom","forex"]:
            st = self.broker.get_stats(market)
            if st["trades"] > 0:
                print(f"  {market.upper():<12} | "
                      f"trades={st['trades']} | WR={st['wr']:.1f}% | PnL=${st['total_pnl']:+.2f}")
        print(f"{'─'*55}\n")


# ══════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Force paper mode")
    parser.add_argument("--live",  action="store_true", help="Force live mode")
    args = parser.parse_args()

    mode = "live" if args.live else "paper" if args.paper else MODE

    if mode == "live":
        print("\n" + "!"*55)
        print("  WARNING: LIVE MODE — REAL MONEY AT RISK")
        print("  Ensure all API keys and risk params are correct.")
        confirm = input("  Type YES to continue: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(0)
        print("!"*55 + "\n")

    bot = TradingBot(mode=mode)
    inject_bot(bot)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
        bot.print_status()
