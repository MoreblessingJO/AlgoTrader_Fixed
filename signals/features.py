"""
Crash & Boom Synthetic Index — Feature Engineering Pipeline
============================================================
Symbols  : Boom 300/500/600/900/1000 · Crash 300/500/600/900/1000
Data src : Deriv WebSocket tick feed  (or CSV export from Deriv)
Output   : Feature matrix ready for XGBoost / LSTM / RL training

All indicators implemented from scratch — no TA-Lib dependency.

Feature groups
--------------
1.  Tick-level microstructure   (spike detection, tick velocity, pressure)
2.  Candle features             (OHLCV derived, body/wick ratios)
3.  RSI family                  (standard, micro-RSI on ticks, divergence)
4.  Moving averages & momentum  (EMA family, MACD, rate-of-change)
5.  Volatility                  (ATR, Bollinger Bands, compression ratio)
6.  Trend / regime              (EMA slope, ADX, streak patterns, HMM-style)
7.  Spike-specific features     (ticks since last spike, spike magnitude history)
8.  Inter-timeframe features    (H4 → H1 → M5 hierarchy)
9.  Target labels               (spike in next N ticks, direction, magnitude)
"""

import warnings
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from scipy.stats import linregress
from sklearn.preprocessing import RobustScaler
from sklearn.pipeline import Pipeline

warnings.filterwarnings("ignore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("CB_Features")


# ══════════════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════════════

@dataclass
class IndexConfig:
    """Per-symbol config derived from Deriv's statistical properties."""
    name: str
    avg_ticks_between_spikes: int     # geometric distribution λ
    spike_direction: str              # "up" (Boom) | "down" (Crash)
    typical_spike_magnitude: float    # in price points, approximate
    session_typical_ticks_per_hour: int = 3600

    # Grid-searched thresholds (override after your own backtest)
    compression_ratio_threshold: float = 0.6
    micro_rsi_exhaustion_low: float = 25.0
    micro_rsi_exhaustion_high: float = 75.0
    tssl_threshold: float = 0.55


INDEX_CONFIGS = {
    "Boom300":   IndexConfig("Boom300",   300,  "up",   15.0),
    "Boom500":   IndexConfig("Boom500",   500,  "up",   25.0),
    "Boom600":   IndexConfig("Boom600",   600,  "up",   30.0),
    "Boom900":   IndexConfig("Boom900",   900,  "up",   45.0),
    "Boom1000":  IndexConfig("Boom1000", 1000,  "up",   60.0),
    "Crash300":  IndexConfig("Crash300",  300,  "down", 15.0),
    "Crash500":  IndexConfig("Crash500",  500,  "down", 25.0),
    "Crash600":  IndexConfig("Crash600",  600,  "down", 30.0),
    "Crash900":  IndexConfig("Crash900",  900,  "down", 45.0),
    "Crash1000": IndexConfig("Crash1000",1000,  "down", 60.0),
}


# ══════════════════════════════════════════════════════════════════════
#  1. Data ingestion & spike labelling
# ══════════════════════════════════════════════════════════════════════

class DataLoader:
    """
    Loads tick data from CSV (Deriv export format) or generates
    synthetic data for testing.

    Deriv CSV columns: timestamp, price
    """

    def load_csv(self, path: str, symbol: str) -> pd.DataFrame:
        cfg = INDEX_CONFIGS[symbol]
        df = pd.read_csv(path, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["symbol"] = symbol
        log.info(f"Loaded {len(df):,} ticks for {symbol}")
        return self._label_spikes(df, cfg)

    def generate_synthetic(self, symbol: str, n_ticks: int = 50_000) -> pd.DataFrame:
        """
        Generates realistic synthetic tick data for testing.
        Uses a geometric distribution for spike intervals —
        the same statistical process Deriv uses.
        """
        cfg = INDEX_CONFIGS[symbol]
        log.info(f"Generating {n_ticks:,} synthetic ticks for {symbol}...")

        np.random.seed(42)
        prices = [10_000.0]
        timestamps = [pd.Timestamp("2024-01-01")]

        spike_countdown = np.random.geometric(1 / cfg.avg_ticks_between_spikes)

        for i in range(1, n_ticks):
            # Normal tick: small Brownian drift
            drift = (1 if cfg.spike_direction == "up" else -1) * 0.002
            noise = np.random.normal(drift, 0.08)

            if spike_countdown <= 0:
                # Spike tick
                magnitude = cfg.typical_spike_magnitude * np.random.uniform(0.6, 1.6)
                move = magnitude if cfg.spike_direction == "up" else -magnitude
                prices.append(prices[-1] + move)
                spike_countdown = np.random.geometric(1 / cfg.avg_ticks_between_spikes)
            else:
                prices.append(prices[-1] + noise)
                spike_countdown -= 1

            timestamps.append(timestamps[-1] + pd.Timedelta(seconds=1))

        df = pd.DataFrame({"timestamp": timestamps, "price": prices, "symbol": symbol})
        return self._label_spikes(df, cfg)

    def _label_spikes(self, df: pd.DataFrame, cfg: IndexConfig) -> pd.DataFrame:
        """
        Detect spikes from raw tick data.
        A spike is a single-tick move exceeding 3× the rolling
        median absolute deviation of recent tick changes.
        """
        df = df.copy()
        df["tick_change"] = df["price"].diff().fillna(0)

        # Rolling MAD (robust to outliers)
        window = min(200, len(df) // 10)
        rolling_mad = (
            df["tick_change"]
            .abs()
            .rolling(window, min_periods=20)
            .median()
            .fillna(df["tick_change"].abs().median())
        )

        threshold = 3.0 * rolling_mad
        direction_ok = (
            (df["tick_change"] > 0) if cfg.spike_direction == "up"
            else (df["tick_change"] < 0)
        )
        df["is_spike"] = (df["tick_change"].abs() > threshold) & direction_ok
        df["spike_magnitude"] = np.where(df["is_spike"], df["tick_change"].abs(), 0.0)

        n_spikes = df["is_spike"].sum()
        log.info(f"  Detected {n_spikes:,} spikes ({n_spikes/len(df)*100:.2f}% of ticks)")
        return df


# ══════════════════════════════════════════════════════════════════════
#  2. Candle builder  (tick → OHLCV)
# ══════════════════════════════════════════════════════════════════════

class CandleBuilder:
    """Resamples tick data into multiple timeframe candles."""

    TIMEFRAMES = {
        "M1":  "1min",
        "M5":  "5min",
        "M15": "15min",
        "H1":  "1h",
        "H4":  "4h",
    }

    def build(self, ticks: pd.DataFrame) -> dict[str, pd.DataFrame]:
        """Returns a dict of {timeframe: OHLCV DataFrame}."""
        ticks = ticks.set_index("timestamp")
        candles = {}

        for label, freq in self.TIMEFRAMES.items():
            ohlcv = ticks["price"].resample(freq).ohlc()
            ohlcv["volume"] = ticks["price"].resample(freq).count()  # tick count as proxy volume
            ohlcv["spike_count"] = ticks["is_spike"].resample(freq).sum()
            ohlcv["spike_mag_sum"] = ticks["spike_magnitude"].resample(freq).sum()
            ohlcv = ohlcv.dropna(subset=["open"])
            candles[label] = ohlcv
            log.info(f"  Built {label}: {len(ohlcv):,} candles")

        return candles


# ══════════════════════════════════════════════════════════════════════
#  3. Indicator library  (pure NumPy / pandas)
# ══════════════════════════════════════════════════════════════════════

class Indicators:
    """
    All technical indicators implemented from first principles.
    Every method takes a pd.Series or np.ndarray and returns
    a pd.Series of the same length (NaN-padded at the start).
    """

    # ── Moving averages ───────────────────────────────────────────────

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def sma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(period, min_periods=1).mean()

    @staticmethod
    def wma(series: pd.Series, period: int) -> pd.Series:
        weights = np.arange(1, period + 1)
        return series.rolling(period, min_periods=1).apply(
            lambda x: np.dot(x[-len(weights[:len(x)]):], weights[:len(x)]) / weights[:len(x)].sum(),
            raw=True,
        )

    @staticmethod
    def hull_ma(series: pd.Series, period: int) -> pd.Series:
        """Hull Moving Average — faster and smoother than EMA."""
        half = max(period // 2, 1)
        sqrt_p = max(int(np.sqrt(period)), 1)
        wma_half = Indicators.wma(series, half)
        wma_full = Indicators.wma(series, period)
        raw = 2 * wma_half - wma_full
        return Indicators.wma(raw, sqrt_p)

    # ── RSI family ────────────────────────────────────────────────────

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def stoch_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14) -> pd.Series:
        rsi_vals = Indicators.rsi(series, rsi_period)
        rsi_min = rsi_vals.rolling(stoch_period, min_periods=1).min()
        rsi_max = rsi_vals.rolling(stoch_period, min_periods=1).max()
        denom = (rsi_max - rsi_min).replace(0, np.nan)
        return (rsi_vals - rsi_min) / denom

    @staticmethod
    def rsi_divergence(price: pd.Series, rsi_vals: pd.Series, lookback: int = 20) -> pd.Series:
        """
        Detects bullish (+1) and bearish (-1) RSI divergence.
        Bullish: price makes lower low, RSI makes higher low.
        Bearish: price makes higher high, RSI makes lower high.
        """
        n = len(price)
        divergence = pd.Series(0.0, index=price.index)

        price_arr = price.values
        rsi_arr = rsi_vals.values

        for i in range(lookback, n):
            window_price = price_arr[i - lookback:i + 1]
            window_rsi = rsi_arr[i - lookback:i + 1]

            if np.isnan(window_rsi).any():
                continue

            # Bearish: new price high but RSI lower than previous high
            prev_price_high_idx = np.argmax(window_price[:-1])
            if window_price[-1] > window_price[prev_price_high_idx]:
                if window_rsi[-1] < window_rsi[prev_price_high_idx]:
                    divergence.iloc[i] = -1.0

            # Bullish: new price low but RSI higher than previous low
            prev_price_low_idx = np.argmin(window_price[:-1])
            if window_price[-1] < window_price[prev_price_low_idx]:
                if window_rsi[-1] > window_rsi[prev_price_low_idx]:
                    divergence.iloc[i] = 1.0

        return divergence

    # ── Volatility ────────────────────────────────────────────────────

    @staticmethod
    def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    @staticmethod
    def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
        """Returns (upper, middle, lower, width, %B)."""
        mid = Indicators.sma(series, period)
        std = series.rolling(period, min_periods=1).std().fillna(0)
        upper = mid + std_dev * std
        lower = mid - std_dev * std
        width = (upper - lower) / mid.replace(0, np.nan)
        pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
        return upper, mid, lower, width, pct_b

    @staticmethod
    def compression_ratio(
        atr_vals: pd.Series,
        bb_width: pd.Series,
        period: int = 20,
    ) -> pd.Series:
        """
        Measures how compressed volatility is relative to recent history.
        0 = maximum compression (squeeze), 1 = maximum expansion.
        Core V4 brain feature.
        """
        atr_min = atr_vals.rolling(period, min_periods=1).min()
        atr_max = atr_vals.rolling(period, min_periods=1).max()
        atr_norm = (atr_vals - atr_min) / (atr_max - atr_min + 1e-9)

        bb_min = bb_width.rolling(period, min_periods=1).min()
        bb_max = bb_width.rolling(period, min_periods=1).max()
        bb_norm = (bb_width - bb_min) / (bb_max - bb_min + 1e-9)

        return (atr_norm + bb_norm) / 2.0

    # ── Momentum / MACD ───────────────────────────────────────────────

    @staticmethod
    def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
        """Returns (macd_line, signal_line, histogram)."""
        ema_fast = Indicators.ema(series, fast)
        ema_slow = Indicators.ema(series, slow)
        macd_line = ema_fast - ema_slow
        signal_line = Indicators.ema(macd_line, signal)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def rate_of_change(series: pd.Series, period: int = 10) -> pd.Series:
        return series.pct_change(period) * 100

    @staticmethod
    def momentum(series: pd.Series, period: int = 10) -> pd.Series:
        return series - series.shift(period)

    # ── ADX / trend strength ──────────────────────────────────────────

    @staticmethod
    def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
        """Returns (ADX, +DI, -DI)."""
        prev_high = high.shift(1)
        prev_low = low.shift(1)

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        atr_vals = Indicators.atr(high, low, close, period)

        plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(
            com=period - 1, adjust=False).mean() / atr_vals.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(
            com=period - 1, adjust=False).mean() / atr_vals.replace(0, np.nan)

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(com=period - 1, adjust=False).mean()
        return adx, plus_di, minus_di

    # ── Linear regression slope ───────────────────────────────────────

    @staticmethod
    def linear_slope(series: pd.Series, period: int = 20) -> pd.Series:
        """Rolling linear regression slope — normalised by price level."""
        def _slope(arr):
            if len(arr) < 2 or np.isnan(arr).any():
                return np.nan
            x = np.arange(len(arr))
            slope, _, _, _, _ = linregress(x, arr)
            return slope / (arr.mean() + 1e-9)

        return series.rolling(period, min_periods=5).apply(_slope, raw=True)

    # ── Candle pattern features ───────────────────────────────────────

    @staticmethod
    def candle_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Body/wick ratios, engulfing patterns, doji detection.
        Requires columns: open, high, low, close.
        """
        out = pd.DataFrame(index=df.index)
        body = (df["close"] - df["open"]).abs()
        candle_range = df["high"] - df["low"]
        upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
        lower_wick = df[["open", "close"]].min(axis=1) - df["low"]

        out["body_ratio"]       = body / candle_range.replace(0, np.nan)
        out["upper_wick_ratio"] = upper_wick / candle_range.replace(0, np.nan)
        out["lower_wick_ratio"] = lower_wick / candle_range.replace(0, np.nan)
        out["is_bullish"]       = (df["close"] > df["open"]).astype(float)
        out["is_doji"]          = (out["body_ratio"] < 0.1).astype(float)
        out["is_engulfing"]     = (
            (body > body.shift(1) * 1.5) &
            (out["is_bullish"] != out["is_bullish"].shift(1))
        ).astype(float)
        out["candle_range_norm"] = candle_range / candle_range.rolling(20, min_periods=1).mean()

        return out


# ══════════════════════════════════════════════════════════════════════
#  4. Tick-level feature extractor
# ══════════════════════════════════════════════════════════════════════

class TickFeatureExtractor:
    """
    Extracts features from raw tick data.
    These are unique to synthetic indices and carry the highest
    predictive signal for spike probability.
    """

    def __init__(self, cfg: IndexConfig):
        self.cfg = cfg

    def extract(self, ticks: pd.DataFrame) -> pd.DataFrame:
        log.info("Extracting tick-level features...")
        t = ticks.copy()

        # ── Core tick features ────────────────────────────────────────
        t["tick_change"]     = t["price"].diff().fillna(0)
        t["tick_change_abs"] = t["tick_change"].abs()

        # ── Ticks since last spike (geometric dist feature) ───────────
        t["ticks_since_spike"] = self._ticks_since_spike(t["is_spike"])

        # ── Geometric probability: P(spike at tick t | no spike for k ticks)
        # P = 1 - (1 - 1/λ)^k  where λ = avg ticks between spikes
        lam = self.cfg.avg_ticks_between_spikes
        t["spike_geometric_prob"] = 1 - (1 - 1 / lam) ** t["ticks_since_spike"].clip(upper=lam * 5)

        # ── Spike magnitude history ───────────────────────────────────
        # Last 5 spike magnitudes (regime detection)
        spike_mags = t.loc[t["is_spike"], "spike_magnitude"]
        t["last_spike_magnitude"]  = spike_mags.reindex(t.index).ffill().fillna(0)
        t["spike_mag_5_mean"]      = (
            spike_mags.reindex(t.index)
            .rolling(5, min_periods=1)
            .mean()
            .ffill()
            .fillna(0)
        )
        t["spike_mag_rolling_std"] = (
            spike_mags.reindex(t.index)
            .rolling(10, min_periods=1)
            .std()
            .ffill()
            .fillna(0)
        )

        # ── Tick velocity (rate of price change) ─────────────────────
        for window in [10, 30, 100]:
            t[f"tick_velocity_{window}"]     = t["tick_change"].rolling(window, min_periods=1).mean()
            t[f"tick_velocity_abs_{window}"] = t["tick_change_abs"].rolling(window, min_periods=1).mean()
            t[f"tick_std_{window}"]          = t["tick_change"].rolling(window, min_periods=1).std().fillna(0)

        # ── Tick pressure state (directional dominance) ───────────────
        # Ratio of up-ticks to down-ticks in recent window
        up_ticks   = (t["tick_change"] > 0).astype(float)
        down_ticks = (t["tick_change"] < 0).astype(float)
        for window in [20, 50, 100]:
            sum_up   = up_ticks.rolling(window, min_periods=1).sum()
            sum_down = down_ticks.rolling(window, min_periods=1).sum()
            t[f"tick_pressure_{window}"] = (sum_up - sum_down) / window

        # ── Micro RSI on tick prices ──────────────────────────────────
        for period in [10, 20, 50]:
            t[f"micro_rsi_{period}"] = Indicators.rsi(t["price"], period)

        # ── RSI exhaustion flags ──────────────────────────────────────
        low_th  = self.cfg.micro_rsi_exhaustion_low
        high_th = self.cfg.micro_rsi_exhaustion_high
        t["rsi_exhaustion_low"]  = (t["micro_rsi_20"] < low_th).astype(float)
        t["rsi_exhaustion_high"] = (t["micro_rsi_20"] > high_th).astype(float)

        # ── Tick acceleration (second derivative) ─────────────────────
        t["tick_accel_10"] = t["tick_velocity_10"].diff(5).fillna(0)
        t["tick_accel_30"] = t["tick_velocity_30"].diff(10).fillna(0)

        # ── TSSL — Tick Spread & Streak Level ─────────────────────────
        # Measures persistent directional streaks in tick data
        t["tssl_score"] = self._tssl(t["tick_change"], window=50)

        # ── Compression ratio on tick scale ──────────────────────────
        tick_std = t["tick_change_abs"].rolling(20, min_periods=5).std().fillna(0)
        tick_std_min = tick_std.rolling(100, min_periods=1).min()
        tick_std_max = tick_std.rolling(100, min_periods=1).max()
        t["tick_compression"] = (tick_std - tick_std_min) / (tick_std_max - tick_std_min + 1e-9)

        log.info(f"  Tick features: {len([c for c in t.columns if c not in ticks.columns])} new columns")
        return t

    @staticmethod
    def _ticks_since_spike(is_spike: pd.Series) -> pd.Series:
        """Count of ticks since the last spike occurred."""
        result = np.zeros(len(is_spike), dtype=np.float32)
        counter = 0
        for i, spike in enumerate(is_spike.values):
            if spike:
                counter = 0
            else:
                counter += 1
            result[i] = counter
        return pd.Series(result, index=is_spike.index)

    @staticmethod
    def _tssl(tick_change: pd.Series, window: int = 50) -> pd.Series:
        """
        TSSL — Tick Spread & Streak Level.
        Measures the persistence and intensity of directional
        tick sequences. High absolute value = strong streak.
        Positive = bullish streak, negative = bearish streak.
        """
        direction = np.sign(tick_change.values)
        result = np.zeros(len(tick_change), dtype=np.float32)
        arr = tick_change.values

        for i in range(window, len(arr)):
            window_dir = direction[i - window:i]
            window_chg = np.abs(arr[i - window:i])

            streak = 0
            streak_strength = 0.0
            current_dir = window_dir[-1]
            for j in range(len(window_dir) - 1, -1, -1):
                if window_dir[j] == current_dir:
                    streak += 1
                    streak_strength += window_chg[j]
                else:
                    break

            result[i] = current_dir * (streak / window) * (
                streak_strength / (window_chg.mean() + 1e-9)
            )

        return pd.Series(result, index=tick_change.index)


# ══════════════════════════════════════════════════════════════════════
#  5. Candle feature extractor (multi-timeframe)
# ══════════════════════════════════════════════════════════════════════

class CandleFeatureExtractor:
    """
    Builds the full indicator suite on OHLCV candles for each timeframe.
    Features are later merged back onto the tick frame by forward-fill.
    """

    def __init__(self, cfg: IndexConfig):
        self.cfg = cfg

    def extract(self, candles: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        prefix = timeframe.lower() + "_"
        df = candles.copy()
        ind = Indicators()
        out = pd.DataFrame(index=df.index)

        # ── Price ─────────────────────────────────────────────────────
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        open_ = df["open"]

        # ── EMA stack ─────────────────────────────────────────────────
        for period in [9, 21, 50, 100, 200]:
            out[f"{prefix}ema{period}"] = ind.ema(close, period)

        # EMA relationships
        ema9  = out[f"{prefix}ema9"]
        ema21 = out[f"{prefix}ema21"]
        ema50 = out[f"{prefix}ema50"]
        out[f"{prefix}ema9_above_21"]  = (ema9 > ema21).astype(float)
        out[f"{prefix}ema21_above_50"] = (ema21 > ema50).astype(float)
        out[f"{prefix}ema_spread_9_21"]  = (ema9 - ema21) / close
        out[f"{prefix}ema_spread_21_50"] = (ema21 - ema50) / close

        # Hull MA
        out[f"{prefix}hull_ma_20"] = ind.hull_ma(close, 20)
        out[f"{prefix}price_vs_hull"] = (close - out[f"{prefix}hull_ma_20"]) / close

        # ── RSI ───────────────────────────────────────────────────────
        for period in [7, 14, 21]:
            out[f"{prefix}rsi{period}"] = ind.rsi(close, period)

        rsi14 = out[f"{prefix}rsi14"]
        out[f"{prefix}rsi_divergence"] = ind.rsi_divergence(close, rsi14, lookback=20)
        out[f"{prefix}stoch_rsi"]      = ind.stoch_rsi(close)

        # RSI distance from extremes
        out[f"{prefix}rsi14_dist_30"] = (rsi14 - 30).clip(lower=0)   # how far above oversold
        out[f"{prefix}rsi14_dist_70"] = (70 - rsi14).clip(lower=0)   # how far below overbought

        # ── MACD ──────────────────────────────────────────────────────
        macd_line, signal_line, hist = ind.macd(close)
        out[f"{prefix}macd"]         = macd_line
        out[f"{prefix}macd_signal"]  = signal_line
        out[f"{prefix}macd_hist"]    = hist
        out[f"{prefix}macd_cross"]   = np.sign(macd_line - signal_line)
        out[f"{prefix}macd_above_zero"] = (macd_line > 0).astype(float)

        # ── ATR & Volatility ─────────────────────────────────────────
        atr_vals = ind.atr(high, low, close, 14)
        out[f"{prefix}atr14"] = atr_vals
        out[f"{prefix}atr_pct"] = atr_vals / close          # normalised
        out[f"{prefix}atr_ratio"] = (                        # vs longer-term ATR
            atr_vals / ind.atr(high, low, close, 50).replace(0, np.nan)
        )

        # ── Bollinger Bands ───────────────────────────────────────────
        bb_upper, bb_mid, bb_lower, bb_width, bb_pct = ind.bollinger_bands(close)
        out[f"{prefix}bb_width"]   = bb_width
        out[f"{prefix}bb_pct"]     = bb_pct
        out[f"{prefix}bb_squeeze"] = (bb_width < bb_width.rolling(50, min_periods=1).quantile(0.2)).astype(float)
        out[f"{prefix}price_vs_bb_upper"] = (close - bb_upper) / close
        out[f"{prefix}price_vs_bb_lower"] = (close - bb_lower) / close

        # ── Compression Ratio (V4 core feature) ─────────────────────
        out[f"{prefix}compression_ratio"] = ind.compression_ratio(atr_vals, bb_width)
        out[f"{prefix}is_compressed"] = (
            out[f"{prefix}compression_ratio"] < self.cfg.compression_ratio_threshold
        ).astype(float)

        # ── ADX / Trend Strength ──────────────────────────────────────
        adx_vals, plus_di, minus_di = ind.adx(high, low, close)
        out[f"{prefix}adx"]      = adx_vals
        out[f"{prefix}plus_di"]  = plus_di
        out[f"{prefix}minus_di"] = minus_di
        out[f"{prefix}di_diff"]  = plus_di - minus_di
        out[f"{prefix}strong_trend"] = (adx_vals > 25).astype(float)

        # ── EMA Slope ─────────────────────────────────────────────────
        out[f"{prefix}ema21_slope"] = ind.linear_slope(ema21, 10)
        out[f"{prefix}ema50_slope"] = ind.linear_slope(ema50, 20)

        # ── ROC & Momentum ────────────────────────────────────────────
        out[f"{prefix}roc10"]  = ind.rate_of_change(close, 10)
        out[f"{prefix}roc20"]  = ind.rate_of_change(close, 20)
        out[f"{prefix}mom10"]  = ind.momentum(close, 10)

        # ── Candle pattern features ───────────────────────────────────
        candle_feats = ind.candle_features(df)
        for col in candle_feats.columns:
            out[f"{prefix}{col}"] = candle_feats[col]

        # ── Streak patterns (H1 streak for Sniper brain) ─────────────
        out[f"{prefix}bullish_streak"] = self._streak(df["close"] > df["open"], True)
        out[f"{prefix}bearish_streak"] = self._streak(df["close"] > df["open"], False)

        # ── H1 Price position (V4 feature: where is price in H1 range) ──
        h1_high_20 = high.rolling(20, min_periods=1).max()
        h1_low_20  = low.rolling(20, min_periods=1).min()
        out[f"{prefix}price_position_20"] = (close - h1_low_20) / (h1_high_20 - h1_low_20 + 1e-9)

        # ── Spike-enriched candle features ────────────────────────────
        if "spike_count" in df.columns:
            out[f"{prefix}spike_count"]   = df["spike_count"]
            out[f"{prefix}spike_density"] = df["spike_count"] / df["volume"].replace(0, np.nan)
            out[f"{prefix}spike_mag_sum"] = df["spike_mag_sum"]

        log.info(f"  {timeframe}: {len(out.columns)} candle features")
        return out

    @staticmethod
    def _streak(condition: pd.Series, value: bool) -> pd.Series:
        """Count consecutive candles where condition == value."""
        result = np.zeros(len(condition), dtype=np.float32)
        count = 0
        for i, val in enumerate(condition.values):
            if val == value:
                count += 1
            else:
                count = 0
            result[i] = count
        return pd.Series(result, index=condition.index)


# ══════════════════════════════════════════════════════════════════════
#  6. Inter-timeframe feature merger
# ══════════════════════════════════════════════════════════════════════

class MultiTimeframeMerger:
    """
    Merges M1/M5/H1/H4 candle features back onto the tick frame.
    Uses forward-fill so each tick inherits the latest candle state.
    """

    def merge(
        self,
        ticks: pd.DataFrame,
        candle_features: dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        log.info("Merging multi-timeframe features onto tick frame...")
        result = ticks.copy()
        result.index = pd.to_datetime(result["timestamp"])

        for tf, feat_df in candle_features.items():
            feat_reindexed = feat_df.reindex(
                result.index.union(feat_df.index)
            ).ffill().reindex(result.index)

            for col in feat_df.columns:
                result[col] = feat_reindexed[col].values

        log.info(f"  Merged frame: {result.shape[1]} total columns")
        return result.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════
#  7. Target label builder
# ══════════════════════════════════════════════════════════════════════

class LabelBuilder:
    """
    Creates multiple target variables for different model objectives.
    """

    def __init__(self, cfg: IndexConfig):
        self.cfg = cfg

    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        log.info("Building target labels...")
        df = df.copy()

        spike_col = df["is_spike"].values
        price_col = df["price"].values
        n = len(df)

        # ── Label 1: Will there be a spike in next N ticks? ──────────
        for horizon in [10, 30, 50, 100, 200]:
            arr = np.zeros(n, dtype=np.float32)
            for i in range(n - horizon):
                arr[i] = float(spike_col[i + 1:i + horizon + 1].any())
            df[f"spike_in_{horizon}t"] = arr

        # ── Label 2: Ticks to next spike (regression) ─────────────────
        ticks_to_spike = np.full(n, np.nan)
        next_spike_idx = n
        for i in range(n - 1, -1, -1):
            if spike_col[i]:
                next_spike_idx = i
            ticks_to_spike[i] = next_spike_idx - i
        df["ticks_to_next_spike"] = ticks_to_spike

        # ── Label 3: Price direction in next 5/10/20 candle ticks ────
        for horizon in [5, 10, 20]:
            future_price = pd.Series(price_col).shift(-horizon)
            df[f"price_direction_{horizon}t"] = np.sign(future_price.values - price_col)

        # ── Label 4: Expected return (magnitude × direction) ─────────
        for horizon in [10, 30]:
            future_price = pd.Series(price_col).shift(-horizon)
            df[f"return_{horizon}t"] = (future_price.values - price_col) / (price_col + 1e-9)

        # ── Label 5: Multi-class spike quality ────────────────────────
        # 0 = no spike soon, 1 = weak spike, 2 = strong spike
        arr = np.zeros(n, dtype=np.int8)
        avg_mag = self.cfg.typical_spike_magnitude
        for i in range(n - 100):
            next_100 = spike_col[i + 1:i + 101]
            if next_100.any():
                first_idx = np.argmax(next_100)
                mag = df["spike_magnitude"].iloc[i + 1 + first_idx]
                if mag > avg_mag * 1.3:
                    arr[i] = 2   # strong spike
                else:
                    arr[i] = 1   # weak spike
        df["spike_quality"] = arr

        log.info(f"  Labels built: spike_in_N, ticks_to_next_spike, price_direction, return, spike_quality")
        return df


# ══════════════════════════════════════════════════════════════════════
#  8. Feature selector & cleaner
# ══════════════════════════════════════════════════════════════════════

class FeatureCleaner:
    """
    Handles NaN imputation, infinite values, low-variance drop,
    and correlation-based deduplication.
    """

    def __init__(
        self,
        variance_threshold: float = 0.001,
        correlation_threshold: float = 0.97,
        nan_fraction_max: float = 0.3,
    ):
        self.var_thresh = variance_threshold
        self.corr_thresh = correlation_threshold
        self.nan_max = nan_fraction_max
        self.dropped_cols: list[str] = []
        self.feature_cols: list[str] = []

    def fit_transform(self, df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
        log.info(f"Cleaning features: {len(feature_cols)} input columns")
        cols = list(feature_cols)

        # ── Drop columns with too many NaNs ───────────────────────────
        nan_fracs = df[cols].isna().mean()
        high_nan = nan_fracs[nan_fracs > self.nan_max].index.tolist()
        if high_nan:
            log.info(f"  Dropping {len(high_nan)} cols with >{self.nan_max*100:.0f}% NaN")
            cols = [c for c in cols if c not in high_nan]
            self.dropped_cols.extend(high_nan)

        # ── Impute remaining NaNs ─────────────────────────────────────
        df[cols] = df[cols].replace([np.inf, -np.inf], np.nan)
        df[cols] = df[cols].ffill().bfill().fillna(0)

        # ── Drop near-zero variance ───────────────────────────────────
        variances = df[cols].var()
        low_var = variances[variances < self.var_thresh].index.tolist()
        if low_var:
            log.info(f"  Dropping {len(low_var)} near-zero variance cols")
            cols = [c for c in cols if c not in low_var]
            self.dropped_cols.extend(low_var)

        # ── Correlation deduplication ─────────────────────────────────
        corr_matrix = df[cols].corr().abs()
        upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))
        corr_drop = [col for col in upper.columns if (upper[col] > self.corr_thresh).any()]
        if corr_drop:
            log.info(f"  Dropping {len(corr_drop)} highly correlated cols (r>{self.corr_thresh})")
            cols = [c for c in cols if c not in corr_drop]
            self.dropped_cols.extend(corr_drop)

        self.feature_cols = cols
        log.info(f"  Final feature count: {len(cols)}")
        return df

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df[self.feature_cols] = df[self.feature_cols].replace([np.inf, -np.inf], np.nan)
        df[self.feature_cols] = df[self.feature_cols].ffill().bfill().fillna(0)
        return df


# ══════════════════════════════════════════════════════════════════════
#  9. Walk-forward splitter
# ══════════════════════════════════════════════════════════════════════

class WalkForwardSplitter:
    """
    Produces non-overlapping train/validation/test splits
    that respect time ordering (no lookahead leakage).

    Structure per fold:
        [----train----][--val--][test]
    """

    def __init__(
        self,
        n_folds: int = 5,
        train_frac: float = 0.70,
        val_frac: float = 0.15,
        # test_frac = 1 - train - val = 0.15
        gap_ticks: int = 500,   # gap between train end and val start (avoids lookahead)
    ):
        self.n_folds   = n_folds
        self.train_frac = train_frac
        self.val_frac   = val_frac
        self.gap        = gap_ticks

    def split(self, df: pd.DataFrame):
        n = len(df)
        fold_size = n // self.n_folds

        for fold in range(self.n_folds):
            start = fold * fold_size
            end   = start + fold_size if fold < self.n_folds - 1 else n

            fold_df = df.iloc[start:end]
            m = len(fold_df)

            train_end = int(m * self.train_frac)
            val_end   = int(m * (self.train_frac + self.val_frac))

            train = fold_df.iloc[:train_end - self.gap]
            val   = fold_df.iloc[train_end:val_end - self.gap]
            test  = fold_df.iloc[val_end:]

            yield fold + 1, train, val, test


# ══════════════════════════════════════════════════════════════════════
#  10. Feature scaler
# ══════════════════════════════════════════════════════════════════════

class FeatureScaler:
    """
    RobustScaler (median/IQR) — appropriate for financial data
    with heavy tails and outliers from spikes.
    """

    def __init__(self):
        self.scaler = RobustScaler()

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        return self.scaler.fit_transform(X)

    def transform(self, X: np.ndarray) -> np.ndarray:
        return self.scaler.transform(X)

    def inverse_transform(self, X: np.ndarray) -> np.ndarray:
        return self.scaler.inverse_transform(X)


# ══════════════════════════════════════════════════════════════════════
#  11. Feature importance report
# ══════════════════════════════════════════════════════════════════════

def feature_importance_report(
    feature_cols: list[str],
    X: np.ndarray,
    y: np.ndarray,
) -> pd.DataFrame:
    """
    Computes mutual information and variance as quick importance proxies.
    Run this before training to sanity-check signal quality.
    """
    from sklearn.feature_selection import mutual_info_classif

    log.info("Computing feature importance proxies...")
    y_int = y.astype(int)

    mi_scores = mutual_info_classif(X, y_int, discrete_features=False, random_state=42)
    variances  = X.var(axis=0)

    df = pd.DataFrame({
        "feature": feature_cols,
        "mutual_info": mi_scores,
        "variance": variances,
    }).sort_values("mutual_info", ascending=False).reset_index(drop=True)

    df["mi_rank"] = df["mutual_info"].rank(ascending=False).astype(int)
    return df


# ══════════════════════════════════════════════════════════════════════
#  12. Master pipeline
# ══════════════════════════════════════════════════════════════════════

class CrashBoomPipeline:
    """
    Orchestrates the full feature engineering pipeline:

    Ticks → spike labelling → tick features → candles (5 TFs)
         → candle features → multi-TF merge → target labels
         → feature cleaning → walk-forward splits → scaled arrays
    """

    def __init__(self, symbol: str):
        assert symbol in INDEX_CONFIGS, f"Unknown symbol: {symbol}"
        self.symbol  = symbol
        self.cfg     = INDEX_CONFIGS[symbol]
        self.loader  = DataLoader()
        self.builder = CandleBuilder()
        self.tick_fe = TickFeatureExtractor(self.cfg)
        self.candle_fe = CandleFeatureExtractor(self.cfg)
        self.merger  = MultiTimeframeMerger()
        self.labeller = LabelBuilder(self.cfg)
        self.cleaner = FeatureCleaner()
        self.splitter = WalkForwardSplitter()
        self.scaler  = FeatureScaler()

    # ── Main entry points ─────────────────────────────────────────────

    def run_from_csv(self, path: str, target: str = "spike_in_50t") -> dict:
        ticks = self.loader.load_csv(path, self.symbol)
        return self._run(ticks, target)

    def run_synthetic(self, n_ticks: int = 50_000, target: str = "spike_in_50t") -> dict:
        ticks = self.loader.generate_synthetic(self.symbol, n_ticks)
        return self._run(ticks, target)

    def _run(self, ticks: pd.DataFrame, target: str) -> dict:
        log.info(f"\n{'='*60}")
        log.info(f"Pipeline: {self.symbol} | target={target}")
        log.info(f"{'='*60}")

        # Step 1: Tick features
        ticks = self.tick_fe.extract(ticks)

        # Step 2: Build candles
        candles = self.builder.build(ticks)

        # Step 3: Candle features per timeframe
        candle_feats = {}
        for tf, ohlcv in candles.items():
            feats = self.candle_fe.extract(ohlcv, tf)
            candle_feats[tf] = feats

        # Step 4: Merge onto tick frame
        merged = self.merger.merge(ticks, candle_feats)

        # Step 5: Target labels
        merged = self.labeller.build(merged)

        # Step 6: Identify feature columns (exclude metadata & targets)
        meta_cols = {
            "timestamp", "price", "symbol", "tick_change",
            "is_spike", "spike_magnitude",
        }
        target_cols = {
            c for c in merged.columns
            if c.startswith("spike_in_") or
               c.startswith("price_direction_") or
               c.startswith("return_") or
               c in {"ticks_to_next_spike", "spike_quality"}
        }
        feature_cols = [
            c for c in merged.columns
            if c not in meta_cols and c not in target_cols
        ]

        # Step 7: Clean features
        merged = self.cleaner.fit_transform(merged, feature_cols)
        feature_cols = self.cleaner.feature_cols

        # Step 8: Remove rows where target is NaN
        merged = merged.dropna(subset=[target]).reset_index(drop=True)

        log.info(f"\nFinal dataset: {len(merged):,} rows × {len(feature_cols)} features")
        log.info(f"Target '{target}' distribution:\n{merged[target].value_counts().to_string()}")

        return {
            "df": merged,
            "feature_cols": feature_cols,
            "target": target,
            "cfg": self.cfg,
            "cleaner": self.cleaner,
            "scaler": self.scaler,
            "splitter": self.splitter,
        }

    def get_train_arrays(
        self,
        result: dict,
        fold: int = 1,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Returns (X_train, y_train, X_val, y_val, X_test, y_test)
        for the specified walk-forward fold.
        Applies RobustScaler fitted on training data only.
        """
        df = result["df"]
        feature_cols = result["feature_cols"]
        target = result["target"]

        folds = list(result["splitter"].split(df))
        fold_num, train, val, test = folds[fold - 1]

        X_train = train[feature_cols].values
        y_train = train[target].values
        X_val   = val[feature_cols].values
        y_val   = val[target].values
        X_test  = test[feature_cols].values
        y_test  = test[target].values

        # Fit scaler on train only
        X_train = self.scaler.fit_transform(X_train)
        X_val   = self.scaler.transform(X_val)
        X_test  = self.scaler.transform(X_test)

        log.info(f"Fold {fold_num}: train={X_train.shape} val={X_val.shape} test={X_test.shape}")
        return X_train, y_train, X_val, y_val, X_test, y_test


# ══════════════════════════════════════════════════════════════════════
#  CLI / demo
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import time

    symbol = sys.argv[1] if len(sys.argv) > 1 else "Boom500"
    target = sys.argv[2] if len(sys.argv) > 2 else "spike_in_50t"
    n_ticks = int(sys.argv[3]) if len(sys.argv) > 3 else 30_000

    t0 = time.time()

    pipe = CrashBoomPipeline(symbol)
    result = pipe.run_synthetic(n_ticks=n_ticks, target=target)

    # ── Feature importance report ─────────────────────────────────────
    df = result["df"]
    feature_cols = result["feature_cols"]
    X = df[feature_cols].values
    y = df[target].values

    importance = feature_importance_report(feature_cols, X, y.astype(int))

    print(f"\n{'='*60}")
    print(f"Top 30 features by mutual information — {symbol}")
    print(f"{'='*60}")
    print(importance.head(30).to_string(index=False))

    # ── Walk-forward fold 1 ───────────────────────────────────────────
    X_train, y_train, X_val, y_val, X_test, y_test = pipe.get_train_arrays(result, fold=1)

    print(f"\n{'='*60}")
    print(f"Walk-forward fold 1 arrays ready:")
    print(f"  X_train: {X_train.shape}  y_train pos rate: {y_train.mean():.3f}")
    print(f"  X_val:   {X_val.shape}    y_val pos rate:   {y_val.mean():.3f}")
    print(f"  X_test:  {X_test.shape}   y_test pos rate:  {y_test.mean():.3f}")
    print(f"\nPipeline completed in {time.time()-t0:.1f}s")
    print(f"{'='*60}")

    # ── Optional: save feature matrix ────────────────────────────────
    out_path = f"/mnt/user-data/outputs/{symbol}_features.parquet"
    df.to_csv(out_path.replace(".parquet",".csv"), index=False)
    log.info(f"Feature matrix saved: {out_path}")
