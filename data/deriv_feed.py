"""
DerivFeed — single shared WebSocket feed for all Crash/Boom symbols.
"""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import DERIV_APP_ID, CRASH_BOOM_SYMBOLS

logger = logging.getLogger(__name__)

DERIV_WS_URL  = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
TICK_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ticks")
TICK_LOG_INTERVAL = 300   # flush to CSV every 5 minutes
# All symbols from config (just the symbol string from each tuple)
_DEFAULT_SYMBOLS = [sym for sym, *_ in CRASH_BOOM_SYMBOLS]
RECONNECT_DELAY = 5

# Spike-detection: price change must exceed this multiple of rolling avg to count
_SPIKE_MULT = 3.0
# Rolling window for average absolute change used in spike detection
_SPIKE_WINDOW = 50
# Max ticks stored per symbol in the ring buffer
_TICK_BUFFER = 10_000


class DerivFeed:
    def __init__(self, symbols: list = None):
        self.symbols = symbols or _DEFAULT_SYMBOLS
        self._callbacks: Dict[str, list] = {sym: [] for sym in self.symbols}
        self._last_prices: Dict[str, float] = {}

        # Ring buffer: deque of (epoch_float, price_float) per symbol
        self._ticks: Dict[str, deque] = {
            sym: deque(maxlen=_TICK_BUFFER) for sym in self.symbols
        }
        # Index of the last known spike tick for each symbol (position in deque)
        self._last_spike_idx: Dict[str, int] = {sym: 0 for sym in self.symbols}

        # direction lookup built from config: symbol -> "up" | "down"
        self._direction: Dict[str, str] = {
            sym: direction for sym, _, direction, *_ in CRASH_BOOM_SYMBOLS
        }

        # last epoch (float) written to CSV per symbol — avoids re-writing duplicates
        self._last_saved_epoch: Dict[str, float] = {sym: 0.0 for sym in self.symbols}

        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register_callback(self, symbol: str, callback: Callable):
        if symbol not in self._callbacks:
            self._callbacks[symbol] = []
        self._callbacks[symbol].append(callback)
        logger.info(f"DerivFeed: registered callback for {symbol}")

    async def stream(self):
        self._running = True
        logger.info(f"DerivFeed: starting stream loop for {len(self.symbols)} symbols")
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                logger.info("DerivFeed: stream cancelled")
                break
            except Exception as e:
                logger.error(f"DerivFeed: connection error — {e}")
                if self._running:
                    logger.info(f"DerivFeed: reconnecting in {RECONNECT_DELAY}s…")
                    await asyncio.sleep(RECONNECT_DELAY)
        logger.info("DerivFeed: stream loop exited")

    async def _connect_and_stream(self):
        logger.info(f"DerivFeed: connecting to {DERIV_WS_URL}")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(DERIV_WS_URL, heartbeat=30) as ws:
                logger.info("DerivFeed: connected — subscribing to symbols")
                for i, sym in enumerate(self.symbols):
                    await ws.send_json({
                        "ticks": sym,
                        "subscribe": 1,
                        "req_id": i + 1,
                    })
                logger.info(f"DerivFeed: streaming {self.symbols}")
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        logger.warning("DerivFeed: WS closed/error — reconnecting")
                        break

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if data.get("msg_type") == "tick":
            tick = data.get("tick", {})
            symbol = tick.get("symbol")
            quote = tick.get("quote")
            epoch = tick.get("epoch") or time.time()
            if symbol and quote is not None:
                price = float(quote)
                self._last_prices[symbol] = price
                buf = self._ticks.get(symbol)
                if buf is not None:
                    buf.append((float(epoch), price))
                    self._update_spike(symbol)
                logger.debug(f"DerivFeed: tick {symbol} = {price}")
                for cb in self._callbacks.get(symbol, []):
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            await cb(tick)
                        else:
                            cb(tick)
                    except Exception as e:
                        logger.error(f"DerivFeed: callback error for {symbol} — {e}")

        elif data.get("msg_type") == "error":
            err = data.get("error", {})
            logger.error(f"DerivFeed: server error — {err.get('message', data)}")

    # ── Spike detection ───────────────────────────────────────────────

    def _update_spike(self, symbol: str):
        buf = self._ticks[symbol]
        n = len(buf)
        if n < 2:
            return
        prices = [p for _, p in buf]
        last_chg = abs(prices[-1] - prices[-2])
        window = prices[-min(n, _SPIKE_WINDOW):]
        diffs = [abs(window[i] - window[i-1]) for i in range(1, len(window))]
        avg_chg = float(np.mean(diffs)) if diffs else 0.0
        if avg_chg == 0:
            return
        direction = self._direction.get(symbol, "up")
        price_chg = prices[-1] - prices[-2]
        is_boom_spike = direction == "up" and price_chg > _SPIKE_MULT * avg_chg
        is_crash_spike = direction == "down" and price_chg < -_SPIKE_MULT * avg_chg
        if is_boom_spike or is_crash_spike:
            self._last_spike_idx[symbol] = n - 1

    def get_ticks_since_spike(self, symbol: str) -> int:
        buf = self._ticks.get(symbol)
        if not buf:
            return 0
        n = len(buf)
        last_spike = self._last_spike_idx.get(symbol, 0)
        return max(0, n - 1 - last_spike)

    # ── Candle builder from tick buffer ───────────────────────────────

    async def get_candles_from_memory(
        self, symbol: str, seconds_per_bar: int = 60, num_bars: int = 200
    ) -> pd.DataFrame:
        buf = self._ticks.get(symbol)
        if not buf or len(buf) < 2:
            return pd.DataFrame()

        ticks: List[Tuple[float, float]] = list(buf)
        if not ticks:
            return pd.DataFrame()

        # Group ticks into time buckets
        bar_size = float(seconds_per_bar)
        earliest = ticks[0][0]
        latest = ticks[-1][0]

        # Build bucket boundaries
        first_bucket = int(earliest // bar_size) * bar_size
        last_bucket = int(latest // bar_size) * bar_size
        buckets: Dict[float, List[float]] = {}
        t = first_bucket
        while t <= last_bucket:
            buckets[t] = []
            t += bar_size

        for epoch, price in ticks:
            bucket = int(epoch // bar_size) * bar_size
            if bucket in buckets:
                buckets[bucket].append(price)

        rows = []
        for bucket_ts in sorted(buckets.keys()):
            prices = buckets[bucket_ts]
            if not prices:
                continue
            rows.append({
                "open":   prices[0],
                "high":   max(prices),
                "low":    min(prices),
                "close":  prices[-1],
                "volume": float(len(prices)),
            })

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # Return last num_bars completed bars (exclude the last partial bar)
        if len(df) > 1:
            df = df.iloc[:-1]   # drop current (partial) bar
        return df.tail(num_bars).reset_index(drop=True)

    # ── Price / state accessors ───────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[float]:
        return self._last_prices.get(symbol)

    def stop(self):
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Live tick persistence ─────────────────────────────────────────

    async def tick_logger(self, interval: int = TICK_LOG_INTERVAL):
        """
        Coroutine that runs alongside the stream loop.
        Every `interval` seconds it appends any new ticks received since
        the last flush to data/ticks/<SYMBOL>_ticks.csv.
        These CSVs are the same format DataLoader.load_csv() expects,
        so models/retrain_real.py --skip-fetch can retrain on them directly.
        """
        os.makedirs(TICK_DATA_DIR, exist_ok=True)
        logger.info(f"TickLogger: will flush every {interval}s → {TICK_DATA_DIR}")

        while self._running:
            await asyncio.sleep(interval)
            total_new = 0
            for symbol in self.symbols:
                buf = self._ticks.get(symbol)
                if not buf:
                    continue

                # Snapshot deque and filter to ticks we haven't saved yet
                ticks = list(buf)
                cutoff = self._last_saved_epoch.get(symbol, 0.0)
                new_ticks = [(ep, pr) for ep, pr in ticks if ep > cutoff]
                if not new_ticks:
                    continue

                csv_path = os.path.join(TICK_DATA_DIR, f"{symbol}_ticks.csv")
                write_header = not os.path.exists(csv_path)

                rows = pd.DataFrame({
                    "timestamp": [
                        datetime.fromtimestamp(ep, tz=timezone.utc)
                              .strftime("%Y-%m-%d %H:%M:%S")
                        for ep, _ in new_ticks
                    ],
                    "price": [pr for _, pr in new_ticks],
                })
                rows.to_csv(csv_path, mode="a", header=write_header, index=False)

                self._last_saved_epoch[symbol] = new_ticks[-1][0]
                total_new += len(new_ticks)

            if total_new:
                logger.info(f"TickLogger: flushed {total_new} new ticks across {len(self.symbols)} symbols")
