"""
data/binance_feed.py
Live Binance data: WebSocket tick stream + REST candles.
Writes ticks to Redis, candles to Postgres.
"""

import asyncio, json, logging, time
from datetime import datetime, timezone
import aiohttp
import pandas as pd
import numpy as np
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET, CRYPTO_SYMBOLS

log = logging.getLogger("BinanceFeed")

WS_BASE  = "wss://stream.testnet.binance.vision/ws" if BINANCE_TESTNET else "wss://stream.binance.com:9443/ws"
REST_BASE= "https://testnet.binance.vision/api/v3" if BINANCE_TESTNET else "https://api.binance.com/api/v3"


class BinanceFeed:
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._prices: dict[str, float] = {}
        self._orderbooks: dict[str, dict] = {}
        self._running = False

    # ── Live price (REST fallback) ─────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        """symbol e.g. 'BTCUSDT'"""
        sym = symbol.replace("/", "").replace(":USDT", "").upper()
        if sym in self._prices:
            return self._prices[sym]
        return await self._fetch_rest_price(sym)

    async def _fetch_rest_price(self, symbol: str) -> float:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REST_BASE}/ticker/price?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                return float(data["price"])

    # ── Candle data ────────────────────────────────────────────────

    async def get_candles(self, symbol: str, interval: str = "5m", limit: int = 200) -> pd.DataFrame:
        """interval: 1m 5m 15m 1h 4h"""
        sym = symbol.replace("/", "").replace(":USDT", "").upper()
        url = f"{REST_BASE}/klines?symbol={sym}&interval={interval}&limit={limit}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()

        if not data or not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        return df.set_index("open_time")[["open","high","low","close","volume"]]

    # ── Funding rate ───────────────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> float:
        """Fetch current funding rate for perp."""
        sym = symbol.replace("/", "").replace(":USDT", "").upper()
        base = "https://testnet.binancefuture.com/fapi/v1" if BINANCE_TESTNET else "https://fapi.binance.com/fapi/v1"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{base}/premiumIndex?symbol={sym}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    return float(data.get("lastFundingRate", 0))
        except Exception as e:
            log.warning(f"Funding rate fetch failed ({sym}): {e}")
            return 0.0

    async def get_funding_history(self, symbol: str, limit: int = 90) -> pd.Series:
        sym = symbol.replace("/", "").replace(":USDT", "").upper()
        base = "https://testnet.binancefuture.com/fapi/v1" if BINANCE_TESTNET else "https://fapi.binance.com/fapi/v1"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{base}/fundingRate?symbol={sym}&limit={limit}", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
            df = pd.DataFrame(data)
            df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
            df["fundingRate"] = df["fundingRate"].astype(float)
            return df.set_index("fundingTime")["fundingRate"]
        except Exception as e:
            log.warning(f"Funding history failed ({sym}): {e}")
            return pd.Series(dtype=float)

    # ── WebSocket stream ──────────────────────────────────────────

    async def stream(self, symbols: list[str]):
        """Stream live trade ticks for multiple symbols."""
        streams = [f"{s.replace('/','').replace(':USDT','').lower()}@aggTrade" for s in symbols]
        url = f"{WS_BASE}/{'/'.join(streams)}" if len(streams) == 1 else f"{WS_BASE.replace('/ws','')}/stream?streams={'/'.join(streams)}"
        self._running = True

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=30) as ws:
                        log.info(f"Binance WS connected: {len(streams)} streams")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data))
            except Exception as e:
                log.warning(f"Binance WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_message(self, data: dict):
        tick = data.get("data", data)
        if tick.get("e") != "aggTrade":
            return
        symbol = tick["s"]
        price  = float(tick["p"])
        self._prices[symbol] = price

        if self.redis:
            key = f"tick:crypto:{symbol}"
            payload = json.dumps({"price": price, "ts": tick["T"], "qty": float(tick["q"])})
            await self.redis.lpush(key, payload)
            await self.redis.ltrim(key, 0, 9999)   # keep last 10k ticks

    def stop(self):
        self._running = False
