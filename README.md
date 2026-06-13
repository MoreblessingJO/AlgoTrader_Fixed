# AlgoTrader — Autonomous Multi-Market Trading System

An institutional-grade algorithmic trading platform operating simultaneously across three markets using AI-driven signal generation and continuous learning.

## Markets
| Market | Broker | Strategies | Backtest WR |
|--------|--------|-----------|-------------|
| Crypto perpetual futures | Binance | CR-S1 (Funding Arb), CR-S2 (Momentum) | 68–80% |
| Crash & Boom synthetic indices | Deriv | CB-S1 to CB-S4 (Compression, Divergence, Sniper) | 80–98% |
| Forex majors & crosses | OANDA | FX-S1 to FX-S4 (Breakout, Divergence, News, Reversion) | 66–80% |

## Architecture
```
bot.py                  ← Main async orchestrator (3 market loops + monitor)
config.py               ← All strategy parameters & risk settings
data/
  binance_feed.py       ← Binance WebSocket + REST
  deriv_feed.py         ← Deriv WebSocket tick stream
  oanda_feed.py         ← OANDA REST candles & prices
signals/
  features.py           ← 110+ feature engineering pipeline
  consensus.py          ← Weighted voting + conflict resolution
  funding_arb.py        ← Funding rate arbitrage strategy
execution/
  broker.py             ← Unified paper/live order execution
  risk.py               ← Kelly sizing + circuit breakers
monitor/
  telegram.py           ← Trade alerts & daily reports
api/
  server.py             ← FastAPI REST + WebSocket backend
web/
  index.html            ← Full trading dashboard (React SPA)
mobile/
  App.js                ← React Native app (iOS + Android)
paper_trading_simulator.py  ← 3-market paper trading comparison engine
```

## Quick start

```bash
# 1. Setup
cp .env.example .env          # fill in your API keys
bash setup.sh                 # install deps, create venv

# 2. Paper trade (no real money)
source venv/bin/activate
python bot.py --paper         # starts bot + API on :8080
# Open web/index.html in browser → sign in (admin / changeme123)

# 3. Run simulation
python paper_trading_simulator.py demo

# 4. Deploy to VPS
bash deploy.sh yourdomain.com
```

## API keys needed (all free tiers work)
- **Binance testnet**: `testnet.binance.vision` — free API keys
- **Deriv**: `developers.deriv.com` — free app ID
- **OANDA practice**: `oanda.com` — free practice account + API key
- **Telegram bot**: `@BotFather` on Telegram — free

## Mobile app
```bash
cd mobile
npx create-expo-app TradingApp
cp App.js TradingApp/App.js
cd TradingApp && npx expo start
```
See `mobile/SETUP.md` for full instructions.

## Risk defaults
- 2% risk per trade
- 6% daily loss limit → auto-halt
- 15% drawdown → emergency halt
- Max 8 concurrent positions
- Minimum 1.5:1 reward-to-risk ratio enforced

## Paper trading → live readiness checklist
- [ ] Live WR within 8pp of backtest WR for all strategies
- [ ] Composite score stable for 2+ consecutive weeks
- [ ] No circuit breakers in final 2 weeks of paper trading
- [ ] API keys verified on all three exchanges
- [ ] Accounts funded at target capital levels

---
*Paper trade for a minimum of 4 weeks before committing real capital.*
