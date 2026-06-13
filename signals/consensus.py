"""
signals/consensus.py
Weighted signal consensus across all brains.
Resolves conflicts, enforces mutex locks, scores confidence.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import STRATEGY_WEIGHTS, CONSENSUS_THRESHOLD

log = logging.getLogger("Consensus")


@dataclass
class Signal:
    brain: str           # "v4" | "v21" | "sniper" | "fx" | "crypto"
    strategy: str        # "CB-S1" etc
    market: str          # "crash_boom" | "forex" | "crypto"
    symbol: str
    direction: str       # "BUY" | "SELL"
    confidence: float    # 0–1
    entry_price: float
    sl: float
    tp: float
    tp2: float           = 0.0
    trailing_pts: float  = 0.0
    lot: str             = "single"   # "single" | "scalper" | "runner"
    session: str         = ""
    metadata: dict       = field(default_factory=dict)
    timestamp: datetime  = field(default_factory=lambda: datetime.now(timezone.utc))


class ConsensusEngine:
    """
    Aggregates signals from all brains and decides whether to execute.

    Rules:
    1. Sniper always gets priority — if it fires, it executes immediately
    2. Other brains are weighted by their backtest WR
    3. Mutex: CB-S1 and CB-S3 cannot be open on the same symbol simultaneously
    4. Minimum weighted score threshold to execute
    """

    def __init__(self, broker=None):
        self.broker = broker
        self._active_symbols: dict[str, list[str]] = {}  # symbol → [strategy_ids]
        self._pending: list[Signal] = []

    def submit(self, signal: Signal) -> Optional[Signal]:
        """
        Submit a signal for consensus evaluation.
        Returns the approved signal or None if blocked.
        """
        # Sniper always executes immediately — bypass consensus
        if signal.brain == "sniper":
            log.info(f"SNIPER PRIORITY: {signal.strategy} {signal.direction} {signal.symbol}")
            return signal

        # Mutex lock: CB-S1 vs CB-S3 on same symbol
        if not self._mutex_ok(signal):
            log.debug(f"Mutex blocked: {signal.strategy} on {signal.symbol}")
            return None

        # Weighted confidence check — per-strategy weight from backtest WR
        weight = STRATEGY_WEIGHTS.get(signal.strategy, 0.70)
        weighted_score = signal.confidence * weight

        if weighted_score < CONSENSUS_THRESHOLD:
            log.debug(
                f"Consensus below threshold: {signal.strategy} "
                f"score={weighted_score:.2f} < {CONSENSUS_THRESHOLD}"
            )
            return None

        log.info(
            f"SIGNAL APPROVED: {signal.strategy} {signal.direction} {signal.symbol} "
            f"| conf={signal.confidence:.2f} weight={weight} score={weighted_score:.2f}"
        )
        return signal

    def _mutex_ok(self, signal: Signal) -> bool:
        """
        CB-S1 (spike anticipation) and CB-S3 (post-spike reversal)
        cannot both be open on the same symbol — they trade opposite directions.
        """
        if signal.market != "crash_boom":
            return True

        conflict_pairs = {
            "CB-S1": "CB-S3",
            "CB-S3": "CB-S1",
        }
        blocked_by = conflict_pairs.get(signal.strategy)
        if not blocked_by:
            return True

        active = self._active_symbols.get(signal.symbol, [])
        if blocked_by in active:
            log.debug(f"Mutex: {signal.strategy} blocked because {blocked_by} is open on {signal.symbol}")
            return False
        return True

    def register_open(self, symbol: str, strategy: str):
        if symbol not in self._active_symbols:
            self._active_symbols[symbol] = []
        self._active_symbols[symbol].append(strategy)

    def register_close(self, symbol: str, strategy: str):
        if symbol in self._active_symbols:
            self._active_symbols[symbol] = [
                s for s in self._active_symbols[symbol] if s != strategy
            ]

    def active_count(self, symbol: str = None) -> int:
        if symbol:
            return len(self._active_symbols.get(symbol, []))
        return sum(len(v) for v in self._active_symbols.values())
