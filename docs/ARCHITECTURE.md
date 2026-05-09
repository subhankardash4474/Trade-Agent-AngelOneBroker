# AI Trading Agent for Indian Stock Market — Architecture Document

## Executive Summary

An autonomous AI-powered trading agent that operates on the Indian equity market through **Zerodha Kite Connect API**. The agent is seeded with **₹10,000** capital and executes intraday and short-term swing trades on NSE/BSE to generate consistent returns while enforcing strict risk management.

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        AI TRADING AGENT SYSTEM                         │
│                                                                        │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────────────────┐  │
│  │  Market Data  │──▶│   Feature    │──▶│    Strategy Engine (AI)     │  │
│  │   Pipeline    │   │  Engineering │   │  ┌────────┐ ┌───────────┐ │  │
│  │              │   │              │   │  │Momentum│ │Mean Revert│ │  │
│  │ • Live Ticks │   │ • Technical  │   │  └────────┘ └───────────┘ │  │
│  │ • OHLCV      │   │ • Sentiment  │   │  ┌────────┐ ┌───────────┐ │  │
│  │ • Order Book │   │ • Volume     │   │  │Breakout│ │ML Ensemble│ │  │
│  └──────────────┘   └──────────────┘   │  └────────┘ └───────────┘ │  │
│                                         └─────────┬──────────────────┘  │
│                                                   │ Signals             │
│                                                   ▼                     │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐           │
│  │  Portfolio    │◀──│    Order     │◀──│ Risk Management  │           │
│  │  Manager     │   │  Executor    │   │                  │           │
│  │              │   │              │   │ • Position Sizing │           │
│  │ • Holdings   │   │ • Kite API   │   │ • Stop-Loss      │           │
│  │ • P&L Track  │   │ • Slippage   │   │ • Daily Limits   │           │
│  │ • NAV        │   │ • Retry      │   │ • Drawdown Guard │           │
│  └──────────────┘   └──────────────┘   └──────────────────┘           │
│                                                                        │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    Monitoring & Alerting                         │   │
│  │  • Daily P&L Reports  • Trade Journal  • Email Alerts            │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Components

### 2.1 Broker Integration Layer (Zerodha Kite Connect)

| Aspect            | Detail                                                   |
|-------------------|----------------------------------------------------------|
| **API**           | Kite Connect v3 (REST + WebSocket)                       |
| **Auth**          | OAuth2 token flow — daily login via `request_token`      |
| **Market Data**   | WebSocket for live ticks; REST for historical OHLCV      |
| **Order Types**   | MARKET, LIMIT, SL, SL-M                                 |
| **Rate Limits**   | 10 requests/sec (orders), 3 requests/sec (historical)   |
| **Cost**          | ₹2,000/month API subscription + ₹20/intraday order      |

#### Authentication Flow

```
┌──────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐
│ Agent │───▶│ Kite     │───▶│ User      │───▶│ Access   │
│ Boot  │    │ Login URL│    │ Redirects │    │ Token    │
└──────┘    └──────────┘    └───────────┘    └──────────┘
                                                   │
                                              Valid for 1 day
                                                   │
                                              ▼ Stored in
                                          Encrypted Vault
```

**Token management**: The access token expires daily. The agent uses a scheduled job at 8:45 AM IST to automate token refresh via a headless browser or manual callback endpoint.

### 2.2 Market Data Pipeline

```
┌──────────────────────────────────────────────────────────┐
│                  DATA PIPELINE                            │
│                                                          │
│  ┌────────────────┐                                      │
│  │ Kite WebSocket │──── Live Ticks (LTP, OHLC, Volume)  │
│  │ (Tick Mode)    │     @ ~500ms per instrument          │
│  └────────────────┘                                      │
│         │                                                │
│         ▼                                                │
│  ┌────────────────┐     ┌──────────────────┐            │
│  │ Tick Aggregator│────▶│ TimescaleDB /    │            │
│  │ (1m, 5m, 15m) │     │ SQLite (local)   │            │
│  └────────────────┘     └──────────────────┘            │
│                                │                         │
│                                ▼                         │
│                    ┌──────────────────┐                  │
│                    │ Historical Data  │                  │
│                    │ (Kite REST API)  │                  │
│                    │ Backfilled daily │                  │
│                    └──────────────────┘                  │
│                                                          │
│  ┌────────────────┐                                      │
│  │ News / Sentiment│── FinShots, MoneyControl RSS       │
│  │ Feed (optional) │   Processed via LLM sentiment      │
│  └────────────────┘                                      │
└──────────────────────────────────────────────────────────┘
```

**Instruments watched** (with ₹10K capital): Focus on liquid, low-price NSE stocks and Nifty 50 components that allow meaningful position sizes.

### 2.3 Feature Engineering

Features computed in real-time for each candidate instrument:

| Category       | Features                                                          |
|----------------|-------------------------------------------------------------------|
| **Trend**      | EMA(9, 21, 50), MACD, ADX, Supertrend                           |
| **Momentum**   | RSI(14), Stochastic, Williams %R, ROC                            |
| **Volatility** | Bollinger Bands, ATR(14), Keltner Channels                      |
| **Volume**     | VWAP, OBV, Volume ratio (current / 20-day avg)                  |
| **Price Action**| Candle patterns (Doji, Engulfing, Hammer), S/R levels           |
| **Market**     | Nifty 50 trend, India VIX level, sector momentum                |
| **Derived**    | Distance from day high/low, gap %, pre-market volume            |

### 2.4 Strategy Engine (AI Core)

The engine runs multiple sub-strategies and an **ensemble meta-model** to generate final signals.

```
┌─────────────────────────────────────────────────────┐
│                STRATEGY ENGINE                       │
│                                                     │
│  ┌─────────────────┐  ┌──────────────────────────┐ │
│  │ Rule-Based Layer │  │   ML Model Layer          │ │
│  │                 │  │                          │ │
│  │ • EMA Crossover │  │ • XGBoost classifier     │ │
│  │ • RSI Reversal  │  │   (direction prediction) │ │
│  │ • VWAP Bounce   │  │                          │ │
│  │ • Opening Range │  │ • LSTM / Transformer     │ │
│  │   Breakout      │  │   (price movement pred)  │ │
│  │ • Supertrend    │  │                          │ │
│  │   Follow        │  │ • Reinforcement Learning │ │
│  │                 │  │   (PPO agent for sizing) │ │
│  └────────┬────────┘  └───────────┬──────────────┘ │
│           │                       │                 │
│           ▼                       ▼                 │
│  ┌─────────────────────────────────────────┐       │
│  │        Ensemble Meta-Model              │       │
│  │  Weighted voting across all strategies  │       │
│  │  Confidence score: 0.0 — 1.0            │       │
│  │  Only trade if confidence ≥ 0.7          │       │
│  └─────────────────────┬───────────────────┘       │
│                         │                           │
│                         ▼                           │
│              Signal: BUY / SELL / HOLD              │
│              + target_price, stop_loss              │
│              + position_size_suggestion             │
└─────────────────────────────────────────────────────┘
```

#### Sub-Strategies Detail

| Strategy               | Type      | Timeframe | Description                                                  |
|------------------------|-----------|-----------|--------------------------------------------------------------|
| **EMA Crossover**      | Trend     | 5m        | EMA(9) crosses EMA(21); confirmed by ADX > 25               |
| **RSI Mean Reversion** | Reversal  | 15m       | RSI < 30 with bullish divergence → BUY; RSI > 70 → SELL     |
| **VWAP Bounce**        | Intraday  | 1m/5m     | Price touches VWAP from below with volume spike              |
| **Opening Range Breakout** | Breakout | 15m   | First 15-min high/low breakout with volume confirmation      |
| **Supertrend Follow**  | Trend     | 5m/15m    | Supertrend(10,3) flip signals with ATR-based stops           |
| **XGBoost Classifier** | ML        | 5m        | Trained on 2 years of data; predicts 15-min direction        |
| **LSTM Price Model**   | Deep ML   | 15m       | Sequence model for short-term price movement prediction      |

### 2.5 Risk Management Framework

**This is the most critical module.** With ₹10,000 capital, survival is priority #1.

```
┌──────────────────────────────────────────────────────────┐
│              RISK MANAGEMENT RULES                        │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │ Position Sizing                               │       │
│  │ • Max risk per trade: 1% of capital (₹100)   │       │
│  │ • Max position size: 20% of capital (₹2,000) │       │
│  │ • Position = Risk Amount / (Entry - StopLoss) │       │
│  └──────────────────────────────────────────────┘       │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │ Stop-Loss Rules                               │       │
│  │ • Hard stop-loss on EVERY trade (no exceptions)│      │
│  │ • ATR-based: 1.5 × ATR(14) from entry        │       │
│  │ • Trailing stop: activated at 1:1 R:R         │       │
│  │ • Time-based: exit all intraday by 3:15 PM    │       │
│  └──────────────────────────────────────────────┘       │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │ Daily Limits                                  │       │
│  │ • Max daily loss: 3% of capital (₹300)        │       │
│  │ • Max trades per day: 5                       │       │
│  │ • Max open positions: 2 simultaneous          │       │
│  │ • Stop trading after 2 consecutive losses     │       │
│  └──────────────────────────────────────────────┘       │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │ Drawdown Protection                           │       │
│  │ • If capital drops below ₹8,500 (15% DD):    │       │
│  │   → Reduce position size to 0.5% risk         │       │
│  │ • If capital drops below ₹7,000 (30% DD):    │       │
│  │   → HALT trading, alert owner, require manual │       │
│  │     restart                                   │       │
│  │ • Weekly max loss: 5% of capital              │       │
│  └──────────────────────────────────────────────┘       │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │ Market Regime Filter                          │       │
│  │ • Skip trading if India VIX > 25 (high fear)  │       │
│  │ • Reduce size if Nifty below 200-day EMA      │       │
│  │ • No trading on budget day / RBI policy day    │       │
│  └──────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────┘
```

### 2.6 Order Execution Engine

```
                    Signal from Strategy Engine
                              │
                              ▼
                  ┌───────────────────────┐
                  │  Pre-Trade Validation  │
                  │  • Risk limits OK?     │
                  │  • Margin available?   │
                  │  • Market hours?       │
                  │  • Instrument tradable?│
                  └───────────┬───────────┘
                              │ PASS
                              ▼
                  ┌───────────────────────┐
                  │  Order Construction    │
                  │  • Calc position size  │
                  │  • Set SL/target price │
                  │  • Choose order type   │
                  │    (LIMIT preferred)   │
                  └───────────┬───────────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │  Place via Kite API    │──── Primary Order
                  │  place_order()         │
                  └───────────┬───────────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │  Place SL Order        │──── Bracket / SL-M
                  │  (immediately after    │
                  │   entry fill)          │
                  └───────────┬───────────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │  Order Monitor Loop    │
                  │  • Check fill status   │
                  │  • Handle partial fills│
                  │  • Update trailing SL  │
                  │  • Time-based exit     │
                  └───────────────────────┘
```

### 2.7 Portfolio & Performance Tracker

```
┌─────────────────────────────────────────────────────┐
│           PORTFOLIO MANAGER                          │
│                                                     │
│  Current State:                                     │
│  ┌───────────────────────────────────────────────┐ │
│  │ Capital:     ₹10,000 (initial)                │ │
│  │ Cash:        ₹8,200                           │ │
│  │ Invested:    ₹1,800 (2 positions)             │ │
│  │ Day P&L:     +₹45 (+0.45%)                    │ │
│  │ Total P&L:   +₹320 (+3.2%)                    │ │
│  │ Win Rate:    58% (29/50 trades)               │ │
│  │ Avg R:R:     1:1.8                            │ │
│  │ Sharpe:      1.42                             │ │
│  └───────────────────────────────────────────────┘ │
│                                                     │
│  Trade Journal (auto-logged):                       │
│  ┌───────────────────────────────────────────────┐ │
│  │ Each trade records:                           │ │
│  │ • Entry/Exit time, price, quantity            │ │
│  │ • Strategy that generated the signal          │ │
│  │ • Confidence score at entry                   │ │
│  │ • P&L (absolute + %)                          │ │
│  │ • Slippage (expected vs actual)               │ │
│  │ • Market context (VIX, Nifty level)           │ │
│  │ • Screenshot of chart at entry                │ │
│  └───────────────────────────────────────────────┘ │
│                                                     │
│  Performance Analytics:                             │
│  • Daily equity curve                              │
│  • Strategy-wise hit rate                          │
│  • Max drawdown tracking                           │
│  • Monthly return report                           │
│  • Brokerage cost tracking                         │
└─────────────────────────────────────────────────────┘
```

### 2.8 Monitoring & Alerting

| Channel       | Events                                                          |
|---------------|-----------------------------------------------------------------|
| **Email**     | Weekly performance report, monthly analytics                    |
| **Dashboard** | Streamlit web UI — live positions, equity curve, trade log      |
| **Logs**      | Structured JSON logs → all decisions, API calls, errors         |

---

## 3. Technology Stack

| Layer               | Technology                                          |
|---------------------|-----------------------------------------------------|
| **Language**        | Python 3.11+                                        |
| **Broker API**      | `kiteconnect` (official Zerodha SDK)                |
| **Data Store**      | SQLite (local) / TimescaleDB (if scaled)            |
| **ML/AI**           | scikit-learn, XGBoost, PyTorch (LSTM), Stable-Baselines3 (RL) |
| **Feature Eng.**    | pandas, pandas-ta, numpy                            |
| **Scheduling**      | APScheduler / cron                                  |
| **Backtesting**     | Backtrader / custom engine                          |
| **Dashboard**       | Streamlit                                           |
| **Alerts**          | Resend API / SMTP                                   |
| **Config**          | YAML + environment variables                        |
| **Logging**         | structlog (JSON structured logging)                 |
| **Secrets**         | python-dotenv + OS keyring                          |

---

## 4. Daily Operational Flow

```
 TIME (IST)     ACTION
 ──────────     ──────────────────────────────────────────────
  08:30 AM      Agent wakes up
                ├── Refresh Kite access token
                ├── Download overnight corporate actions
                └── Load watchlist for the day

  08:45 AM      Pre-market analysis
                ├── Fetch pre-market OHLC data
                ├── Compute features on 15m/daily candles
                ├── Run ML models for stock scoring
                └── Select top 10 candidates

  09:15 AM      Market opens — WebSocket stream starts
                ├── Monitor opening range (first 15 min)
                └── Aggregate tick data into candles

  09:30 AM      Trading begins
                ├── Opening Range Breakout strategy active
                ├── Other strategies start signaling
                └── Execute trades per risk rules

  09:30–3:00    Active trading loop (every 5 sec)
                ├── Update features on new candle close
                ├── Run strategy ensemble
                ├── Execute new signals (if risk allows)
                ├── Update trailing stop-losses
                └── Monitor open positions

  03:00 PM      Wind-down begins
                ├── No new intraday positions
                └── Tighten trailing stops

  03:15 PM      Forced exit
                ├── Close ALL intraday positions
                └── Cancel open orders

  03:30 PM      Market closes — WebSocket disconnects

  03:45 PM      End-of-day processing
                ├── Compute daily P&L
                ├── Update equity curve
                ├── Log all trades to journal
                ├── Run strategy performance analysis
                ├── Send daily summary email
                └── Retrain models (weekly, on weekends)

  WEEKEND        Model retraining & strategy optimization
                ├── Backtest updated models on last 6 months
                ├── Tune hyperparameters
                ├── Update stock universe
                └── Generate weekly performance report
```

---

## 5. Database Schema

```sql
-- Core tables for the trading agent

CREATE TABLE instruments (
    token           INTEGER PRIMARY KEY,
    tradingsymbol   TEXT NOT NULL,
    exchange        TEXT NOT NULL,      -- NSE / BSE
    segment         TEXT NOT NULL,
    lot_size        INTEGER DEFAULT 1,
    tick_size       REAL DEFAULT 0.05,
    in_watchlist    BOOLEAN DEFAULT 0
);

CREATE TABLE candles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_token INTEGER NOT NULL,
    timestamp       DATETIME NOT NULL,
    timeframe       TEXT NOT NULL,       -- 1m, 5m, 15m, 1d
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER NOT NULL,
    FOREIGN KEY (instrument_token) REFERENCES instruments(token)
);

CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    instrument_token INTEGER NOT NULL,
    strategy        TEXT NOT NULL,
    direction       TEXT NOT NULL,       -- BUY / SELL
    confidence      REAL NOT NULL,
    entry_price     REAL,
    stop_loss       REAL,
    target_price    REAL,
    executed        BOOLEAN DEFAULT 0
);

CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER,
    order_id        TEXT NOT NULL,       -- Kite order ID
    instrument_token INTEGER NOT NULL,
    direction       TEXT NOT NULL,
    entry_time      DATETIME NOT NULL,
    entry_price     REAL NOT NULL,
    exit_time       DATETIME,
    exit_price      REAL,
    quantity        INTEGER NOT NULL,
    pnl             REAL,
    pnl_pct         REAL,
    brokerage       REAL,
    slippage        REAL,
    strategy        TEXT NOT NULL,
    status          TEXT DEFAULT 'OPEN', -- OPEN / CLOSED / CANCELLED
    notes           TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE portfolio (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL UNIQUE,
    opening_capital REAL NOT NULL,
    closing_capital REAL NOT NULL,
    day_pnl         REAL NOT NULL,
    total_pnl       REAL NOT NULL,
    num_trades      INTEGER NOT NULL,
    win_rate        REAL,
    max_drawdown    REAL,
    sharpe_ratio    REAL
);

CREATE TABLE risk_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME NOT NULL,
    event_type      TEXT NOT NULL,       -- DAILY_LOSS_LIMIT, DRAWDOWN_HALT, etc.
    details         TEXT,
    action_taken    TEXT
);
```

---

## 6. Configuration (config.yaml)

```yaml
broker:
  api_key: "${KITE_API_KEY}"
  api_secret: "${KITE_API_SECRET}"
  redirect_url: "http://localhost:5000/callback"

capital:
  initial: 10000
  currency: "INR"

risk:
  max_risk_per_trade_pct: 1.0        # 1% = ₹100
  max_position_size_pct: 20.0        # ₹2,000 max per position
  max_daily_loss_pct: 3.0            # ₹300 then stop
  max_trades_per_day: 5
  max_open_positions: 2
  consecutive_loss_halt: 2
  drawdown_reduce_threshold: 15.0    # reduce size at 15% DD
  drawdown_halt_threshold: 30.0      # stop at 30% DD
  weekly_max_loss_pct: 5.0

strategy:
  min_confidence: 0.7
  timeframes: ["1m", "5m", "15m"]
  enabled_strategies:
    - ema_crossover
    - rsi_reversal
    - vwap_bounce
    - opening_range_breakout
    - supertrend_follow
    - xgboost_classifier

market:
  exchange: "NSE"
  max_vix_threshold: 25
  skip_dates: ["2026-02-01"]  # budget day etc.
  market_open: "09:15"
  market_close: "15:30"
  no_new_positions_after: "15:00"
  force_exit_by: "15:15"

watchlist:
  max_instruments: 10
  selection_criteria:
    min_avg_volume: 500000
    max_price: 500              # affordable with ₹10K
    min_price: 50
    exchanges: ["NSE"]

notifications:
  email:
    enabled: true
    provider: "resend"
    recipient: "alerts@example.com"

logging:
  level: "INFO"
  format: "json"
  file: "logs/agent.log"
```

---

## 7. Project Structure

```
zerodha-trading-agent/
│
├── config/
│   ├── config.yaml              # Main configuration
│   └── instruments.yaml         # Watchlist overrides
│
├── src/
│   ├── __init__.py
│   ├── main.py                  # Entry point — orchestrator
│   │
│   ├── broker/
│   │   ├── __init__.py
│   │   ├── kite_auth.py         # Token management & login
│   │   ├── kite_client.py       # Kite API wrapper
│   │   └── order_executor.py    # Order placement & monitoring
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── market_data.py       # WebSocket + historical data
│   │   ├── tick_aggregator.py   # Tick → Candle conversion
│   │   └── data_store.py        # SQLite CRUD operations
│   │
│   ├── features/
│   │   ├── __init__.py
│   │   ├── technical.py         # Technical indicator computation
│   │   ├── volume.py            # Volume-based features
│   │   └── market_context.py    # VIX, Nifty, sector features
│   │
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base_strategy.py     # Abstract strategy interface
│   │   ├── ema_crossover.py
│   │   ├── rsi_reversal.py
│   │   ├── vwap_bounce.py
│   │   ├── opening_range.py
│   │   ├── supertrend.py
│   │   └── ml_strategy.py       # XGBoost / LSTM wrapper
│   │
│   ├── ml/
│   │   ├── __init__.py
│   │   ├── xgboost_model.py     # XGBoost direction classifier
│   │   ├── lstm_model.py        # PyTorch LSTM for price pred
│   │   ├── ensemble.py          # Meta-model combining strategies
│   │   └── trainer.py           # Model training pipeline
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── position_sizer.py    # Calculate position sizes
│   │   ├── risk_manager.py      # Enforce all risk rules
│   │   └── drawdown_guard.py    # Drawdown monitoring & halt
│   │
│   ├── portfolio/
│   │   ├── __init__.py
│   │   ├── portfolio_manager.py # Track holdings, NAV, P&L
│   │   └── trade_journal.py     # Auto-log every trade
│   │
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── alert_manager.py     # Email alerts
│   │   └── email_reporter.py    # Email reports
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logger.py            # Structured logging setup
│       ├── scheduler.py         # APScheduler wrapper
│       └── helpers.py           # Misc utility functions
│
├── backtest/
│   ├── __init__.py
│   ├── backtester.py            # Backtesting engine
│   └── results/                 # Backtest output reports
│
├── dashboard/
│   └── app.py                   # Streamlit dashboard
│
├── models/                      # Saved ML model artifacts
│   ├── xgboost_v1.pkl
│   └── lstm_v1.pt
│
├── logs/                        # Runtime logs
├── data/                        # Local data cache
│   └── agent.db                 # SQLite database
│
├── tests/
│   ├── test_risk_manager.py
│   ├── test_strategies.py
│   ├── test_order_executor.py
│   └── test_portfolio.py
│
├── .env.example                 # Template for secrets
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## 8. Brokerage Cost Analysis (₹10K Capital)

| Item                       | Cost                          |
|----------------------------|-------------------------------|
| Kite Connect API           | ₹2,000/month                 |
| Brokerage (intraday)       | ₹20/order (or 0.03%, lower)  |
| STT (sell side, intraday)  | 0.025% of turnover            |
| Transaction charges (NSE)  | 0.00345% of turnover          |
| GST                        | 18% on brokerage + txn charges|
| SEBI charges               | ₹10 per crore                 |
| Stamp duty                 | 0.003% (buy side)             |

**Estimated cost per round-trip trade (₹2,000 position):**
- Brokerage: ₹20 × 2 = ₹40
- STT: ~₹0.50
- Other: ~₹8
- **Total: ~₹48 per trade**

**Break-even per trade: ~2.4% gain needed on ₹2,000 position**

> **Important**: With ₹10K capital and ₹2,000/month API cost, the agent needs to generate at least **20% monthly returns** just to cover API costs. Consider starting with paper trading or using the free Kite web-based approach initially.

---

## 9. Phased Implementation Roadmap

### Phase 1: Foundation (Week 1–2)
- [ ] Set up Kite Connect authentication
- [ ] Build market data pipeline (WebSocket + historical)
- [ ] Implement SQLite data store
- [ ] Create basic technical feature computation
- [ ] Build risk management module

### Phase 2: Strategy Engine (Week 3–4)
- [ ] Implement rule-based strategies (EMA, RSI, VWAP, ORB)
- [ ] Build backtesting engine
- [ ] Backtest each strategy on 1-year historical data
- [ ] Implement order executor with paper trading mode

### Phase 3: ML Integration (Week 5–6)
- [ ] Train XGBoost direction classifier
- [ ] Build ensemble meta-model
- [ ] Integrate ML signals with rule-based strategies
- [ ] Comprehensive backtesting of ensemble

### Phase 4: Go Live (Week 7–8)
- [ ] Paper trade for 2 weeks minimum
- [ ] Set up email notifications
- [ ] Build Streamlit dashboard
- [ ] Deploy with real capital (start with ₹5,000)
- [ ] Scale to full ₹10,000 after 1 week of live validation

### Phase 5: Optimize (Ongoing)
- [ ] Weekly model retraining
- [ ] Strategy performance review
- [ ] Add LSTM / RL models
- [ ] Expand instrument universe
- [ ] Scale capital as profits grow

---

## 10. Key Risks & Mitigations

| Risk                          | Mitigation                                              |
|-------------------------------|---------------------------------------------------------|
| API costs exceed profits      | Start with paper trading; only go live when backtests show > 25% monthly returns |
| Model overfitting             | Walk-forward validation; retrain on rolling 6-month windows |
| Flash crash / gap down        | Hard stop-losses; max 20% per position; drawdown halt at 30% |
| API downtime                  | Graceful degradation; close all positions on disconnect |
| Token expiry mid-day          | Monitor token validity; auto-refresh mechanism          |
| Slippage on low-liquidity stocks | Only trade stocks with > 5L daily volume; use LIMIT orders |
| Regulatory changes (SEBI)     | Monitor SEBI circulars; keep agent rules updatable via config |
| Over-trading (high brokerage) | Max 5 trades/day; min confidence threshold of 0.7       |

---

## 11. Legal & Compliance Notes

- **SEBI Algo Trading**: Retail algo trading via API is permitted through registered brokers like Zerodha. No special registration needed for personal use.
- **Tax**: Short-term capital gains (intraday = speculative income) taxed at slab rate. Maintain detailed trade journal for ITR filing.
- **Audit**: If turnover exceeds ₹10 crore, tax audit under Section 44AB may apply (unlikely with ₹10K capital).
- **Disclaimer**: This is an automated system. Past backtested performance does not guarantee future results. Use at your own risk.
