"""
Unified Paper Trading Simulator
================================
Markets : Crypto (Binance) · Crash/Boom (Deriv) · Forex (OANDA demo)
Purpose : Run all strategies in simulation simultaneously, track per-market
          and per-strategy performance, surface where the system performs best.

Dependencies:
    pip install ccxt oandapyV20 pandas numpy scipy requests python-dotenv websocket-client

Env vars (.env):
    BINANCE_API_KEY=...   BINANCE_SECRET=...
    OANDA_API_KEY=...     OANDA_ACCOUNT_ID=...
    DERIV_APP_ID=...      (get free at https://developers.deriv.com)
"""

import os, time, json, logging, threading, uuid
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from collections import defaultdict
from dotenv import load_dotenv
import numpy as np
import pandas as pd

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("paper_trading.log")],
)
log = logging.getLogger("PaperSim")


# ══════════════════════════════════════════════════════════════════════
#  Enums & constants
# ══════════════════════════════════════════════════════════════════════

class Market(str, Enum):
    CRYPTO    = "Crypto"
    CRASH_BOOM= "Crash/Boom"
    FOREX     = "Forex"

class Side(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class TradeStatus(str, Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

# Forex sessions in UTC
SESSIONS = {
    "Sydney":  (21, 6),   # 21:00–06:00 UTC
    "Tokyo":   (0,  9),
    "London":  (7,  16),
    "NewYork": (13, 22),
}

# High-impact news events (static calendar stub — replace with live feed)
NEWS_EVENTS = [
    # (weekday 0=Mon, hour_utc, minute, description)
    (4, 13, 30, "NFP"),
    (1, 13, 30, "CPI"),
    (2, 19,  0, "FOMC"),
    (3,  9, 30, "BOE"),
    (3,  8, 30, "ECB"),
]


# ══════════════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════════════

@dataclass
class SimConfig:
    # Starting balance per market (paper money)
    initial_balance: dict = field(default_factory=lambda: {
        Market.CRYPTO:     10_000.0,
        Market.CRASH_BOOM: 10_000.0,
        Market.FOREX:      10_000.0,
    })

    # Risk per trade (fraction of current balance)
    risk_per_trade: float = 0.02          # 2%

    # Max concurrent open trades per market
    max_open_per_market: int = 3

    # Slippage models (realistic for paper trading)
    slippage: dict = field(default_factory=lambda: {
        Market.CRYPTO:     0.0003,   # 0.03% per side
        Market.CRASH_BOOM: 0.0005,   # Deriv spread proxy
        Market.FOREX:      0.0001,   # ~1 pip on EUR/USD
    })

    # Minimum WR before raising concern flag
    min_wr_threshold: float = 0.55

    # Divergence from backtest WR to trigger alert
    wr_divergence_alert: float = 0.08

    # Rebalance check interval (seconds)
    rebalance_interval: int = 3600        # hourly


CFG = SimConfig()


# ══════════════════════════════════════════════════════════════════════
#  Trade record
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    id: str                = field(default_factory=lambda: str(uuid.uuid4())[:8])
    market: Market         = Market.CRYPTO
    strategy: str          = ""
    symbol: str            = ""
    side: Side             = Side.BUY
    entry_price: float     = 0.0
    exit_price: float      = 0.0
    size: float            = 0.0           # units / lots / contracts
    notional_usd: float    = 0.0
    sl: float              = 0.0
    tp: float              = 0.0
    status: TradeStatus    = TradeStatus.OPEN
    open_time: datetime    = field(default_factory=lambda: datetime.now(timezone.utc))
    close_time: Optional[datetime] = None
    pnl_usd: float         = 0.0
    pnl_pct: float         = 0.0
    exit_reason: str       = ""
    session: str           = ""            # Forex: which session
    news_filter_passed: bool = True        # Forex: was news check passed
    slippage_cost: float   = 0.0
    metadata: dict         = field(default_factory=dict)

    def hold_minutes(self) -> float:
        end = self.close_time or datetime.now(timezone.utc)
        return (end - self.open_time).total_seconds() / 60

    def is_winner(self) -> bool:
        return self.pnl_usd > 0


# ══════════════════════════════════════════════════════════════════════
#  Market-specific price feeds (paper — uses live prices, no real orders)
# ══════════════════════════════════════════════════════════════════════

class CryptoPriceFeed:
    """Live prices from Binance public API — no auth needed."""

    BASE = "https://api.binance.com/api/v3"

    def get_price(self, symbol: str) -> float:
        """symbol e.g. 'BTCUSDT'"""
        import urllib.request
        url = f"{self.BASE}/ticker/price?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        return float(data["price"])

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> pd.DataFrame:
        import urllib.request
        url = f"{self.BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_buy_base",
            "taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df.set_index("open_time")


class DerivPriceFeed:
    """
    Deriv WebSocket tick feed.
    Runs in background thread, exposes latest tick via get_price().
    """

    WS_URL = "wss://ws.binaryws.com/websockets/v3?app_id={app_id}"

    def __init__(self, app_id: str = None):
        self.app_id  = app_id or os.getenv("DERIV_APP_ID", "1089")
        self._prices = {}
        self._lock   = threading.Lock()

    def subscribe(self, symbols: list[str]):
        """Start WebSocket subscription in background thread."""
        import websocket

        def on_message(ws, message):
            data = json.loads(message)
            if data.get("msg_type") == "tick":
                sym = data["tick"]["symbol"]
                price = data["tick"]["quote"]
                with self._lock:
                    self._prices[sym] = price

        def on_open(ws):
            for sym in symbols:
                ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
            log.info(f"Deriv WS subscribed: {symbols}")

        def run():
            ws = websocket.WebSocketApp(
                self.WS_URL.format(app_id=self.app_id),
                on_open=on_open,
                on_message=on_message,
            )
            ws.run_forever(ping_interval=30)

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def get_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(symbol)

    def get_ticks_dataframe(self, symbol: str, count: int = 1000) -> pd.DataFrame:
        """
        Fetch historical tick data via HTTP (Deriv ticks history API).
        Returns DataFrame with columns: time, price.
        """
        import urllib.request
        end = int(time.time())
        url = (
            f"https://api.deriv.com/v3/ticks_history"
            f"?ticks_history={symbol}&end={end}&count={count}&style=ticks"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            times  = data["history"]["times"]
            prices = data["history"]["prices"]
            df = pd.DataFrame({"time": times, "price": prices})
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            return df.set_index("time")
        except Exception as e:
            log.warning(f"Deriv tick fetch failed: {e}")
            return pd.DataFrame()


class ForexPriceFeed:
    """
    OANDA v20 REST API — demo account, no real money.
    Falls back to stub prices if no API key configured.
    """

    BASE = "https://api-fxpractice.oanda.com/v3"

    def __init__(self):
        self.api_key    = os.getenv("OANDA_API_KEY", "")
        self.account_id = os.getenv("OANDA_ACCOUNT_ID", "")
        self.headers    = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    def get_price(self, instrument: str) -> tuple[float, float]:
        """Returns (bid, ask). instrument e.g. 'EUR_USD'"""
        if not self.api_key:
            return self._stub_price(instrument)
        import urllib.request
        url = f"{self.BASE}/accounts/{self.account_id}/pricing?instruments={instrument}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            price = data["prices"][0]
            return float(price["bids"][0]["price"]), float(price["asks"][0]["price"])
        except Exception as e:
            log.warning(f"OANDA price fetch failed ({instrument}): {e}")
            return self._stub_price(instrument)

    def get_candles(self, instrument: str, granularity: str = "M5", count: int = 200) -> pd.DataFrame:
        """granularity: S5/M1/M5/M15/H1/H4/D"""
        if not self.api_key:
            return pd.DataFrame()
        import urllib.request
        url = (
            f"{self.BASE}/instruments/{instrument}/candles"
            f"?granularity={granularity}&count={count}&price=M"
        )
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            rows = []
            for c in data["candles"]:
                if c["complete"]:
                    m = c["mid"]
                    rows.append({
                        "time":   pd.Timestamp(c["time"]),
                        "open":   float(m["o"]),
                        "high":   float(m["h"]),
                        "low":    float(m["l"]),
                        "close":  float(m["c"]),
                        "volume": int(c["volume"]),
                    })
            df = pd.DataFrame(rows).set_index("time")
            df.index = df.index.tz_localize("UTC")
            return df
        except Exception as e:
            log.warning(f"OANDA candles failed: {e}")
            return pd.DataFrame()

    @staticmethod
    def _stub_price(instrument: str) -> tuple[float, float]:
        """Stub prices for testing without API key."""
        stubs = {
            "EUR_USD": (1.0850, 1.0851),
            "GBP_USD": (1.2700, 1.2702),
            "USD_JPY": (149.50, 149.52),
            "XAU_USD": (2320.0, 2320.5),
            "EUR_JPY": (162.20, 162.22),
            "GBP_JPY": (189.80, 189.83),
        }
        return stubs.get(instrument, (1.0000, 1.0002))


# ══════════════════════════════════════════════════════════════════════
#  Session & news utilities (Forex-specific)
# ══════════════════════════════════════════════════════════════════════

class ForexContext:

    @staticmethod
    def current_sessions(dt: datetime = None) -> list[str]:
        """Return list of currently active Forex sessions."""
        dt = dt or datetime.now(timezone.utc)
        h = dt.hour
        active = []
        for name, (start, end) in SESSIONS.items():
            if start < end:
                if start <= h < end:
                    active.append(name)
            else:   # wraps midnight
                if h >= start or h < end:
                    active.append(name)
        return active

    @staticmethod
    def is_london_open(dt: datetime = None) -> bool:
        sessions = ForexContext.current_sessions(dt)
        return "London" in sessions

    @staticmethod
    def is_overlap(dt: datetime = None) -> bool:
        """London/NY overlap — highest liquidity."""
        sessions = ForexContext.current_sessions(dt)
        return "London" in sessions and "NewYork" in sessions

    @staticmethod
    def is_asian(dt: datetime = None) -> bool:
        sessions = ForexContext.current_sessions(dt)
        return "Tokyo" in sessions or "Sydney" in sessions

    @staticmethod
    def minutes_to_news(dt: datetime = None) -> Optional[int]:
        """Returns minutes until next high-impact news, or None if >4h away."""
        dt = dt or datetime.now(timezone.utc)
        wd, h, m = dt.weekday(), dt.hour, dt.minute
        current_mins = wd * 1440 + h * 60 + m
        best = None
        for (event_wd, event_h, event_m, _) in NEWS_EVENTS:
            event_mins = event_wd * 1440 + event_h * 60 + event_m
            diff = event_mins - current_mins
            if 0 <= diff <= 240:    # within 4 hours
                if best is None or diff < best:
                    best = diff
        return best

    @staticmethod
    def news_blackout(dt: datetime = None, window_min: int = 15) -> bool:
        """True if within window_min minutes of a high-impact news event."""
        mins = ForexContext.minutes_to_news(dt)
        return mins is not None and mins <= window_min


# ══════════════════════════════════════════════════════════════════════
#  Simple signal stubs (replace with real model predictions)
# ══════════════════════════════════════════════════════════════════════

class SignalEngine:
    """
    Stub signal engine. In production, each method calls the trained
    ML model (XGBoost / LSTM) from the feature pipeline.
    Returns None = no signal, or dict with direction and confidence.
    """

    def fx_s1_london_breakout(self, candles: pd.DataFrame) -> Optional[dict]:
        """Asian range breakout at London open."""
        if candles.empty or len(candles) < 20:
            return None
        if not ForexContext.is_london_open():
            return None
        if ForexContext.news_blackout():
            return None

        # Identify Asian range (last 6 hours before London open)
        asian_candles = candles.iloc[-36:-6]   # M10 proxy
        if asian_candles.empty:
            return None
        range_high = asian_candles["high"].max()
        range_low  = asian_candles["low"].min()
        current    = candles["close"].iloc[-1]
        atr        = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1]

        if current > range_high + atr * 0.1:
            return {"direction": Side.BUY, "confidence": 0.70, "range_high": range_high, "atr": atr}
        if current < range_low - atr * 0.1:
            return {"direction": Side.SELL, "confidence": 0.70, "range_low": range_low, "atr": atr}
        return None

    def fx_s2_overlap_divergence(self, candles: pd.DataFrame) -> Optional[dict]:
        """RSI divergence during London/NY overlap."""
        if not ForexContext.is_overlap():
            return None
        if ForexContext.news_blackout():
            return None
        if candles.empty or len(candles) < 30:
            return None

        close = candles["close"]
        rsi   = self._rsi(close, 14)
        if rsi.isna().all():
            return None

        current_rsi   = rsi.iloc[-1]
        current_price = close.iloc[-1]
        lookback      = 20

        # Bearish divergence: new high + lower RSI
        if (current_price > close.iloc[-lookback:-1].max() and
                current_rsi < rsi.iloc[-lookback:-1].max()):
            return {"direction": Side.SELL, "confidence": 0.74, "rsi": current_rsi}

        # Bullish divergence: new low + higher RSI
        if (current_price < close.iloc[-lookback:-1].min() and
                current_rsi > rsi.iloc[-lookback:-1].min()):
            return {"direction": Side.BUY, "confidence": 0.74, "rsi": current_rsi}

        return None

    def fx_s3_news_breakout(self, candles: pd.DataFrame) -> Optional[dict]:
        """Pre-news compression breakout."""
        mins = ForexContext.minutes_to_news()
        if mins is None or not (5 <= mins <= 25):
            return None

        if candles.empty or len(candles) < 20:
            return None

        close = candles["close"]
        std   = close.rolling(20).std().iloc[-1]
        std5  = close.rolling(5).std().iloc[-1]

        # Squeeze: short-term vol below long-term vol (compression)
        if std5 < std * 0.5:
            return {"direction": "STRADDLE", "confidence": 0.65, "mins_to_news": mins}
        return None

    def fx_s4_asian_reversion(self, candles: pd.DataFrame) -> Optional[dict]:
        """Mean reversion during Asian session."""
        if not ForexContext.is_asian():
            return None
        if ForexContext.news_blackout(window_min=30):
            return None
        if candles.empty or len(candles) < 30:
            return None

        close  = candles["close"]
        rsi    = self._rsi(close, 14)
        hurst  = self._hurst(close.values[-50:]) if len(close) >= 50 else 0.5

        if hurst > 0.45:    # Not mean-reverting
            return None

        cur_rsi = rsi.iloc[-1]
        if cur_rsi < 25:
            return {"direction": Side.BUY, "confidence": 0.72, "rsi": cur_rsi, "hurst": hurst}
        if cur_rsi > 75:
            return {"direction": Side.SELL, "confidence": 0.72, "rsi": cur_rsi, "hurst": hurst}
        return None

    def crypto_momentum(self, candles: pd.DataFrame, funding_rate: float = 0.0) -> Optional[dict]:
        """Funding rate + momentum signal for crypto perps."""
        if candles.empty or len(candles) < 30:
            return None
        close  = candles["close"]
        rsi    = self._rsi(close, 14).iloc[-1]
        ema9   = close.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21  = close.ewm(span=21, adjust=False).mean().iloc[-1]

        if funding_rate > 0.001 and rsi > 60 and ema9 > ema21:
            return {"direction": Side.BUY, "confidence": 0.68}
        if funding_rate < -0.0005 and rsi < 40 and ema9 < ema21:
            return {"direction": Side.SELL, "confidence": 0.68}
        return None

    def cb_compression_spike(self, ticks_since_spike: int, avg_ticks: int,
                              compression: float, tssl: float) -> Optional[dict]:
        """Crash/Boom: geometric probability + compression gate."""
        prob = 1 - (1 - 1 / avg_ticks) ** ticks_since_spike
        if prob > 0.70 and compression < 0.4 and abs(tssl) > 0.5:
            direction = Side.BUY if tssl > 0 else Side.SELL
            return {"direction": direction, "confidence": prob, "prob": prob}
        return None

    def cb_sniper(self, h4_rsi: float, h1_streak: int, m5_rsi: float,
                  spike_direction: str) -> Optional[dict]:
        """M5 Sniper: 3-TF exhaustion check."""
        h4_extreme = h4_rsi < 30 or h4_rsi > 70
        h1_pattern = h1_streak >= 4
        if spike_direction == "up":
            m5_exhaust = m5_rsi < 28
        else:
            m5_exhaust = m5_rsi > 72

        if h4_extreme and h1_pattern and m5_exhaust:
            direction = Side.BUY if spike_direction == "up" else Side.SELL
            return {"direction": direction, "confidence": 0.95}
        return None

    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss  = (-delta).clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _hurst(ts: np.ndarray) -> float:
        """Hurst exponent via R/S analysis. <0.5 = mean-reverting."""
        if len(ts) < 20:
            return 0.5
        lags = range(2, min(20, len(ts) // 2))
        rs_vals = []
        for lag in lags:
            sub  = ts[:lag]
            mean = sub.mean()
            dev  = np.cumsum(sub - mean)
            r    = dev.max() - dev.min()
            s    = sub.std()
            if s > 0:
                rs_vals.append((lag, r / s))
        if len(rs_vals) < 4:
            return 0.5
        x = np.log([v[0] for v in rs_vals])
        y = np.log([v[1] for v in rs_vals])
        return float(np.polyfit(x, y, 1)[0])


# ══════════════════════════════════════════════════════════════════════
#  Paper account
# ══════════════════════════════════════════════════════════════════════

class PaperAccount:
    """Simulates a broker account per market with full trade ledger."""

    def __init__(self, market: Market, initial_balance: float):
        self.market      = market
        self.balance     = initial_balance
        self.initial     = initial_balance
        self.trades: list[Trade] = []
        self._lock       = threading.Lock()

    # ── Order management ──────────────────────────────────────────────

    def open_trade(
        self, strategy: str, symbol: str, side: Side,
        entry_price: float, sl: float, tp: float,
        session: str = "", news_ok: bool = True,
    ) -> Optional[Trade]:
        with self._lock:
            open_count = sum(1 for t in self.trades if t.status == TradeStatus.OPEN)
            if open_count >= CFG.max_open_per_market:
                return None

            # Apply slippage
            slip  = CFG.slippage[self.market]
            ep    = entry_price * (1 + slip) if side == Side.BUY else entry_price * (1 - slip)
            slip_cost = abs(ep - entry_price)

            # Size by risk
            risk_usd  = self.balance * CFG.risk_per_trade
            sl_dist   = abs(ep - sl)
            if sl_dist < 1e-9:
                return None
            size      = risk_usd / sl_dist
            notional  = size * ep

            t = Trade(
                market=self.market,
                strategy=strategy,
                symbol=symbol,
                side=side,
                entry_price=ep,
                sl=sl,
                tp=tp,
                size=size,
                notional_usd=notional,
                session=session,
                news_filter_passed=news_ok,
                slippage_cost=slip_cost * size,
            )
            self.trades.append(t)
            log.info(
                f"[{self.market.value}] OPEN {strategy} {side.value} {symbol} "
                f"@ {ep:.5f} | SL={sl:.5f} TP={tp:.5f} | notional=${notional:.2f}"
            )
            return t

    def close_trade(self, trade: Trade, exit_price: float, reason: str = "") -> float:
        with self._lock:
            if trade.status != TradeStatus.OPEN:
                return 0.0

            slip = CFG.slippage[self.market]
            ep   = exit_price * (1 - slip) if trade.side == Side.BUY else exit_price * (1 + slip)
            slip_cost = abs(ep - exit_price) * trade.size

            if trade.side == Side.BUY:
                raw_pnl = (ep - trade.entry_price) * trade.size
            else:
                raw_pnl = (trade.entry_price - ep) * trade.size

            pnl = raw_pnl - slip_cost
            self.balance += pnl

            trade.exit_price   = ep
            trade.close_time   = datetime.now(timezone.utc)
            trade.pnl_usd      = pnl
            trade.pnl_pct      = pnl / trade.notional_usd if trade.notional_usd else 0
            trade.slippage_cost += slip_cost
            trade.exit_reason  = reason
            trade.status       = TradeStatus.CLOSED

            emoji = "+" if pnl >= 0 else ""
            log.info(
                f"[{self.market.value}] CLOSE {trade.strategy} {trade.symbol} "
                f"@ {ep:.5f} | PnL={emoji}{pnl:.2f} USD | reason={reason}"
            )
            return pnl

    def mark_to_market(self, trade: Trade, current_price: float) -> float:
        """Unrealised PnL at current price."""
        if trade.side == Side.BUY:
            return (current_price - trade.entry_price) * trade.size
        return (trade.entry_price - current_price) * trade.size

    def check_sl_tp(self, trade: Trade, current_price: float) -> Optional[str]:
        """Returns 'SL' | 'TP' | None."""
        if trade.status != TradeStatus.OPEN:
            return None
        if trade.side == Side.BUY:
            if current_price <= trade.sl:
                return "SL"
            if trade.tp > 0 and current_price >= trade.tp:
                return "TP"
        else:
            if current_price >= trade.sl:
                return "SL"
            if trade.tp > 0 and current_price <= trade.tp:
                return "TP"
        return None

    # ── Statistics ────────────────────────────────────────────────────

    def stats(self) -> dict:
        closed = [t for t in self.trades if t.status == TradeStatus.CLOSED]
        if not closed:
            return {
                "market": self.market.value,
                "balance": self.balance,
                "equity_pct": 0.0,
                "trades": 0,
                "wr": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "max_dd": 0.0,
                "avg_hold_min": 0.0,
            }

        wins    = [t for t in closed if t.is_winner()]
        losses  = [t for t in closed if not t.is_winner()]
        pnls    = [t.pnl_usd for t in closed]
        gross_p = sum(t.pnl_usd for t in wins)
        gross_l = abs(sum(t.pnl_usd for t in losses))

        # Drawdown
        running = np.cumsum(pnls)
        peak    = np.maximum.accumulate(running)
        dd      = (peak - running) / (peak + self.initial + 1e-9)
        max_dd  = float(dd.max()) if len(dd) else 0.0

        # Sharpe (annualised, assume 252 trading days, ~8 trades/day)
        pnl_arr = np.array(pnls)
        sharpe  = (pnl_arr.mean() / (pnl_arr.std() + 1e-9)) * np.sqrt(252 * 8) if len(pnl_arr) > 1 else 0.0

        return {
            "market":      self.market.value,
            "balance":     round(self.balance, 2),
            "equity_pct":  round((self.balance - self.initial) / self.initial * 100, 2),
            "trades":      len(closed),
            "wr":          round(len(wins) / len(closed) * 100, 1),
            "avg_pnl":     round(np.mean(pnls), 2),
            "total_pnl":   round(sum(pnls), 2),
            "profit_factor": round(gross_p / gross_l, 2) if gross_l > 0 else float("inf"),
            "sharpe":      round(sharpe, 2),
            "max_dd":      round(max_dd * 100, 2),
            "avg_hold_min": round(np.mean([t.hold_minutes() for t in closed]), 1),
        }

    def strategy_breakdown(self) -> pd.DataFrame:
        closed = [t for t in self.trades if t.status == TradeStatus.CLOSED]
        if not closed:
            return pd.DataFrame()
        rows = []
        for strat in set(t.strategy for t in closed):
            st = [t for t in closed if t.strategy == strat]
            wins = [t for t in st if t.is_winner()]
            rows.append({
                "strategy":  strat,
                "trades":    len(st),
                "wr_%":      round(len(wins) / len(st) * 100, 1),
                "total_pnl": round(sum(t.pnl_usd for t in st), 2),
                "avg_pnl":   round(np.mean([t.pnl_usd for t in st]), 2),
                "avg_hold":  round(np.mean([t.hold_minutes() for t in st]), 1),
            })
        return pd.DataFrame(rows).sort_values("wr_%", ascending=False).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════
#  Performance comparator
# ══════════════════════════════════════════════════════════════════════

class PerformanceComparator:
    """
    Ranks the three markets and identifies the best-performing one
    across multiple dimensions. Used to decide capital allocation.
    """

    BACKTEST_WR = {
        # Strategy : expected WR from backtests
        "CB-S1": 83, "CB-S2": 81, "CB-S3": 92, "CB-S4": 96,
        "FX-S1": 71, "FX-S2": 76, "FX-S3": 66, "FX-S4": 73,
        "CR-S1": 75, "CR-S2": 80,
    }

    def compare(self, accounts: dict[Market, PaperAccount]) -> pd.DataFrame:
        rows = []
        for market, acc in accounts.items():
            s = acc.stats()
            rows.append(s)
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        # Composite score: WR × 0.3 + Sharpe × 0.3 + Profit factor × 0.2 + Equity% × 0.2
        df["wr_norm"]  = df["wr"] / max(df["wr"].max(), 1)
        sh_pos = df["sharpe"].where(df["sharpe"] >= 0, 0)
        df["sh_norm"]  = sh_pos / max(sh_pos.max(), 1e-9)
        pf_capped = df["profit_factor"].where(df["profit_factor"] <= 5, 5)
        df["pf_norm"]  = pf_capped / 5
        df["eq_norm"]  = (df["equity_pct"] + 100) / 200

        df["composite_score"] = (
            df["wr_norm"]  * 0.30 +
            df["sh_norm"]  * 0.30 +
            df["pf_norm"]  * 0.20 +
            df["eq_norm"]  * 0.20
        ).round(3)

        df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
        df["rank"] = df.index + 1
        return df

    def divergence_alerts(self, accounts: dict[Market, PaperAccount]) -> list[str]:
        """Check live WR vs backtest WR. Flag divergences."""
        alerts = []
        for market, acc in accounts.items():
            breakdown = acc.strategy_breakdown()
            if breakdown.empty:
                continue
            for _, row in breakdown.iterrows():
                strat = row["strategy"]
                bt_wr = self.BACKTEST_WR.get(strat)
                if bt_wr and row["trades"] >= 20:
                    diff = bt_wr - row["wr_%"]
                    if diff > CFG.wr_divergence_alert * 100:
                        alerts.append(
                            f"DIVERGENCE [{market.value}] {strat}: "
                            f"live WR={row['wr_%']}% vs backtest={bt_wr}% "
                            f"(gap={diff:.1f}pp over {row['trades']} trades)"
                        )
        return alerts

    def allocation_recommendation(self, df: pd.DataFrame) -> dict:
        """
        Suggests how to redistribute capital based on performance.
        Top performer gets 50%, second 30%, third 20%.
        """
        if df.empty or len(df) < 3:
            return {}
        allocs = {0: 0.50, 1: 0.30, 2: 0.20}
        return {
            row["market"]: allocs[i]
            for i, (_, row) in enumerate(df.iterrows())
        }


# ══════════════════════════════════════════════════════════════════════
#  Unified simulator
# ══════════════════════════════════════════════════════════════════════

class UnifiedPaperSimulator:
    """
    Main simulator. Runs all three markets in parallel threads,
    evaluates signals on each cycle, manages paper trades, and
    prints a live performance dashboard every 60 seconds.
    """

    def __init__(self):
        self.accounts = {
            Market.CRYPTO:     PaperAccount(Market.CRYPTO,     CFG.initial_balance[Market.CRYPTO]),
            Market.CRASH_BOOM: PaperAccount(Market.CRASH_BOOM, CFG.initial_balance[Market.CRASH_BOOM]),
            Market.FOREX:      PaperAccount(Market.FOREX,      CFG.initial_balance[Market.FOREX]),
        }
        self.crypto_feed = CryptoPriceFeed()
        self.deriv_feed  = DerivPriceFeed()
        self.forex_feed  = ForexPriceFeed()
        self.signals     = SignalEngine()
        self.comparator  = PerformanceComparator()
        self._stop       = threading.Event()

    def run(self, duration_hours: float = None):
        log.info("=" * 70)
        log.info("UNIFIED PAPER TRADING SIMULATOR — starting")
        log.info(f"Crypto:     ${CFG.initial_balance[Market.CRYPTO]:,.0f}")
        log.info(f"Crash/Boom: ${CFG.initial_balance[Market.CRASH_BOOM]:,.0f}")
        log.info(f"Forex:      ${CFG.initial_balance[Market.FOREX]:,.0f}")
        log.info("=" * 70)

        threads = [
            threading.Thread(target=self._run_crypto,    daemon=True, name="CryptoLoop"),
            threading.Thread(target=self._run_crash_boom,daemon=True, name="CBLoop"),
            threading.Thread(target=self._run_forex,     daemon=True, name="ForexLoop"),
            threading.Thread(target=self._dashboard_loop,daemon=True, name="Dashboard"),
        ]
        for t in threads:
            t.start()

        try:
            if duration_hours:
                time.sleep(duration_hours * 3600)
                self._stop.set()
            else:
                while not self._stop.is_set():
                    time.sleep(1)
        except KeyboardInterrupt:
            self._stop.set()

        log.info("Simulator stopped — final report:")
        self.print_report()

    # ── Market loops ──────────────────────────────────────────────────

    def _run_crypto(self):
        acc = self.accounts[Market.CRYPTO]
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        while not self._stop.is_set():
            for sym in symbols:
                try:
                    candles = self.crypto_feed.get_klines(sym, "5m", 100)
                    price   = self.crypto_feed.get_price(sym)
                    self._check_sl_tp_crypto(acc, sym, price)
                    sig = self.signals.crypto_momentum(candles)
                    if sig and sig["direction"] != "STRADDLE":
                        atr = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1]
                        sl  = price - atr * 1.5 if sig["direction"] == Side.BUY else price + atr * 1.5
                        tp  = price + atr * 3.0 if sig["direction"] == Side.BUY else price - atr * 3.0
                        acc.open_trade("CR-S1", sym, sig["direction"], price, sl, tp)
                except Exception as e:
                    log.warning(f"Crypto loop error ({sym}): {e}")
            time.sleep(60)

    def _run_crash_boom(self):
        acc = self.accounts[Market.CRASH_BOOM]
        symbols = [
            ("Boom_500",  500, "up"),
            ("Crash_1000",1000,"down"),
            ("Boom_300",  300, "up"),
        ]
        ticks_since_spike = defaultdict(int)

        while not self._stop.is_set():
            for sym, avg_ticks, direction in symbols:
                try:
                    price = self.deriv_feed.get_price(sym)
                    if price is None:
                        ticks_since_spike[sym] += 1
                        continue

                    # Simulate spike detection (real: use tick stream)
                    ticks_since_spike[sym] += 1
                    compression = np.random.uniform(0.2, 0.8)
                    tssl        = np.random.uniform(-1, 1)
                    h4_rsi      = np.random.uniform(20, 80)
                    h1_streak   = np.random.randint(0, 8)
                    m5_rsi      = np.random.uniform(15, 85)

                    self._check_sl_tp_cb(acc, sym, price)

                    # S1: Apex compression
                    sig = self.signals.cb_compression_spike(
                        ticks_since_spike[sym], avg_ticks, compression, tssl
                    )
                    if sig:
                        sl = price * 0.997 if sig["direction"] == Side.BUY else price * 1.003
                        tp = price * 1.008 if sig["direction"] == Side.BUY else price * 0.992
                        acc.open_trade("CB-S1", sym, sig["direction"], price, sl, tp)

                    # S4: Sniper
                    snap = self.signals.cb_sniper(h4_rsi, h1_streak, m5_rsi, direction)
                    if snap:
                        sl = price * 0.998 if snap["direction"] == Side.BUY else price * 1.002
                        tp = price * 1.005 if snap["direction"] == Side.BUY else price * 0.995
                        acc.open_trade("CB-S4", sym, snap["direction"], price, sl, tp)

                except Exception as e:
                    log.warning(f"CB loop error ({sym}): {e}")
            time.sleep(30)

    def _run_forex(self):
        acc     = self.accounts[Market.FOREX]
        pairs   = ["EUR_USD", "GBP_USD", "USD_JPY", "EUR_JPY", "GBP_JPY"]
        context = ForexContext()

        while not self._stop.is_set():
            sessions = context.current_sessions()
            for pair in pairs:
                try:
                    bid, ask = self.forex_feed.get_price(pair)
                    mid      = (bid + ask) / 2
                    candles  = self.forex_feed.get_candles(pair, "M5", 200)

                    self._check_sl_tp_fx(acc, pair, mid)

                    news_ok = not context.news_blackout()

                    # FX-S1: London breakout
                    if "London" in sessions:
                        sig = self.signals.fx_s1_london_breakout(candles)
                        if sig and news_ok:
                            atr = sig.get("atr", mid * 0.001)
                            sl  = mid - atr * 1.5 if sig["direction"] == Side.BUY else mid + atr * 1.5
                            tp  = mid + atr * 3.0 if sig["direction"] == Side.BUY else mid - atr * 3.0
                            acc.open_trade("FX-S1", pair, sig["direction"], ask if sig["direction"] == Side.BUY else bid, sl, tp, session="London", news_ok=news_ok)

                    # FX-S2: Overlap divergence
                    if context.is_overlap():
                        sig = self.signals.fx_s2_overlap_divergence(candles)
                        if sig and news_ok:
                            atr = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1] if not candles.empty else mid * 0.001
                            sl  = mid - atr * 1.2 if sig["direction"] == Side.BUY else mid + atr * 1.2
                            tp  = mid + atr * 2.5 if sig["direction"] == Side.BUY else mid - atr * 2.5
                            acc.open_trade("FX-S2", pair, sig["direction"], ask if sig["direction"] == Side.BUY else bid, sl, tp, session="Overlap", news_ok=news_ok)

                    # FX-S3: News breakout (straddle — simplified: pick direction by RSI)
                    sig3 = self.signals.fx_s3_news_breakout(candles)
                    if sig3 and not candles.empty:
                        rsi_now = self.signals._rsi(candles["close"], 14).iloc[-1]
                        direction = Side.BUY if rsi_now < 50 else Side.SELL
                        atr = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1]
                        sl  = mid - atr * 1.0 if direction == Side.BUY else mid + atr * 1.0
                        tp  = mid + atr * 2.0 if direction == Side.BUY else mid - atr * 2.0
                        acc.open_trade("FX-S3", pair, direction, ask if direction == Side.BUY else bid, sl, tp, session="News", news_ok=True)

                    # FX-S4: Asian reversion
                    if context.is_asian() and pair in ["EUR_JPY", "GBP_JPY"]:
                        sig = self.signals.fx_s4_asian_reversion(candles)
                        if sig and news_ok:
                            atr = (candles["high"] - candles["low"]).rolling(14).mean().iloc[-1] if not candles.empty else mid * 0.001
                            sl  = mid - atr * 1.0 if sig["direction"] == Side.BUY else mid + atr * 1.0
                            tp  = mid + atr * 1.5 if sig["direction"] == Side.BUY else mid - atr * 1.5
                            acc.open_trade("FX-S4", pair, sig["direction"], ask if sig["direction"] == Side.BUY else bid, sl, tp, session="Asian", news_ok=news_ok)

                except Exception as e:
                    log.warning(f"Forex loop error ({pair}): {e}")
            time.sleep(60)

    # ── SL/TP monitors ────────────────────────────────────────────────

    def _check_sl_tp_crypto(self, acc: PaperAccount, symbol: str, price: float):
        for t in [t for t in acc.trades if t.status == TradeStatus.OPEN and t.symbol == symbol]:
            hit = acc.check_sl_tp(t, price)
            if hit:
                acc.close_trade(t, price, hit)

    def _check_sl_tp_cb(self, acc: PaperAccount, symbol: str, price: float):
        for t in [t for t in acc.trades if t.status == TradeStatus.OPEN and t.symbol == symbol]:
            hit = acc.check_sl_tp(t, price)
            if hit:
                acc.close_trade(t, price, hit)

    def _check_sl_tp_fx(self, acc: PaperAccount, symbol: str, price: float):
        for t in [t for t in acc.trades if t.status == TradeStatus.OPEN and t.symbol == symbol]:
            hit = acc.check_sl_tp(t, price)
            if hit:
                acc.close_trade(t, price, hit)

    # ── Dashboard ─────────────────────────────────────────────────────

    def _dashboard_loop(self):
        while not self._stop.is_set():
            time.sleep(60)
            self.print_report()

    def print_report(self):
        df = self.comparator.compare(self.accounts)
        alerts = self.comparator.divergence_alerts(self.accounts)
        alloc  = self.comparator.allocation_recommendation(df)

        print("\n" + "═" * 72)
        print(f"  PAPER TRADING REPORT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print("═" * 72)

        if not df.empty:
            cols = ["rank","market","trades","wr","total_pnl","equity_pct",
                    "profit_factor","sharpe","max_dd","composite_score"]
            available = [c for c in cols if c in df.columns]
            print(df[available].to_string(index=False))
            print()

        # Per-market strategy breakdown
        for market, acc in self.accounts.items():
            bd = acc.strategy_breakdown()
            if not bd.empty:
                print(f"  {market.value} — strategy breakdown:")
                print(bd.to_string(index=False))
                print()

        # Alerts
        if alerts:
            print("  DIVERGENCE ALERTS:")
            for a in alerts:
                print(f"  ⚠  {a}")
            print()

        # Allocation recommendation
        if alloc:
            print("  RECOMMENDED CAPITAL ALLOCATION (based on live performance):")
            for mkt, pct in alloc.items():
                print(f"  {mkt:15s}  {pct*100:.0f}%")
        print("═" * 72 + "\n")

    def export_trades(self, path: str = "paper_trades.csv"):
        all_trades = []
        for acc in self.accounts.values():
            for t in acc.trades:
                all_trades.append({
                    "id": t.id, "market": t.market.value,
                    "strategy": t.strategy, "symbol": t.symbol,
                    "side": t.side.value, "status": t.status.value,
                    "entry_price": t.entry_price, "exit_price": t.exit_price,
                    "size": t.size, "notional_usd": round(t.notional_usd, 2),
                    "pnl_usd": round(t.pnl_usd, 4),
                    "pnl_pct": round(t.pnl_pct * 100, 4),
                    "slippage_cost": round(t.slippage_cost, 4),
                    "open_time": t.open_time.isoformat(),
                    "close_time": t.close_time.isoformat() if t.close_time else "",
                    "hold_min": round(t.hold_minutes(), 1),
                    "exit_reason": t.exit_reason,
                    "session": t.session,
                    "news_ok": t.news_filter_passed,
                })
        pd.DataFrame(all_trades).to_csv(path, index=False)
        log.info(f"Trades exported: {path} ({len(all_trades)} records)")


# ══════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"

    if mode == "report":
        # One-shot report on a saved trade CSV
        try:
            df = pd.read_csv("paper_trades.csv")
            print(df.groupby(["market","strategy"]).agg(
                trades=("pnl_usd","count"),
                wr=("pnl_usd", lambda x: round((x > 0).mean() * 100, 1)),
                total_pnl=("pnl_usd","sum"),
                avg_pnl=("pnl_usd","mean"),
            ).round(2).to_string())
        except FileNotFoundError:
            print("No paper_trades.csv found. Run the simulator first.")

    elif mode == "demo":
        # 5-minute demo run (no live API keys needed)
        sim = UnifiedPaperSimulator()
        print("Running 5-minute demo (no API keys required)...")
        sim.run(duration_hours=5/60)
        sim.export_trades()

    else:
        # Full live paper trading run
        sim = UnifiedPaperSimulator()
        try:
            sim.run()
        except KeyboardInterrupt:
            pass
        finally:
            sim.export_trades()
