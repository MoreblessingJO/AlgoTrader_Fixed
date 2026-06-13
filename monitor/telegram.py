"""
monitor/telegram.py
Telegram alerts: trade open/close, circuit breaker, daily summary.
Non-blocking — failures never stop the trading loop.
"""

import asyncio, logging
import aiohttp
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, MODE

log = logging.getLogger("Telegram")
BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

_MARKET_EMOJI = {
    "crash_boom": "💥",
    "crypto":     "🔷",
    "forex":      "💱",
}
_MODE_TAG = "📋 <i>PAPER</i>" if MODE == "paper" else "🔴 <b>LIVE</b>"


async def _send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug(f"[TG stub] {text[:120]}")
        return
    try:
        url  = f"{BASE}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning(f"Telegram {r.status}: {body[:120]}")
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")


def _fmt_held(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def _fmt_price(price: float) -> str:
    """Auto-precision: large prices (CB) use 2dp, small forex use 5dp."""
    if price >= 100:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.5f}"


async def alert_trade_open(order) -> None:
    mkt   = _MARKET_EMOJI.get(order.market, "📊")
    side  = "🟢 BUY " if order.side == "BUY" else "🔴 SELL"
    risk  = abs(order.fill_price - order.sl)
    rwd   = abs(order.tp - order.fill_price)
    rr    = rwd / risk if risk > 1e-9 else 0

    text = (
        f"📈 {mkt} <b>TRADE OPEN — {order.strategy}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{side}  <b>{order.symbol}</b>  [{order.lot}]\n"
        f"Entry: <code>{_fmt_price(order.fill_price)}</code>\n"
        f"SL:    <code>{_fmt_price(order.sl)}</code>  "
        f"(<i>-{_fmt_price(risk)}</i>)\n"
        f"TP:    <code>{_fmt_price(order.tp)}</code>  "
        f"(<i>+{_fmt_price(rwd)}</i>)\n"
        f"R:R  <b>{rr:.1f} : 1</b>  |  💵 ${order.notional_usd:,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_MODE_TAG}"
    )
    await _send(text)


async def alert_trade_close(order, strategy_stats: dict = None) -> None:
    win   = order.pnl_usd >= 0
    icon  = "✅" if win else "❌"
    label = "WIN 💰" if win else "LOSS 📉"
    mkt   = _MARKET_EMOJI.get(order.market, "📊")
    sign  = "+" if win else ""

    # P&L as % of notional (trade-level return)
    pnl_pct = order.pnl_usd / max(order.notional_usd, 1e-9) * 100

    # Actual R multiple achieved
    risk_pts = abs(order.fill_price - order.sl)
    pnl_pts  = abs(order.exit_price - order.fill_price)
    r_actual = pnl_pts / risk_pts if risk_pts > 1e-9 else 0
    r_sign   = "+" if win else "-"

    held_str = _fmt_held(order.hold_seconds())

    text = (
        f"{icon} {mkt} <b>TRADE CLOSED — {label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{order.strategy}</b>  {order.symbol}  [{order.lot}]\n"
        f"P&L:  <b>{sign}${abs(order.pnl_usd):.2f}</b>  "
        f"(<i>{sign}{abs(pnl_pct):.2f}%</i>)\n"
        f"─────────────────────\n"
        f"Entry: <code>{_fmt_price(order.fill_price)}</code>\n"
        f"Exit:  <code>{_fmt_price(order.exit_price)}</code>  "
        f"[<b>{order.exit_reason}</b>]\n"
        f"⏱ Held: {held_str}  |  "
        f"R: <b>{r_sign}{r_actual:.2f}</b>\n"
    )

    # Running strategy stats
    if strategy_stats:
        s = strategy_stats.get(order.strategy, {})
        t = s.get("trades", 0)
        if t > 0:
            wr   = s["wins"] / t * 100
            pnl  = s["pnl"]
            psign = "+" if pnl >= 0 else ""
            text += (
                f"─────────────────────\n"
                f"📊 <b>{order.strategy}</b>  "
                f"{t} trade{'s' if t!=1 else ''}  |  "
                f"WR <b>{wr:.0f}%</b>  |  "
                f"<b>{psign}${abs(pnl):.2f}</b>\n"
            )

    text += f"━━━━━━━━━━━━━━━━━━━━━\n{_MODE_TAG}"
    await _send(text)


async def alert_circuit_breaker(reason: str, balance: float) -> None:
    text = (
        f"🚨 <b>CIRCUIT BREAKER TRIGGERED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: <b>{reason}</b>\n"
        f"Balance: <b>${balance:,.2f}</b>\n"
        f"⛔ All trading halted — review required.\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n{_MODE_TAG}"
    )
    await _send(text)


async def alert_divergence(strategy: str, live_wr: float, bt_wr: float, trades: int) -> None:
    gap = bt_wr - live_wr
    text = (
        f"⚠️ <b>WIN-RATE DIVERGENCE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy: <b>{strategy}</b>  ({trades} trades)\n"
        f"Live WR:  <b>{live_wr:.1f}%</b>  "
        f"vs  Backtest: <b>{bt_wr:.1f}%</b>\n"
        f"Gap:  <b>-{gap:.1f}pp</b> below target\n"
        f"⚡ Parameter review recommended.\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n{_MODE_TAG}"
    )
    await _send(text)


async def send_daily_report(stats: dict) -> None:
    lines = [f"📅 <b>DAILY REPORT</b>\n━━━━━━━━━━━━━━━━━━━━━"]
    total_trades = 0
    total_pnl    = 0.0
    for market, s in stats.items():
        t   = s.get("trades", 0)
        wr  = s.get("wr", 0.0)
        pnl = s.get("total_pnl", 0.0)
        if t == 0:
            continue
        total_trades += t
        total_pnl    += pnl
        mkt_e = {"Crypto": "🔷", "Crash/Boom": "💥", "Forex": "💱"}.get(market, "📊")
        psign = "+" if pnl >= 0 else ""
        lines.append(
            f"{mkt_e} <b>{market}</b>:  "
            f"{t} trades  |  WR <b>{wr:.0f}%</b>  |  "
            f"<b>{psign}${pnl:.2f}</b>"
        )
    if total_trades == 0:
        lines.append("No trades today.")
    else:
        tsign = "+" if total_pnl >= 0 else ""
        lines.append(
            f"─────────────────────\n"
            f"Total: {total_trades} trades  |  "
            f"<b>{tsign}${total_pnl:.2f}</b>"
        )
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━\n{_MODE_TAG}")
    await _send("\n".join(lines))
