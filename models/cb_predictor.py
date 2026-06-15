"""
models/cb_predictor.py
======================
Live inference wrapper for the trained CB XGBoost spike predictor.

The live bot calls predict() every 30s cycle with the same features
it already computes for rule-based signals — no extra data fetching.

Usage (in bot.py)
-----------------
    from models.cb_predictor import CBPredictor
    predictor = CBPredictor.load("Boom500")
    conf = predictor.predict(
        geo_prob=0.72, compression=0.31, tssl=0.65,
        rsi14=38.2, rsi7=32.1, ema_spread=-0.42, atr_pct=0.0018,
        bb_width=0.012, bb_pct_b=0.21, h1_rsi=35.0, h1_streak=5,
        price_ema50_dist=-1.8, rsi_div=1.0, macd_hist=-0.31,
    )
    # conf is 0.0–1.0 spike probability
"""

import os, pickle, logging
import numpy as np

log = logging.getLogger("CBPredictor")

MODEL_DIR = os.path.dirname(__file__)

FEATURE_NAMES = [
    "geo_prob", "compression", "tssl", "rsi14", "rsi7",
    "ema_spread", "atr_pct", "bb_width", "bb_pct_b",
    "h1_rsi", "h1_streak", "price_ema50_dist", "rsi_div", "macd_hist",
]


class CBPredictor:
    """
    Wraps a trained XGBoost model for a single Crash/Boom symbol.
    Thread-safe (predict() is stateless).
    """

    def __init__(self, symbol: str, model, scaler, feature_names: list):
        self.symbol        = symbol
        self._model        = model
        self._scaler       = scaler
        self._feature_names = feature_names

    @classmethod
    def load(cls, symbol: str, model_dir: str = MODEL_DIR):
        """
        Load a trained model from disk.
        Returns None (with a warning) if model file doesn't exist yet —
        bot continues with rule-based signals as fallback.
        """
        path = os.path.join(model_dir, f"cb_model_{symbol}.pkl")
        if not os.path.exists(path):
            log.warning(f"No model file for {symbol} at {path} — run train_cb_model.py first")
            return None
        try:
            with open(path, "rb") as f:
                artifact = pickle.load(f)
            log.info(
                f"Loaded CB model: {symbol}  "
                f"AUC={artifact.get('avg_auc',0):.3f}  "
                f"Prec={artifact.get('avg_precision',0):.3f}"
            )
            return cls(
                symbol       = symbol,
                model        = artifact["model"],
                scaler       = artifact["scaler"],
                feature_names= artifact["feature_names"],
            )
        except Exception as e:
            log.error(f"Failed to load model for {symbol}: {e}")
            return None

    def predict(self, **kwargs) -> float:
        """
        Returns spike probability (0.0–1.0) given the feature values
        computed by the live bot for this symbol.

        Accepts keyword arguments matching FEATURE_NAMES.
        Missing features default to 0 (safe fallback).
        """
        vec = np.array(
            [float(kwargs.get(f, 0.0)) for f in self._feature_names],
            dtype=np.float32,
        ).reshape(1, -1)

        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)

        try:
            vec_s = self._scaler.transform(vec)
            prob  = float(self._model.predict_proba(vec_s)[0, 1])
            return prob
        except Exception as e:
            log.warning(f"Predict error ({self.symbol}): {e}")
            return 0.0

    def predict_array(self, feature_array: np.ndarray) -> float:
        """Alternative: pass features as a pre-built numpy array (len=14)."""
        vec = feature_array.reshape(1, -1).astype(np.float32)
        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
        try:
            return float(self._model.predict_proba(self._scaler.transform(vec))[0, 1])
        except Exception as e:
            log.warning(f"Predict error ({self.symbol}): {e}")
            return 0.0


class CBPredictorPool:
    """
    Manages predictors for all Crash/Boom symbols.
    The bot creates one pool at startup; symbols without a model
    transparently fall back to rule-based logic.
    """

    def __init__(self, model_dir: str = MODEL_DIR):
        self._pool: dict[str, CBPredictor] = {}
        self._model_dir = model_dir

    def load_all(self, symbols: list[str]):
        """Attempt to load a model for each symbol. Missing ones are skipped."""
        loaded = 0
        for sym in symbols:
            p = CBPredictor.load(sym, self._model_dir)
            if p is not None:
                self._pool[sym] = p
                loaded += 1
        log.info(f"CBPredictorPool: {loaded}/{len(symbols)} models loaded")
        return self

    def predict(self, symbol: str, **kwargs) -> float | None:
        """
        Returns spike probability for symbol, or None if no model available.
        The bot treats None as "use rule-based logic only".
        """
        predictor = self._pool.get(symbol)
        if predictor is None:
            return None
        return predictor.predict(**kwargs)

    def has_model(self, symbol: str) -> bool:
        return symbol in self._pool

    def __len__(self):
        return len(self._pool)
