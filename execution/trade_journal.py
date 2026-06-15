"""
execution/trade_journal.py
Persistent trade history — survives bot restarts.

Writes every trade open/close to:
  db/trade_journal.db   (SQLite — queryable, primary store)
  logs/trade_journal.csv (CSV mirror — human-readable, for spreadsheets)

Both files accumulate indefinitely and are never wiped.
"""

import csv
import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("TradeJournal")

_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(_ROOT, "db",   "trade_journal.db")
CSV_PATH = os.path.join(_ROOT, "logs", "trade_journal.csv")

_CSV_HEADERS = [
    "id", "opened_at", "closed_at", "symbol", "market", "strategy",
    "direction", "entry_price", "fill_price", "sl", "tp",
    "exit_price", "pnl_usd", "result", "exit_reason",
    "hold_minutes", "lot", "mode",
]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id           TEXT PRIMARY KEY,
    opened_at    TEXT,
    closed_at    TEXT,
    symbol       TEXT,
    market       TEXT,
    strategy     TEXT,
    direction    TEXT,
    entry_price  REAL,
    fill_price   REAL,
    sl           REAL,
    tp           REAL,
    exit_price   REAL,
    pnl_usd      REAL,
    result       TEXT,
    exit_reason  TEXT,
    hold_minutes REAL,
    lot          TEXT,
    mode         TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbol   ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_opened   ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_strategy ON trades(strategy);
"""


class TradeJournal:

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        os.makedirs(os.path.dirname(DB_PATH),  exist_ok=True)
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(_CREATE_SQL)
        log.info(f"TradeJournal ready — db={DB_PATH}")

    # ── Write on open ─────────────────────────────────────────────

    def record_open(self, order) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO trades
                       (id, opened_at, symbol, market, strategy, direction,
                        entry_price, fill_price, sl, tp, lot, mode, result)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        order.id,
                        order.open_time.isoformat(),
                        order.symbol, order.market, order.strategy, order.side,
                        order.entry_price, order.fill_price, order.sl, order.tp,
                        order.lot, self.mode, "OPEN",
                    ),
                )
        except Exception as e:
            log.error(f"record_open failed: {e}")

    # ── Update on close ───────────────────────────────────────────

    def record_close(self, order) -> None:
        hold_min = round(order.hold_seconds() / 60, 2)
        result   = "WIN" if order.pnl_usd > 0 else "LOSS"

        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """UPDATE trades SET
                           closed_at=?, exit_price=?, pnl_usd=?,
                           result=?, exit_reason=?, hold_minutes=?
                       WHERE id=?""",
                    (
                        order.close_time.isoformat(),
                        order.exit_price, order.pnl_usd,
                        result, order.exit_reason, hold_min,
                        order.id,
                    ),
                )
        except Exception as e:
            log.error(f"record_close DB failed: {e}")

        # Mirror to CSV
        try:
            write_header = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(_CSV_HEADERS)
                w.writerow([
                    order.id,
                    order.open_time.isoformat(),
                    order.close_time.isoformat(),
                    order.symbol, order.market, order.strategy, order.side,
                    order.entry_price, order.fill_price, order.sl, order.tp,
                    order.exit_price, order.pnl_usd,
                    result, order.exit_reason, hold_min,
                    order.lot, self.mode,
                ])
        except Exception as e:
            log.error(f"record_close CSV failed: {e}")

    # ── Query helpers ─────────────────────────────────────────────

    def daily_stats(self, date_str: str = None) -> dict:
        """Return WR and PnL for a given date (YYYY-MM-DD). Default: today."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    """SELECT result, pnl_usd, strategy, symbol
                       FROM trades
                       WHERE closed_at LIKE ? AND result != 'OPEN'""",
                    (f"{date_str}%",),
                ).fetchall()
        except Exception:
            return {}

        if not rows:
            return {"date": date_str, "trades": 0}

        wins      = [r for r in rows if r[0] == "WIN"]
        pnls      = [r[1] for r in rows if r[1] is not None]
        wr        = round(len(wins) / len(rows) * 100, 1) if rows else 0.0
        total_pnl = round(sum(pnls), 2)

        return {
            "date":       date_str,
            "trades":     len(rows),
            "wins":       len(wins),
            "losses":     len(rows) - len(wins),
            "win_rate":   wr,
            "total_pnl":  total_pnl,
            "avg_pnl":    round(total_pnl / len(rows), 2) if rows else 0.0,
        }

    def lifetime_stats(self) -> dict:
        """Overall stats since inception."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT result, pnl_usd FROM trades WHERE result != 'OPEN'"
                ).fetchall()
                first = conn.execute(
                    "SELECT MIN(opened_at) FROM trades"
                ).fetchone()[0]
        except Exception:
            return {}

        if not rows:
            return {"trades": 0}

        wins  = [r for r in rows if r[0] == "WIN"]
        pnls  = [r[1] for r in rows if r[1] is not None]
        return {
            "since":      first,
            "trades":     len(rows),
            "wins":       len(wins),
            "losses":     len(rows) - len(wins),
            "win_rate":   round(len(wins) / len(rows) * 100, 1),
            "total_pnl":  round(sum(pnls), 2),
        }
