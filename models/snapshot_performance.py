#!/usr/bin/env python3
"""
models/snapshot_performance.py
Appends one row to logs/daily_performance.csv after each daily retrain.
Captures: yesterday's trade stats + today's newly trained model AUCs.

Called automatically by models/daily_retrain.sh.

Usage:
    python3 models/snapshot_performance.py --date 2026-06-15
    python3 models/snapshot_performance.py             # defaults to yesterday
"""

import sys, os, argparse, csv, json, pickle, logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("Snapshot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERF_CSV  = os.path.join(_ROOT, "logs", "daily_performance.csv")
MODEL_DIR = os.path.join(_ROOT, "models")

PERF_HEADERS = [
    "date", "trades", "wins", "losses", "win_rate_pct", "total_pnl_usd", "avg_pnl_usd",
    "avg_auc_all", "best_model", "best_auc", "worst_model", "worst_auc",
    "training_ticks_per_symbol", "models_above_065",
]


def get_trade_stats(date_str: str) -> dict:
    try:
        from execution.trade_journal import TradeJournal, DB_PATH
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT result, pnl_usd FROM trades WHERE closed_at LIKE ? AND result != 'OPEN'",
                (f"{date_str}%",),
            ).fetchall()
    except Exception as e:
        log.warning(f"Could not read trade journal: {e}")
        rows = []

    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0,
                "win_rate_pct": 0.0, "total_pnl_usd": 0.0, "avg_pnl_usd": 0.0}

    wins  = [r for r in rows if r[0] == "WIN"]
    pnls  = [r[1] for r in rows if r[1] is not None]
    n     = len(rows)
    return {
        "trades":       n,
        "wins":         len(wins),
        "losses":       n - len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 1),
        "total_pnl_usd": round(sum(pnls), 2),
        "avg_pnl_usd":   round(sum(pnls) / n, 2) if n else 0.0,
    }


def get_model_stats() -> dict:
    aucs = {}
    ticks = 0

    for fname in os.listdir(MODEL_DIR):
        if not fname.startswith("cb_model_") or not fname.endswith(".pkl"):
            continue
        try:
            with open(os.path.join(MODEL_DIR, fname), "rb") as f:
                artifact = pickle.load(f)
            sym = artifact.get("symbol", fname)
            aucs[sym] = round(artifact.get("avg_auc", 0.0), 3)
        except Exception:
            continue

    if not aucs:
        return {}

    avg_auc     = round(sum(aucs.values()) / len(aucs), 3)
    best_sym    = max(aucs, key=aucs.get)
    worst_sym   = min(aucs, key=aucs.get)
    above_065   = sum(1 for a in aucs.values() if a >= 0.65)

    # Get tick count from a sample CSV
    ticks_dir = os.path.join(_ROOT, "data", "ticks")
    if os.path.exists(ticks_dir):
        csv_files = [f for f in os.listdir(ticks_dir) if f.endswith("_ticks.csv")]
        if csv_files:
            sample = os.path.join(ticks_dir, csv_files[0])
            try:
                import pandas as pd
                ticks = len(pd.read_csv(sample))
            except Exception:
                pass

    return {
        "avg_auc_all":               avg_auc,
        "best_model":                best_sym,
        "best_auc":                  aucs[best_sym],
        "worst_model":               worst_sym,
        "worst_auc":                 aucs[worst_sym],
        "training_ticks_per_symbol": ticks,
        "models_above_065":          above_065,
    }


def archive_models(date_str: str):
    """Copy current .pkl files to models/history/<date>_<sym>.pkl"""
    history_dir = os.path.join(MODEL_DIR, "history")
    os.makedirs(history_dir, exist_ok=True)

    import shutil
    archived = 0
    for fname in os.listdir(MODEL_DIR):
        if fname.startswith("cb_model_") and fname.endswith(".pkl"):
            src = os.path.join(MODEL_DIR, fname)
            dst = os.path.join(history_dir, f"{date_str}_{fname}")
            shutil.copy2(src, dst)
            archived += 1

    log.info(f"Archived {archived} model files to models/history/")


def append_model_history(date_str: str, model_stats: dict):
    """Append per-symbol AUC row to logs/model_history.csv"""
    hist_csv = os.path.join(_ROOT, "logs", "model_history.csv")
    headers  = ["date", "symbol", "avg_auc", "training_ticks"]

    rows = []
    for fname in os.listdir(MODEL_DIR):
        if not fname.startswith("cb_model_") or not fname.endswith(".pkl"):
            continue
        try:
            with open(os.path.join(MODEL_DIR, fname), "rb") as f:
                artifact = pickle.load(f)
            rows.append({
                "date":            date_str,
                "symbol":          artifact.get("symbol", fname),
                "avg_auc":         round(artifact.get("avg_auc", 0.0), 4),
                "training_ticks":  model_stats.get("training_ticks_per_symbol", 0),
            })
        except Exception:
            continue

    if not rows:
        return

    write_header = not os.path.exists(hist_csv)
    with open(hist_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            w.writeheader()
        w.writerows(rows)

    log.info(f"Appended {len(rows)} rows to model_history.csv")


def run(date_str: str):
    log.info(f"Snapshot for date: {date_str}")

    trade_stats = get_trade_stats(date_str)
    model_stats = get_model_stats()

    row = {"date": date_str}
    row.update(trade_stats)
    row.update(model_stats)

    # Ensure all columns present
    for h in PERF_HEADERS:
        row.setdefault(h, "")

    write_header = not os.path.exists(PERF_CSV)
    with open(PERF_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PERF_HEADERS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

    log.info(f"Appended to daily_performance.csv: {row}")

    archive_models(date_str)
    append_model_history(date_str, model_stats)

    # Print summary to terminal / cron log
    print(f"\n{'='*55}")
    print(f"  DAILY SNAPSHOT — {date_str}")
    print(f"{'='*55}")
    print(f"  Trades:      {row.get('trades', 0)}  "
          f"(W:{row.get('wins',0)} / L:{row.get('losses',0)})")
    print(f"  Win Rate:    {row.get('win_rate_pct', 0)}%")
    print(f"  PnL:         ${row.get('total_pnl_usd', 0)}")
    print(f"  Avg AUC:     {row.get('avg_auc_all', 'N/A')}")
    print(f"  Best model:  {row.get('best_model','')} AUC={row.get('best_auc','')}")
    print(f"  Worst model: {row.get('worst_model','')} AUC={row.get('worst_auc','')}")
    print(f"  AUC ≥ 0.65:  {row.get('models_above_065', 0)}/10 symbols")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None,
                        help="Date to snapshot (YYYY-MM-DD). Default: yesterday UTC.")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        date_str  = yesterday.strftime("%Y-%m-%d")

    run(date_str)
