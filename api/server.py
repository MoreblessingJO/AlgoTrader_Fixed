"""
api/server.py
FastAPI backend — serves the web dashboard and mobile app.
Exposes REST + WebSocket endpoints connected to the trading engine.

Run standalone: uvicorn api.server:app --host 0.0.0.0 --port 8080 --reload
Or via bot.py which starts it automatically in a background task.
"""

import asyncio, json, time, os, sys
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import jwt

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MODE, BACKTEST_WR

# ── JWT config ────────────────────────────────────────────────────────
JWT_SECRET    = os.getenv("JWT_SECRET", "change-this-in-production-please")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_H  = 24
ADMIN_USER    = os.getenv("DASHBOARD_USER", "admin")
ADMIN_PASS    = os.getenv("DASHBOARD_PASS", "changeme123")

# ── Shared state (injected by bot.py at startup) ──────────────────────
_bot_ref = None

def inject_bot(bot):
    global _bot_ref
    _bot_ref = bot

def get_bot():
    return _bot_ref


# ══════════════════════════════════════════════════════════════════════
#  WebSocket connection manager
# ══════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg  = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# Background task: push live updates every 2 seconds
async def _live_push_loop():
    while True:
        try:
            if manager.active and _bot_ref:
                payload = _build_live_snapshot()
                await manager.broadcast({"type": "snapshot", "data": payload})
        except Exception:
            pass
        await asyncio.sleep(2)


# ══════════════════════════════════════════════════════════════════════
#  App lifespan
# ══════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_live_push_loop())
    yield

app = FastAPI(
    title="Trading System API",
    version="1.0.0",
    description="REST + WebSocket API for the autonomous trading system",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve built React dashboard from /web/dist
web_dist = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
if os.path.exists(web_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(web_dist, "assets")), name="assets")


# ══════════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════════

security = HTTPBearer(auto_error=False)

def make_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_H),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

class LoginRequest(BaseModel):
    username: str
    password: str


# ══════════════════════════════════════════════════════════════════════
#  Data builders (pull from bot state)
# ══════════════════════════════════════════════════════════════════════

def _build_live_snapshot() -> dict:
    bot = _bot_ref
    if not bot:
        return _demo_snapshot()
    risk = bot.risk.summary()
    open_orders = [_order_to_dict(o) for o in bot.broker.open_orders]
    market_stats = {
        m: bot.broker.get_stats(m)
        for m in ["crypto", "crash_boom", "forex"]
    }
    return {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "mode":          MODE,
        "risk":          risk,
        "open_orders":   open_orders,
        "market_stats":  market_stats,
        "strategy_stats": bot._strategy_stats,
        "prices": {
            "BTCUSDT":  bot.binance._prices.get("BTC/USDT:USDT") or bot.binance._prices.get("BTCUSDT", 0),
            "ETHUSDT":  bot.binance._prices.get("ETH/USDT:USDT") or bot.binance._prices.get("ETHUSDT", 0),
            "BOOM500":  bot.deriv.get_price("BOOM500"),
            "CRASH500": bot.deriv.get_price("CRASH500"),
            "EUR_USD":  (lambda c: round((c[0]+c[1])/2, 5) if c else None)(
                            getattr(bot.oanda, "_cache", {}).get("EUR_USD")
                        ),
        },
    }

def _demo_snapshot() -> dict:
    """Returns realistic demo data when bot is not connected."""
    import random, math
    t = time.time()
    btc = 67000 + math.sin(t / 300) * 800
    return {
        "ts":   datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "risk": {
            "balance": 11420.50, "total_return_pct": 14.2,
            "daily_pnl_pct": 1.8, "drawdown_pct": 2.1,
            "open_positions": 2, "halted": False, "halt_reason": "",
        },
        "open_orders": [
            {"id":"a1b2","market":"crash_boom","strategy":"CB-S4","symbol":"Boom_500",
             "side":"BUY","fill_price":8012.40,"sl":7992.40,"tp":8112.40,
             "notional_usd":200,"unrealised_pnl":18.6,"open_time":"2024-01-15T09:22:00Z","lot":"single"},
            {"id":"c3d4","market":"forex","strategy":"FX-S2","symbol":"EUR_USD",
             "side":"SELL","fill_price":1.08420,"sl":1.08580,"tp":1.08060,
             "notional_usd":540,"unrealised_pnl":34.2,"open_time":"2024-01-15T10:05:00Z","lot":"runner"},
        ],
        "market_stats": {
            "crypto":     {"trades": 12, "wr": 75.0, "total_pnl": 380.5,  "avg_pnl": 31.7},
            "crash_boom": {"trades": 18, "wr": 83.3, "total_pnl": 740.2,  "avg_pnl": 41.1},
            "forex":      {"trades": 15, "wr": 73.3, "total_pnl": 299.8,  "avg_pnl": 20.0},
        },
        "strategy_stats": {
            "CB-S1": {"trades": 5,  "wins": 4, "pnl": 185.2},
            "CB-S2": {"trades": 6,  "wins": 5, "pnl": 210.4},
            "CB-S3": {"trades": 4,  "wins": 4, "pnl": 210.6},
            "CB-S4": {"trades": 3,  "wins": 3, "pnl": 134.0},
            "FX-S1": {"trades": 4,  "wins": 3, "pnl": 112.4},
            "FX-S2": {"trades": 6,  "wins": 5, "pnl": 132.6},
            "FX-S3": {"trades": 3,  "wins": 2, "pnl": 28.8},
            "FX-S4": {"trades": 2,  "wins": 1, "pnl": 26.0},
            "CR-S1": {"trades": 7,  "wins": 5, "pnl": 204.1},
            "CR-S2": {"trades": 5,  "wins": 4, "pnl": 176.4},
        },
        "prices": {"BTCUSDT": round(btc,2), "ETHUSDT": round(btc/18.5,2), "Boom_500": 8030.4, "EUR_USD": 1.0847},
    }

def _get_current_price(order) -> float | None:
    """Synchronous cached price lookup for open position P&L."""
    bot = _bot_ref
    if not bot:
        return None
    if order.market == "crypto":
        return bot.binance._prices.get(order.symbol)
    if order.market == "crash_boom":
        return bot.deriv.get_price(order.symbol)
    if order.market == "forex":
        cached = getattr(bot.oanda, "_cache", {}).get(order.symbol)
        if cached:
            bid, ask = cached[0], cached[1]
            return (bid + ask) / 2
    return None

def _order_to_dict(o) -> dict:
    current = _get_current_price(o)
    unrealised = round(o.unrealised_pnl(current), 2) if current else 0.0
    return {
        "id": o.id, "market": o.market, "strategy": o.strategy,
        "symbol": o.symbol, "side": o.side, "fill_price": o.fill_price,
        "sl": o.sl, "tp": o.tp, "notional_usd": o.notional_usd,
        "unrealised_pnl": unrealised,
        "current_price": round(current, 6) if current else None,
        "open_time": o.open_time.isoformat() if o.open_time else "",
        "lot": o.lot,
    }

def _build_trade_history(limit: int = 100, market: str = None, strategy: str = None) -> list:
    if not _bot_ref:
        return _demo_trades()
    orders = _bot_ref.broker.closed_orders
    if market:
        orders = [o for o in orders if o.market == market]
    if strategy:
        orders = [o for o in orders if o.strategy == strategy]
    orders = sorted(orders, key=lambda o: o.close_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [
        {
            "id": o.id, "market": o.market, "strategy": o.strategy,
            "symbol": o.symbol, "side": o.side,
            "entry": o.fill_price, "exit": o.exit_price,
            "pnl": round(o.pnl_usd, 2), "pnl_pct": round(o.pnl_usd / max(o.notional_usd, 1e-9) * 100, 3),
            "exit_reason": o.exit_reason,
            "held_min": round(o.hold_seconds() / 60, 1),
            "open_time": o.open_time.isoformat() if o.open_time else "",
            "close_time": o.close_time.isoformat() if o.close_time else "",
            "lot": o.lot,
        }
        for o in orders[:limit]
    ]

def _demo_trades() -> list:
    import random
    random.seed(99)
    strategies = ["CB-S1","CB-S2","CB-S3","CB-S4","FX-S1","FX-S2","CR-S1","CR-S2"]
    markets    = {"CB": "crash_boom", "FX": "forex", "CR": "crypto"}
    symbols    = {"CB":"Boom_500","FX":"EUR_USD","CR":"BTCUSDT"}
    trades = []
    for i in range(40):
        strat = random.choice(strategies)
        mkt   = markets[strat[:2]]
        sym   = symbols[strat[:2]]
        won   = random.random() < 0.78
        pnl   = round(random.uniform(15, 120) if won else -random.uniform(10, 50), 2)
        entry = {"crash_boom": 8000+random.uniform(-100,100), "forex": 1.085+random.uniform(-0.005,0.005), "crypto": 65000+random.uniform(-2000,2000)}[mkt]
        trades.append({
            "id": f"demo_{i:03d}", "market": mkt, "strategy": strat,
            "symbol": sym, "side": "BUY" if random.random() > 0.5 else "SELL",
            "entry": round(entry,5), "exit": round(entry*(1+pnl/10000),5),
            "pnl": pnl, "pnl_pct": round(pnl/200*100,3),
            "exit_reason": "TP" if won else "SL",
            "held_min": round(random.uniform(2,120),1),
            "open_time": f"2024-01-{15-i//3:02d}T{8+i%12:02d}:00:00Z",
            "close_time": f"2024-01-{15-i//3:02d}T{9+i%12:02d}:00:00Z",
            "lot": random.choice(["single","scalper","runner"]),
        })
    return trades


# ══════════════════════════════════════════════════════════════════════
#  REST Endpoints
# ══════════════════════════════════════════════════════════════════════

# ── Auth ──────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(body: LoginRequest):
    if body.username != ADMIN_USER or body.password != ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = make_token(body.username)
    return {"token": token, "expires_in": JWT_EXPIRE_H * 3600, "user": body.username}

@app.get("/api/auth/me")
async def me(user: str = Depends(verify_token)):
    return {"user": user, "mode": MODE}


# ── Dashboard snapshot ────────────────────────────────────────────────

@app.get("/api/snapshot")
async def snapshot(user: str = Depends(verify_token)):
    return _build_live_snapshot()


# ── Trades ────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_trades(
    limit: int = 100,
    market: Optional[str] = None,
    strategy: Optional[str] = None,
    user: str = Depends(verify_token),
):
    return {"trades": _build_trade_history(limit, market, strategy)}

@app.get("/api/trades/stats")
async def trade_stats(user: str = Depends(verify_token)):
    snap = _build_live_snapshot()
    stats = snap["market_stats"]
    strategy_stats = snap["strategy_stats"]
    # Compute WR divergences
    divergences = []
    for strat, s in strategy_stats.items():
        t = s.get("trades", 0)
        if t >= 10:
            live_wr = s["wins"] / t * 100
            bt_wr   = BACKTEST_WR.get(strat, 0)
            diff    = bt_wr - live_wr
            if abs(diff) > 5:
                divergences.append({
                    "strategy": strat, "live_wr": round(live_wr,1),
                    "bt_wr": bt_wr, "gap": round(diff,1), "trades": t,
                })
    return {
        "market_stats": stats,
        "strategy_stats": strategy_stats,
        "divergences": divergences,
    }


# ── Portfolio / positions ─────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions(user: str = Depends(verify_token)):
    snap = _build_live_snapshot()
    return {
        "open":  snap["open_orders"],
        "count": len(snap["open_orders"]),
        "risk":  snap["risk"],
    }

@app.delete("/api/positions/{order_id}")
async def close_position(order_id: str, user: str = Depends(verify_token)):
    """Manually close a specific position."""
    if not _bot_ref:
        return {"status": "demo_mode", "message": "Connect bot to close positions"}
    for o in _bot_ref.broker.open_orders:
        if o.id == order_id:
            # Get current price
            price_map = {
                "crypto": lambda: _bot_ref.binance._prices.get(o.symbol.replace("/","").replace(":USDT",""), 0),
                "crash_boom": lambda: _bot_ref.deriv.get_price(o.symbol) or 0,
                "forex": lambda: 0,
            }
            price = price_map.get(o.market, lambda: 0)()
            if price > 0:
                await _bot_ref.broker.close(o, price, "manual_close")
                return {"status": "closed", "order_id": order_id, "pnl": round(o.pnl_usd, 2)}
    raise HTTPException(status_code=404, detail="Position not found")


# ── Bot control ───────────────────────────────────────────────────────

@app.get("/api/bot/status")
async def bot_status(user: str = Depends(verify_token)):
    if not _bot_ref:
        return {"running": False, "mode": MODE, "halted": False}
    return {
        "running": _bot_ref._running,
        "mode": _bot_ref.mode,
        "halted": _bot_ref.risk.state.halted,
        "halt_reason": _bot_ref.risk.state.halt_reason,
        "uptime_seconds": 0,
    }

@app.post("/api/bot/resume")
async def bot_resume(user: str = Depends(verify_token)):
    """Resume trading after circuit breaker halt."""
    if _bot_ref:
        _bot_ref.risk.resume()
        return {"status": "resumed"}
    return {"status": "bot_not_connected"}

@app.post("/api/bot/halt")
async def bot_halt(user: str = Depends(verify_token)):
    """Emergency halt — stop all new trades."""
    if _bot_ref:
        _bot_ref.risk._halt("manual_halt")
        return {"status": "halted"}
    return {"status": "bot_not_connected"}


# ── Performance metrics ───────────────────────────────────────────────

@app.get("/api/metrics")
async def get_metrics(user: str = Depends(verify_token)):
    snap = _build_live_snapshot()
    trades = _build_trade_history(1000)
    if not trades:
        return {"equity_curve": [], "pnl_by_market": {}, "pnl_by_strategy": {}, "daily": []}

    # Equity curve: cumulative pnl over time
    sorted_trades = sorted(trades, key=lambda t: t["close_time"])
    cumulative = 0.0
    equity_curve = []
    for t in sorted_trades:
        cumulative += t["pnl"]
        equity_curve.append({"ts": t["close_time"], "equity": round(10000 + cumulative, 2)})

    pnl_by_market = {}
    for m in ["crash_boom","forex","crypto"]:
        pnl_by_market[m] = round(sum(t["pnl"] for t in trades if t["market"] == m), 2)

    pnl_by_strategy = {}
    for strat in set(t["strategy"] for t in trades):
        pnl_by_strategy[strat] = round(sum(t["pnl"] for t in trades if t["strategy"] == strat), 2)

    return {
        "equity_curve": equity_curve[-200:],
        "pnl_by_market": pnl_by_market,
        "pnl_by_strategy": pnl_by_strategy,
        "total_trades": len(trades),
        "overall_wr": round(sum(1 for t in trades if t["pnl"] > 0) / max(len(trades),1) * 100, 1),
    }

@app.get("/api/prices")
async def get_prices(user: str = Depends(verify_token)):
    snap = _build_live_snapshot()
    return snap["prices"]


# ══════════════════════════════════════════════════════════════════════
#  WebSocket — live feed
# ══════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = None):
    # Verify token from query param
    if token:
        try:
            jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except Exception:
            await ws.close(code=4001)
            return

    await manager.connect(ws)
    # Send initial snapshot immediately
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": _build_live_snapshot()}))
        while True:
            # Keep alive — client can send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ══════════════════════════════════════════════════════════════════════
#  SPA fallback — serve React app for all non-API routes
# ══════════════════════════════════════════════════════════════════════

@app.get("/")
@app.get("/{full_path:path}")
async def serve_spa(full_path: str = ""):
    index = os.path.join(web_dist, "index.html")
    if os.path.exists(index) and not full_path.startswith("api") and not full_path.startswith("ws"):
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not found")
