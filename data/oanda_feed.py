"""
data/oanda_feed.py - Alpha Vantage Forex feed (OANDA blocked for Nigeria)
"""
import asyncio, logging, time, os, sys
from datetime import datetime, timezone
import aiohttp
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

log = logging.getLogger("OANDAFeed")
AV_BASE = "https://www.alphavantage.co/query"
PAIR_MAP = {
    "EUR_USD": ("EUR","USD"), "GBP_USD": ("GBP","USD"),
    "USD_JPY": ("USD","JPY"), "EUR_JPY": ("EUR","JPY"),
    "GBP_JPY": ("GBP","JPY"), "XAU_USD": ("XAU","USD"),
}
STUB = {
    "EUR_USD":(1.0850,1.0851),"GBP_USD":(1.2700,1.2702),
    "USD_JPY":(149.50,149.52),"XAU_USD":(2320.0,2320.5),
    "EUR_JPY":(162.20,162.22),"GBP_JPY":(189.80,189.83),
}

class OANDAFeed:
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._cache = {}
        self._candle_cache = {}
        self._last_fetch = {}
        self._api_key = os.getenv("ALPHA_VANTAGE_KEY","")
        self._calls = 0
        self._reset_time = time.time()

    def _rate_ok(self):
        if time.time() - self._reset_time > 60:
            self._calls = 0
            self._reset_time = time.time()
        return self._calls < 4

    async def get_price(self, instrument):
        if not self._api_key:
            return STUB.get(instrument,(1.0,1.0002))
        cached = self._cache.get(instrument)
        if cached and time.time()-cached[2] < 30:
            return cached[0], cached[1]
        if not self._rate_ok():
            return STUB.get(instrument,(1.0,1.0002))
        pair = PAIR_MAP.get(instrument)
        if not pair:
            return STUB.get(instrument,(1.0,1.0002))
        try:
            url = f"{AV_BASE}?function=CURRENCY_EXCHANGE_RATE&from_currency={pair[0]}&to_currency={pair[1]}&apikey={self._api_key}"
            async with aiohttp.ClientSession() as s:
                async with s.get(url,timeout=aiohttp.ClientTimeout(total=8)) as r:
                    data = await r.json()
            rate = float(data.get("Realtime Currency Exchange Rate",{}).get("5. Exchange Rate",0))
            if rate == 0:
                return STUB.get(instrument,(1.0,1.0002))
            bid,ask = rate*0.9999, rate*1.0001
            self._cache[instrument] = (bid,ask,time.time())
            self._calls += 1
            return bid, ask
        except Exception as e:
            log.warning(f"AV price failed ({instrument}): {e}")
            return STUB.get(instrument,(1.0,1.0002))

    async def get_mid(self, instrument):
        b,a = await self.get_price(instrument)
        return (b+a)/2

    async def get_candles(self, instrument, granularity="M5", count=200):
        if not self._api_key:
            return pd.DataFrame()
        key = f"{instrument}_{granularity}"
        if time.time()-self._last_fetch.get(key,0) < 300 and key in self._candle_cache:
            return self._candle_cache[key]
        if not self._rate_ok():
            return self._candle_cache.get(key, pd.DataFrame())
        pair = PAIR_MAP.get(instrument)
        if not pair:
            return pd.DataFrame()
        imap = {"M5":"5min","M15":"15min","H1":"60min","H4":"60min","D":"daily"}
        av_int = imap.get(granularity,"5min")
        try:
            if av_int == "daily":
                url = f"{AV_BASE}?function=FX_DAILY&from_symbol={pair[0]}&to_symbol={pair[1]}&outputsize=compact&apikey={self._api_key}"
                tkey = "Time Series FX (Daily)"
            else:
                url = f"{AV_BASE}?function=FX_INTRADAY&from_symbol={pair[0]}&to_symbol={pair[1]}&interval={av_int}&outputsize=compact&apikey={self._api_key}"
                tkey = f"Time Series FX ({av_int})"
            async with aiohttp.ClientSession() as s:
                async with s.get(url,timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
            self._calls += 1
            series = data.get(tkey,{})
            if not series:
                return pd.DataFrame()
            rows = [{"time":pd.Timestamp(ts,tz="UTC"),"open":float(v["1. open"]),"high":float(v["2. high"]),"low":float(v["3. low"]),"close":float(v["4. close"]),"volume":0} for ts,v in series.items()]
            df = pd.DataFrame(rows).set_index("time").sort_index().tail(count)
            self._candle_cache[key] = df
            self._last_fetch[key] = time.time()
            return df
        except Exception as e:
            log.warning(f"AV candles failed ({instrument}): {e}")
            return self._candle_cache.get(key, pd.DataFrame())

    async def get_multi_tf(self, instrument):
        m5 = await self.get_candles(instrument,"M5",200)
        return {"M5":m5,"H1":pd.DataFrame(),"H4":pd.DataFrame()}

    async def get_balance(self):
        return 10000.0

    @staticmethod
    def current_sessions(dt=None):
        dt = dt or datetime.now(timezone.utc)
        h = dt.hour
        active = []
        for name,(s,e) in {"Sydney":(21,6),"Tokyo":(0,9),"London":(7,16),"NewYork":(13,22)}.items():
            if s<e:
                if s<=h<e: active.append(name)
            else:
                if h>=s or h<e: active.append(name)
        return active

    @staticmethod
    def is_london_open(dt=None):
        return "London" in OANDAFeed.current_sessions(dt)

    @staticmethod
    def is_overlap(dt=None):
        s = OANDAFeed.current_sessions(dt)
        return "London" in s and "NewYork" in s

    @staticmethod
    def is_asian(dt=None):
        s = OANDAFeed.current_sessions(dt)
        return "Tokyo" in s or "Sydney" in s

    @staticmethod
    def minutes_to_next_news(dt=None):
        try:
            from config import FX
            dt = dt or datetime.now(timezone.utc)
            wd,h,m = dt.weekday(),dt.hour,dt.minute
            cur = wd*1440+h*60+m
            best = None
            for (ewd,eh,em,_) in FX.news_events:
                ev = ewd*1440+eh*60+em
                diff = ev-cur
                if 0<=diff<=240:
                    best = diff if best is None else min(best,diff)
            return best
        except:
            return None

    @staticmethod
    def news_blackout(window_min=15, dt=None):
        mins = OANDAFeed.minutes_to_next_news(dt)
        return mins is not None and mins <= window_min

    async def poll_prices(self, interval=60):
        while True:
            for pair in ["EUR_USD","GBP_USD","USD_JPY"]:
                try:
                    await self.get_price(pair)
                    await asyncio.sleep(13)
                except:
                    pass
            await asyncio.sleep(interval)
