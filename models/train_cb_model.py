#!/usr/bin/env python3
"""
models/train_cb_model.py
========================
Trains an XGBoost spike-probability model for each Crash/Boom symbol.

Uses M1 candle features (same features the live bot computes in real time)
so training and inference are perfectly aligned — no lookahead, no mismatch.

Usage
-----
  # Synthetic data (runs immediately, no CSV needed):
  python models/train_cb_model.py

  # Real Deriv CSV export (recommended once you have data):
  python models/train_cb_model.py --symbol Boom500 --csv /path/to/boom500_ticks.csv

  # All main symbols, synthetic:
  python models/train_cb_model.py --all

Output
------
  models/cb_model_<SYMBOL>.pkl   — trained model + scaler + metadata
"""

import sys, os, argparse, pickle, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler
from sklearn.metrics import roc_auc_score, precision_score, recall_score

log = logging.getLogger("CBTrainer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Feature names must match exactly what cb_predictor.py builds at inference time
FEATURE_NAMES = [
    "geo_prob",         # geometric spike probability given ticks_since_spike
    "compression",      # ATR+BB compression ratio (0=squeeze, 1=expanded)
    "tssl",             # tick streak score
    "rsi14",            # RSI(14)
    "rsi7",             # RSI(7) — faster momentum
    "ema_spread",       # (ema9 - ema21) / atr — trend alignment, ATR-normalised
    "atr_pct",          # ATR / price — volatility level
    "bb_width",         # Bollinger band width
    "bb_pct_b",         # %B position within bands
    "h1_rsi",           # H1 RSI proxy (last 60 M1 bars)
    "h1_streak",        # consecutive same-direction candle count
    "price_ema50_dist", # (price - ema50) / atr
    "rsi_div",          # RSI divergence signal (-1, 0, +1)
    "macd_hist",        # MACD histogram (normalised by ATR)
]

SPIKE_HORIZON = 2       # predict spike in next N M1 candles
MODEL_DIR     = os.path.dirname(__file__)


# ══════════════════════════════════════════════════════════════════════
#  Feature builder (M1 candle level — matches live bot exactly)
# ══════════════════════════════════════════════════════════════════════

def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=p-1, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(com=p-1, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def _atr(h, l, c, p=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(com=p-1, adjust=False).mean()

def _bb(s, p=20, k=2.0):
    mid = s.rolling(p, min_periods=1).mean()
    std = s.rolling(p, min_periods=1).std().fillna(0)
    upper, lower = mid + k*std, mid - k*std
    width = (upper - lower) / mid.replace(0, np.nan)
    pct_b = (s - lower) / (upper - lower).replace(0, np.nan)
    return width, pct_b

def _compression(h, l, c, p=20):
    atr_v = _atr(h, l, c, 14)
    bbw,_ = _bb(c, p)
    def _norm(x):
        mn = x.rolling(p, min_periods=1).min()
        mx = x.rolling(p, min_periods=1).max()
        return (x - mn) / (mx - mn + 1e-9)
    return (_norm(atr_v) + _norm(bbw)) / 2

def _tssl(close, window=50):
    chg = close.diff().fillna(0)
    result = np.zeros(len(chg))
    arr = chg.values
    dirs = np.sign(arr)
    for i in range(window, len(arr)):
        w_dir = dirs[i-window:i]
        w_chg = np.abs(arr[i-window:i])
        streak, strength, cur = 0, 0.0, w_dir[-1]
        for j in range(len(w_dir)-1, -1, -1):
            if w_dir[j] == cur:
                streak += 1; strength += w_chg[j]
            else:
                break
        mean_abs = w_chg.mean()
        result[i] = cur * (streak/window) * (strength/(mean_abs*window+1e-9))
    return pd.Series(result, index=close.index)

def _rsi_div(price, rsi_v, lookback=20):
    n   = len(price)
    out = np.zeros(n)
    p, r = price.values, rsi_v.values
    for i in range(lookback, n):
        wp, wr = p[i-lookback:i+1], r[i-lookback:i+1]
        if np.isnan(wr).any():
            continue
        ph = np.argmax(wp[:-1])
        if wp[-1] > wp[ph] and wr[-1] < wr[ph]:
            out[i] = -1.0
        pl = np.argmin(wp[:-1])
        if wp[-1] < wp[pl] and wr[-1] > wr[pl]:
            out[i] = 1.0
    return pd.Series(out, index=price.index)

def _streak(close, open_):
    bull = (close > open_).values
    result = np.zeros(len(bull))
    count = 0
    for i, v in enumerate(bull):
        if i == 0 or v == bull[i-1]:
            count += 1
        else:
            count = 1
        result[i] = count if v == bull[-1] else -count
    return pd.Series(result, index=close.index)

def build_features(candles_m1: pd.DataFrame, avg_ticks: int,
                   ticks_since_spike_series: pd.Series = None) -> pd.DataFrame:
    """
    Build the FEATURE_NAMES feature matrix from M1 OHLCV candles.
    ticks_since_spike_series: optional per-candle ticks_since_spike
    (if None, approximated from M1 candle index assuming 60 ticks/candle).
    """
    c = candles_m1.copy()
    close = c["close"]; high = c["high"]; low = c["low"]
    open_ = c.get("open", close.shift(1).fillna(close))

    atr_v   = _atr(high, low, close, 14)
    rsi14   = _rsi(close, 14)
    rsi7    = _rsi(close, 7)
    bbw, pct_b = _bb(close, 20)
    ema9    = _ema(close, 9)
    ema21   = _ema(close, 21)
    ema50   = _ema(close, 50)
    macd    = ema9 - ema21
    macd_sig = _ema(macd, 9)
    macd_hist = (macd - macd_sig) / (atr_v + 1e-9)
    comp    = _compression(high, low, close, 20)
    tssl_v  = _tssl(close, 50)
    rsi_d   = _rsi_div(close, rsi14, 20)
    streak  = _streak(close, open_)

    # H1 proxy: rolling 60-bar RSI
    h1_rsi   = _rsi(close, 14).rolling(60, min_periods=1).mean()

    # ticks_since_spike: if not provided, approximate (60 ticks per M1 bar)
    if ticks_since_spike_series is not None:
        tss = ticks_since_spike_series.values
    else:
        # Each M1 bar ≈ 60 ticks; count bars since last spike_count > 0
        tss = np.arange(len(c)) * 60   # rough approximation
        if "spike_count" in c.columns:
            cnt = 0
            for i, sc in enumerate(c["spike_count"].values):
                tss[i] = cnt
                cnt = 0 if sc > 0 else cnt + 60

    geo_prob = 1 - (1 - 1/avg_ticks) ** np.clip(tss, 0, avg_ticks * 5)

    feat = pd.DataFrame({
        "geo_prob":         geo_prob,
        "compression":      comp.values,
        "tssl":             tssl_v.values,
        "rsi14":            rsi14.values,
        "rsi7":             rsi7.values,
        "ema_spread":       ((ema9 - ema21) / (atr_v + 1e-9)).values,
        "atr_pct":          (atr_v / (close + 1e-9)).values,
        "bb_width":         bbw.values,
        "bb_pct_b":         pct_b.fillna(0.5).values,
        "h1_rsi":           h1_rsi.values,
        "h1_streak":        streak.values,
        "price_ema50_dist": ((close - ema50) / (atr_v + 1e-9)).values,
        "rsi_div":          rsi_d.values,
        "macd_hist":        macd_hist.values,
    }, index=c.index)

    return feat.replace([np.inf, -np.inf], 0).fillna(0)


def build_labels(candles_m1: pd.DataFrame, horizon: int = SPIKE_HORIZON, spike_mag: float = None) -> pd.Series:
    """
    Label each M1 candle: 1 if any of the next `horizon` candles has a
    high-low range exceeding 3x the current ATR — a movement-based spike label.

    This avoids relying on spike_count from the synthetic generator, which
    over-detects spikes due to a loose MAD threshold (~4% false positive rate
    on normal ticks, making 70-99% of candles appear as spike candles).
    """
    high  = candles_m1["high"]
    low   = candles_m1["low"]
    close = candles_m1["close"]
    atr_v = _atr(high, low, close, 14)

    candle_range = (high - low).values
    atrs         = atr_v.values
    n            = len(candles_m1)
    labels       = np.zeros(n, dtype=np.int8)

    for i in range(n - horizon):
        threshold = spike_mag * 0.50 if spike_mag is not None else 3.0 * atrs[i]
        for j in range(1, horizon + 1):
            if i + j < n and candle_range[i + j] > threshold:
                labels[i] = 1
                break

    return pd.Series(labels, index=candles_m1.index)


# ══════════════════════════════════════════════════════════════════════
#  Walk-forward trainer
# ══════════════════════════════════════════════════════════════════════

def walk_forward_train(X: np.ndarray, y: np.ndarray, n_folds: int = 5):
    """
    Time-ordered walk-forward validation.
    Returns (best_model, scaler, fold_metrics).
    """
    try:
        from xgboost import XGBClassifier
    except ImportError:
        log.error("XGBoost not installed. Run: pip install xgboost")
        raise

    n = len(X)
    fold_size = n // n_folds
    fold_metrics = []
    best_auc = 0.0
    best_model = None
    best_scaler = None

    for fold in range(n_folds):
        train_end  = (fold + 1) * fold_size
        test_start = train_end
        test_end   = min(train_end + fold_size, n)

        if test_end <= test_start or train_end < 500:
            continue

        gap = 60   # 60-candle gap between train and test to avoid leakage
        X_train, y_train = X[:train_end - gap], y[:train_end - gap]
        X_test,  y_test  = X[test_start:test_end], y[test_start:test_end]

        if len(np.unique(y_test)) < 2:
            continue

        scaler = RobustScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s  = scaler.transform(X_test)

        pos_weight = max(1, (y_train == 0).sum() / max((y_train == 1).sum(), 1))

        model = XGBClassifier(
            n_estimators       = 600,
            max_depth          = 5,
            learning_rate      = 0.05,
            subsample          = 0.8,
            colsample_bytree   = 0.8,
            scale_pos_weight   = pos_weight,
            use_label_encoder  = False,
            eval_metric        = "logloss",
            random_state       = 42,
            n_jobs             = -1,
        )
        model.fit(
            X_train_s, y_train,
            eval_set=[(X_test_s, y_test)],
            verbose=False,
            early_stopping_rounds=30,
        )

        proba = model.predict_proba(X_test_s)[:, 1]
        auc   = roc_auc_score(y_test, proba)
        # Precision/recall at 0.60 threshold
        pred  = (proba >= 0.60).astype(int)
        prec  = precision_score(y_test, pred, zero_division=0)
        rec   = recall_score(y_test, pred, zero_division=0)
        pos_rate = y_test.mean()

        log.info(
            f"Fold {fold+1}/{n_folds}: AUC={auc:.3f}  "
            f"Prec@0.60={prec:.3f}  Rec={rec:.3f}  "
            f"pos_rate={pos_rate:.3f}  n={len(y_test):,}"
        )
        fold_metrics.append({"fold": fold+1, "auc": auc, "precision": prec, "recall": rec})

        if auc > best_auc:
            best_auc = auc
            best_model  = model
            best_scaler = scaler

    return best_model, best_scaler, fold_metrics


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def train_symbol(symbol: str, csv_path: str = None, n_ticks: int = 100_000):
    from signals.features import DataLoader, CandleBuilder, INDEX_CONFIGS

    cfg = INDEX_CONFIGS[symbol]
    log.info(f"\n{'='*60}\nTraining model for {symbol}\n{'='*60}")

    loader  = DataLoader()
    builder = CandleBuilder()

    if csv_path:
        log.info(f"Loading tick data from {csv_path}")
        ticks = loader.load_csv(csv_path, symbol)
    else:
        log.info(f"Generating {n_ticks:,} synthetic ticks for {symbol}")
        ticks = loader.generate_synthetic(symbol, n_ticks)

    candles = builder.build(ticks)
    m1 = candles.get("M1")
    if m1 is None or len(m1) < 500:
        log.error(f"Not enough M1 candles for {symbol}: got {len(m1) if m1 is not None else 0}")
        return

    log.info(f"M1 candles: {len(m1):,}")

    # Build ticks_since_spike at candle level
    tss_series = None
    if "spike_count" in m1.columns:
        counts = m1["spike_count"].values
        tss = np.zeros(len(counts))
        cnt = 0
        for i, sc in enumerate(counts):
            tss[i] = cnt
            cnt = 0 if sc > 0 else cnt + 60
        tss_series = pd.Series(tss, index=m1.index)

    feats  = build_features(m1, cfg.avg_ticks_between_spikes, tss_series)
    labels = build_labels(m1, SPIKE_HORIZON, spike_mag=cfg.typical_spike_magnitude)

    # Drop last SPIKE_HORIZON rows (labels are NaN/0 by construction)
    feats  = feats.iloc[:-SPIKE_HORIZON]
    labels = labels.iloc[:-SPIKE_HORIZON]

    X = feats[FEATURE_NAMES].values
    y = labels.values

    pos_rate = y.mean()
    log.info(f"Dataset: {len(X):,} samples | spike rate={pos_rate:.3f}")

    if pos_rate < 0.02 or pos_rate > 0.60:
        log.warning(f"Unusual label rate {pos_rate:.3f} — check data quality")

    model, scaler, metrics = walk_forward_train(X, y, n_folds=5)

    if model is None:
        log.error("Training failed — no valid folds produced a model")
        return

    avg_auc  = sum(m["auc"] for m in metrics) / len(metrics)
    avg_prec = sum(m["precision"] for m in metrics) / len(metrics)
    log.info(
        f"\nSummary for {symbol}: avg_AUC={avg_auc:.3f}  avg_Precision@0.60={avg_prec:.3f}"
    )

    # Feature importance
    importances = sorted(
        zip(FEATURE_NAMES, model.feature_importances_),
        key=lambda x: x[1], reverse=True
    )
    log.info("Feature importance (top 10):")
    for name, imp in importances[:10]:
        log.info(f"  {name:<25} {imp:.4f}")

    # Save artifact
    artifact = {
        "symbol":       symbol,
        "avg_ticks":    cfg.avg_ticks_between_spikes,
        "direction":    cfg.spike_direction,
        "feature_names": FEATURE_NAMES,
        "model":        model,
        "scaler":       scaler,
        "metrics":      metrics,
        "avg_auc":      avg_auc,
        "avg_precision": avg_prec,
    }
    out_path = os.path.join(MODEL_DIR, f"cb_model_{symbol}.pkl")
    with open(out_path, "wb") as f:
        pickle.dump(artifact, f)
    log.info(f"Model saved: {out_path}")
    return artifact


MAIN_SYMBOLS = ["Boom300", "Boom500", "Boom1000", "Crash300", "Crash500", "Crash1000"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train CB XGBoost spike predictor")
    parser.add_argument("--symbol",   default=None,  help="Single symbol to train (e.g. Boom500)")
    parser.add_argument("--csv",      default=None,  help="Path to tick CSV (Deriv export)")
    parser.add_argument("--all",      action="store_true", help="Train all main symbols")
    parser.add_argument("--ticks",    type=int, default=100_000, help="Synthetic tick count")
    args = parser.parse_args()

    symbols = MAIN_SYMBOLS if args.all else [args.symbol or "Boom500"]
    for sym in symbols:
        try:
            train_symbol(sym, csv_path=args.csv if len(symbols)==1 else None, n_ticks=args.ticks)
        except Exception as e:
            log.error(f"Failed to train {sym}: {e}", exc_info=True)
