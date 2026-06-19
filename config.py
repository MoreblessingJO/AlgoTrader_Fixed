"""
config.py — Master configuration for the trading system.
All strategy parameters, symbol lists, thresholds, and
risk settings live here. Edit this file to tune the system.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════
#  System mode
# ══════════════════════════════════════════════════════

MODE = os.getenv("MODE", "paper")          # "paper" | "live"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 8080))

# ══════════════════════════════════════════════════════
#  API credentials
# ══════════════════════════════════════════════════════

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET     = os.getenv("BINANCE_SECRET", "")
BINANCE_TESTNET    = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

DERIV_APP_ID       = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN    = os.getenv("DERIV_API_TOKEN", "")

OANDA_API_KEY      = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID   = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENVIRONMENT  = os.getenv("OANDA_ENVIRONMENT", "practice")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

POSTGRES_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER','trader')}:"
    f"{os.getenv('POSTGRES_PASSWORD','changeme')}@"
    f"{os.getenv('POSTGRES_HOST','localhost')}:"
    f"{os.getenv('POSTGRES_PORT','5432')}/"
    f"{os.getenv('POSTGRES_DB','trading_system')}"
)
REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT     = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)

# ══════════════════════════════════════════════════════
#  Universe of instruments
# ══════════════════════════════════════════════════════

CRYPTO_SYMBOLS = [
    "BTC/USDT:USDT",   # Binance perp
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
]

CRASH_BOOM_SYMBOLS = [
    # (symbol, avg_ticks_between_spikes, direction, typical_spike_magnitude)
    ("BOOM300N",   300, "up",    15.0),
    ("BOOM500",    500, "up",    25.0),
    ("BOOM600",    600, "up",    30.0),
    ("BOOM900",    900, "up",    45.0),
    ("BOOM1000",  1000, "up",    60.0),
    ("CRASH300N",  300, "down",  15.0),
    ("CRASH500",   500, "down",  25.0),
    ("CRASH600",   600, "down",  30.0),
    ("CRASH900",   900, "down",  45.0),
    ("CRASH1000", 1000, "down",  60.0),
]

FOREX_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY",
    "EUR_JPY", "GBP_JPY", "XAU_USD",
]

# ══════════════════════════════════════════════════════
#  Risk management
# ══════════════════════════════════════════════════════

@dataclass
class RiskConfig:
    risk_per_trade_pct: float   = 0.02     # 2% of balance per trade
    max_open_per_market: int    = 3        # max concurrent positions per market
    max_open_total: int         = 15       # hard cap across all markets
    daily_loss_limit_pct: float = 0.06     # 6% daily loss → halt all trading
    max_drawdown_pct: float     = 0.15     # 15% drawdown → emergency stop
    min_rr_ratio: float         = 1.5      # minimum reward:risk to take a trade
    correlation_block: float    = 0.85     # block new trade if corr > this with open position

    # Hard notional cap per trade — prevents tiny SL distances blowing up position size.
    # When qty × price would exceed this, qty is scaled down to hit the cap instead.
    # Set per market so CB (high prices, tight pip stops) and forex (micro lots) are distinct.
    max_notional_usd: dict = field(default_factory=lambda: {
        "crypto":     500,    # 5% of $10k account — tight, crypto moves fast
        "crash_boom": 1000,   # 10% — CB synthetic indices, no margin call risk
        "forex":      2000,   # 20% — leveraged forex, conservative notional
    })

    # Slippage model per market (fraction of price)
    slippage: dict = field(default_factory=lambda: {
        "crypto":     0.0003,
        "crash_boom": 0.0005,
        "forex":      0.00010,
    })

RISK = RiskConfig()

# ══════════════════════════════════════════════════════
#  Crash & Boom strategy parameters
# ══════════════════════════════════════════════════════

@dataclass
class CrashBoomConfig:
    # CB-S1: Apex compression spike hunter
    s1_geometric_prob_threshold: float  = 0.70
    s1_compression_threshold: float     = 0.40
    s1_tssl_threshold: float            = 0.50
    # TP/SL expressed as fraction of the symbol's typical_spike_magnitude.
    # e.g. for BOOM500 (spike=25pts): TP = 25×1.0 = 25pts, SL = 25×0.45 = 11.25pts
    s1_tp_spike_frac: float             = 1.0   # target = full spike magnitude
    s1_sl_spike_frac: float             = 0.45  # stop = 45% of spike mag (2.2:1 RR)

    # CB-S2: Compression trend rider
    s2_ema_fast: int                    = 9
    s2_ema_slow: int                    = 21
    s2_compression_max: float           = 0.55
    s2_tp_spike_frac: float             = 0.80  # TP = 80% of spike mag
    s2_sl_spike_frac: float             = 0.50  # SL = 50% of spike mag (1.6:1 RR)

    # CB-S3: Kingpin divergence + reversal (already uses fixed pts — correct approach)
    s3_h4_rsi_extreme_low: float        = 32.0
    s3_h4_rsi_extreme_high: float       = 68.0
    s3_scalper_tp_pts: float            = 85.0
    s3_runner_trail_pts: float          = 135.0
    s3_scalper_sl_pts: float            = 25.0

    # CB-S4: Sniper exhaustion
    s4_h4_rsi_low: float                = 25.0
    s4_h4_rsi_high: float               = 75.0
    s4_h1_streak_min: int               = 6
    s4_m5_rsi_exhaust_low: float        = 22.0
    s4_m5_rsi_exhaust_high: float       = 78.0
    s4_tp_spike_frac: float             = 1.5   # TP = 1.5× spike mag (runner play)
    s4_sl_spike_frac: float             = 0.45  # SL = 45% of spike mag
    s4_trail_spike_frac: float          = 0.30  # trailing stop = 30% of spike mag

CB = CrashBoomConfig()

# ══════════════════════════════════════════════════════
#  Forex strategy parameters
# ══════════════════════════════════════════════════════

@dataclass
class ForexConfig:
    # Sessions (UTC hours)
    london_open: int            = 7
    london_close: int           = 16
    ny_open: int                = 13
    ny_close: int               = 22
    asian_open: int             = 0
    asian_close: int            = 9

    # FX-S1: London breakout
    s1_asian_range_hours: int   = 6
    s1_breakout_atr_mult: float = 0.10
    s1_tp_atr_mult: float       = 3.0
    s1_sl_atr_mult: float       = 1.5
    s1_news_blackout_min: int   = 30

    # FX-S2: Overlap RSI divergence
    s2_rsi_period: int          = 14
    s2_divergence_lookback: int = 20
    s2_scalper_tp_pips: float   = 18.0
    s2_runner_trail_pips: float = 60.0
    s2_sl_atr_mult: float       = 1.2

    # FX-S3: News compression
    s3_squeeze_ratio: float     = 0.50
    s3_pre_news_min_low: int    = 5
    s3_pre_news_min_high: int   = 25
    s3_tp_atr_mult: float       = 2.0
    s3_sl_atr_mult: float       = 1.0

    # FX-S4: Asian mean reversion
    s4_hurst_threshold: float   = 0.45
    s4_rsi_low: float           = 25.0
    s4_rsi_high: float          = 75.0
    s4_adx_max: float           = 20.0
    s4_tp_atr_mult: float       = 1.5
    s4_sl_atr_mult: float       = 1.0

    # High-impact news events (weekday, hour UTC, minute, name)
    news_events: list = field(default_factory=lambda: [
        (4, 13, 30, "NFP"),
        (1, 13, 30, "CPI"),
        (2, 19,  0, "FOMC"),
        (3,  9, 30, "BOE"),
        (3,  8, 30, "ECB"),
    ])

FX = ForexConfig()

# ══════════════════════════════════════════════════════
#  Crypto strategy parameters
# ══════════════════════════════════════════════════════

@dataclass
class CryptoConfig:
    # Funding rate arb
    funding_long_threshold: float   = 0.001
    funding_short_threshold: float  = -0.0005
    funding_z_score_entry: float    = 2.0
    funding_z_score_exit: float     = 0.5
    funding_max_hold_hours: int     = 72
    funding_history_periods: int    = 90

    # Momentum
    momentum_ema_fast: int          = 9
    momentum_ema_slow: int          = 21
    momentum_rsi_period: int        = 14
    momentum_tp_atr_mult: float     = 3.0
    momentum_sl_atr_mult: float     = 1.5

CR = CryptoConfig()

# ══════════════════════════════════════════════════════
#  Signal consensus weights
#  (weighted by each brain's backtest WR)
# ══════════════════════════════════════════════════════

# Per-strategy weights drawn from backtest WR.
# score = signal_confidence × strategy_weight  must reach CONSENSUS_THRESHOLD.
# Higher-WR strategies tolerate weaker signal conditions; lower-WR strategies
# require stronger setups to compensate for their lower base rate.
STRATEGY_WEIGHTS = {
    "CB-S1": 0.83,
    "CB-S2": 0.81,
    "CB-S3": 0.0,   # disabled — 100% loss rate in paper trading
    "CB-S4": 0.96,
    "FX-S1": 0.71,
    "FX-S2": 0.76,
    "FX-S3": 0.66,
    "FX-S4": 0.73,
    "CR-S1": 0.75,
    "CR-S2": 0.68,
}

CONSENSUS_THRESHOLD = 0.65     # minimum weighted score to execute

# ══════════════════════════════════════════════════════
#  Performance monitoring
# ══════════════════════════════════════════════════════

@dataclass
class MonitorConfig:
    wr_divergence_alert_pp: float   = 8.0     # alert when live WR drops 8pp below backtest
    min_trades_for_alert: int       = 20       # need at least 20 trades to fire divergence alert
    rebalance_check_hours: int      = 24       # check allocation rebalance daily
    retrain_trigger_pp: float       = 12.0    # trigger model retrain at 12pp WR divergence
    dashboard_refresh_seconds: int  = 5

MON = MonitorConfig()

# ══════════════════════════════════════════════════════
#  Backtest WR targets (for divergence monitoring)
# ══════════════════════════════════════════════════════

BACKTEST_WR = {
    "CB-S1": 83.0,
    "CB-S2": 81.0,
    "CB-S3": 92.0,
    "CB-S4": 96.0,
    "FX-S1": 71.0,
    "FX-S2": 76.0,
    "FX-S3": 66.0,
    "FX-S4": 73.0,
    "CR-S1": 75.0,   # funding arb
    "CR-S2": 68.0,   # momentum
}
