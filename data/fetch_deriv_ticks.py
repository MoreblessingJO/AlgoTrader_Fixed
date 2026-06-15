#!/usr/bin/env python3
"""
data/fetch_deriv_ticks.py
Fetches real historical tick data from Deriv WebSocket API for all
Crash/Boom symbols and saves CSVs to data/ticks/<SYMBOL>_ticks.csv.

CSV format: timestamp, price   (matches DataLoader.load_csv)

Usage:
    python data/fetch_deriv_ticks.py                   # all symbols, 200k ticks each
    python data/fetch_deriv_ticks.py --symbol BOOM500 --ticks 100000
    python data/fetch_deriv_ticks.py --ticks 300000    # more data = better models
"""

import asyncio
import json
import os
import sys
import time
import argparse
import logging
import pandas as pd

log = logging.getLogger("DerivFetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

WS_URL     = "wss://ws.binaryws.com/websockets/v3?app_id=1089"
BATCH_SIZE = 5000   # Deriv max per request

SYMBOLS = [
    "BOOM300N", "BOOM500", "BOOM600", "BOOM900", "BOOM1000",
    "CRASH300N", "CRASH500", "CRASH600", "CRASH900", "CRASH1000",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ticks")


async def _send_recv(ws, payload: dict) -> dict:
    import json as _json
    await ws.send(_json.dumps(payload))
    while True:
        raw = await ws.recv()
        msg = _json.loads(raw)
        if msg.get("req_id") == payload.get("req_id"):
            return msg


async def fetch_symbol_async(symbol: str, target_ticks: int) -> pd.DataFrame:
    try:
        import websockets
    except ImportError:
        log.error("websockets not installed — run: pip install websockets")
        raise

    all_prices = []
    all_times  = []
    end_time   = int(time.time())
    req_id     = 1

    log.info(f"[{symbol}] Fetching {target_ticks:,} ticks from Deriv…")

    async with websockets.connect(WS_URL, ping_interval=20, open_timeout=30) as ws:
        while len(all_prices) < target_ticks:
            batch_size = min(BATCH_SIZE, target_ticks - len(all_prices))

            resp = await _send_recv(ws, {
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": batch_size,
                "end": end_time,
                "style": "ticks",
                "req_id": req_id,
            })

            if "error" in resp:
                code = resp["error"].get("code", "")
                msg  = resp["error"].get("message", "")
                log.error(f"[{symbol}] API error {code}: {msg}")
                break

            hist   = resp.get("history", {})
            prices = hist.get("prices", [])
            times  = hist.get("times",  [])

            if not prices:
                log.warning(f"[{symbol}] Empty batch at end={end_time} — stopping")
                break

            # Prepend (going backwards in time)
            all_prices = prices + all_prices
            all_times  = times  + all_times

            oldest = int(times[0])
            end_time = oldest - 1

            log.info(f"[{symbol}] +{len(prices):,} ticks → total {len(all_prices):,}")
            await asyncio.sleep(0.35)   # stay under rate limit

    if not all_prices:
        raise RuntimeError(f"No tick data received for {symbol}")

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(all_times, unit="s", utc=True).tz_localize(None),
        "price":     [float(p) for p in all_prices],
    })
    df = (
        df.drop_duplicates("timestamp")
          .sort_values("timestamp")
          .reset_index(drop=True)
    )
    log.info(
        f"[{symbol}] Done: {len(df):,} ticks | "
        f"{df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}"
    )
    return df


def fetch_all(symbols: list, target_ticks: int, force: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)

    for symbol in symbols:
        out_path = os.path.join(DATA_DIR, f"{symbol}_ticks.csv")

        if not force and os.path.exists(out_path):
            existing = pd.read_csv(out_path)
            if len(existing) >= int(target_ticks * 0.85):
                log.info(f"[{symbol}] Already have {len(existing):,} ticks — skipping (use --force to re-fetch)")
                continue

        try:
            df = asyncio.run(fetch_symbol_async(symbol, target_ticks))
            df.to_csv(out_path, index=False)
            log.info(f"[{symbol}] Saved → {out_path}")
        except Exception as e:
            log.error(f"[{symbol}] Failed: {e}", exc_info=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch real Deriv Crash/Boom tick data")
    parser.add_argument("--symbol", default=None, help="Single symbol (default: all)")
    parser.add_argument("--ticks",  type=int, default=200_000, help="Target ticks per symbol (default: 200000)")
    parser.add_argument("--force",  action="store_true", help="Re-fetch even if CSV already exists")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else SYMBOLS
    fetch_all(symbols, args.ticks, force=args.force)
