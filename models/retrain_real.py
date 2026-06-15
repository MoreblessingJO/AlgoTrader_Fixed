#!/usr/bin/env python3
"""
models/retrain_real.py
Orchestrates: fetch real Deriv tick data → retrain all CB models.
Run this on the server after deploying fetch_deriv_ticks.py.

Usage:
    python models/retrain_real.py                    # fetch + retrain all
    python models/retrain_real.py --skip-fetch       # retrain only (data already downloaded)
    python models/retrain_real.py --symbol BOOM500   # single symbol
    python models/retrain_real.py --ticks 300000     # more ticks = better models
"""

import sys, os, argparse, logging, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("RetrainReal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ticks")

SYMBOLS_CB = [
    ("BOOM300N",  "Boom300"),
    ("BOOM500",   "Boom500"),
    ("BOOM600",   "Boom600"),
    ("BOOM900",   "Boom900"),
    ("BOOM1000",  "Boom1000"),
    ("CRASH300N", "Crash300"),
    ("CRASH500",  "Crash500"),
    ("CRASH600",  "Crash600"),
    ("CRASH900",  "Crash900"),
    ("CRASH1000", "Crash1000"),
]


def run(cmd: list, desc: str):
    log.info(f">>> {desc}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        log.error(f"FAILED: {desc} (exit {result.returncode})")
        return False
    return True


def fetch_data(symbols_deriv: list, ticks: int):
    log.info("=" * 60)
    log.info("STEP 1: Fetching real Deriv tick data")
    log.info("=" * 60)

    fetcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "fetch_deriv_ticks.py")

    for deriv_sym in symbols_deriv:
        run(
            [sys.executable, fetcher, "--symbol", deriv_sym, "--ticks", str(ticks)],
            f"Fetch {deriv_sym}",
        )


def retrain_models(symbol_pairs: list):
    log.info("=" * 60)
    log.info("STEP 2: Retraining XGBoost models on real tick data")
    log.info("=" * 60)

    trainer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_cb_model.py")

    for deriv_sym, train_sym in symbol_pairs:
        csv_path = os.path.join(DATA_DIR, f"{deriv_sym}_ticks.csv")

        if not os.path.exists(csv_path):
            log.warning(f"No CSV for {deriv_sym} at {csv_path} — skipping retrain")
            continue

        import pandas as pd
        n_rows = len(pd.read_csv(csv_path))
        log.info(f"[{train_sym}] Training on {n_rows:,} real ticks from {csv_path}")

        run(
            [sys.executable, trainer, "--symbol", train_sym, "--csv", csv_path],
            f"Retrain {train_sym}",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",       default=None, help="Single Deriv symbol (e.g. BOOM500)")
    parser.add_argument("--ticks",        type=int, default=200_000)
    parser.add_argument("--skip-fetch",   action="store_true", help="Skip data fetch (use existing CSVs)")
    args = parser.parse_args()

    if args.symbol:
        pairs = [(p[0], p[1]) for p in SYMBOLS_CB if p[0] == args.symbol]
        if not pairs:
            log.error(f"Unknown symbol: {args.symbol}. Valid: {[p[0] for p in SYMBOLS_CB]}")
            sys.exit(1)
    else:
        pairs = SYMBOLS_CB

    deriv_syms = [p[0] for p in pairs]

    if not args.skip_fetch:
        fetch_data(deriv_syms, args.ticks)

    retrain_models(pairs)

    log.info("=" * 60)
    log.info("All done. Restart the bot to load new models:")
    log.info("  pkill -f bot.py && sleep 3 && nohup python3 bot.py --paper >> logs/bot.log 2>&1 &")
    log.info("=" * 60)
