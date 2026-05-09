# AI Trading Agent for Indian Stock Market

An autonomous, production-ready trading agent for NSE/BSE equities, built in Python with AngelOne broker integration. Starts with ₹10,000 seed capital and supports intraday and short-term swing strategies.

## Architecture

```
trading-agent/
├── trading_agent.py         # Main orchestrator (loop, signal flow, exits)
├── run_daemon.py            # Daemon entry point (with watchdog)
├── main.py                  # CLI entry (trade, backtest, status)
├── backtest.py              # Single-strategy backtest
├── backtest_ensemble.py     # Full-fidelity ensemble backtest
├── analyze_day.py           # Daily post-session analysis
├── config.yaml, requirements.txt, .env.example
│
├── core/                    # Portfolio, risk, ensemble, DB, regime, execution
├── strategies/              # 1 file per strategy (mean_reversion, xgboost, etc.)
├── brokers/                 # Broker abstraction (AngelOne, paper)
├── monitoring/              # Alerts (Resend, SMTP) with retry+spool
├── data/                    # Data handlers, scanners, datasets
├── training/                # ML training pipeline
├── models/                  # Serialized ML artefacts (gitignored)
│
├── tools/                   # CLIs: now, postmortem, health_check, battery, etc.
│
├── tests/
│   ├── unit/                # Pure logic, mock-heavy
│   └── integration/         # DB persistence, full pipelines
│
├── docs/                    # ARCHITECTURE.md + journals + audits + postmortems
└── logs/                    # Runtime artefacts (live agent, daemon, backtests)
```

For deeper architecture, design decisions, and operational runbooks see [`docs/`](./docs/README.md).

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `config.yaml` with your credentials and preferences:

```yaml
broker:
  api_key: "YOUR_ANGELONE_API_KEY"
  client_id: "YOUR_CLIENT_ID"
  password: "YOUR_PASSWORD"
  totp_secret: "YOUR_TOTP_SECRET"
  mode: paper  # Start with paper, switch to live when ready

capital:
  initial_balance: 10000.0

strategies:
  active:
    - moving_average_crossover
    - rsi_momentum
    - mean_reversion
```

### 3. AngelOne API Setup

1. Register at [AngelOne SmartAPI](https://smartapi.angelone.in/)
2. Create an app to get your API key
3. Generate a TOTP secret for 2FA
4. Add credentials to `config.yaml`

## Usage

### Paper Trading (Recommended Start)

```bash
# Start paper trading with the live CLI dashboard
python main.py trade --paper --dashboard

# Paper trade with 30-second poll interval
python main.py trade --paper --interval 30
```

### Backtesting

```bash
# Backtest all configured strategies on all instruments
python main.py backtest

# Backtest specific symbols and strategies
python main.py backtest --symbols RELIANCE TCS INFY --strategies rsi_momentum

# Export results to CSV
python main.py backtest --export

# Use 5-minute candles
python main.py backtest --interval 5min
```

### Live Trading

> **Warning**: Live trading uses real money. Thoroughly test in paper mode first.

```bash
# Set mode to 'live' in config.yaml, then:
python main.py trade --dashboard
```

### Status Check

```bash
python main.py status
```

## Strategies

### Moving Average Crossover
- Generates BUY when fast EMA (9) crosses above slow EMA (21)
- Generates SELL on bearish crossover
- Filters noise with configurable signal threshold

### RSI Momentum
- BUY when RSI exits oversold zone (< 30) with upward momentum
- SELL when RSI exits overbought zone (> 70) with downward momentum
- Volume surge confirmation for higher-confidence signals

### Mean Reversion
- BUY when Z-score drops below -2.0 and price starts reverting to mean
- SELL when Z-score rises above +2.0 and price starts reverting
- Bollinger Band width filter to avoid low-volatility traps

### Adding a Custom Strategy

Create a new file in `strategies/` extending `BaseStrategy`:

```python
from strategies.base_strategy import BaseStrategy, Signal

class MyStrategy(BaseStrategy):
    def __init__(self, params=None):
        super().__init__(name="my_strategy", params=params or {})

    @property
    def required_history_bars(self) -> int:
        return 50

    def generate_signal(self, data, symbol):
        # Your logic here
        return self._make_signal(Signal.BUY, symbol, data, confidence=0.8)
```

Register it in `strategies/__init__.py`:

```python
from strategies.my_strategy import MyStrategy
STRATEGY_REGISTRY["my_strategy"] = MyStrategy
```

Add to `config.yaml`:

```yaml
strategies:
  active:
    - my_strategy
  my_strategy:
    param1: value1
```

## Risk Management

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_position_size_pct` | 20% | Max single position as % of balance |
| `max_portfolio_risk_pct` | 2% | Max risk per trade as % of balance |
| `stop_loss_pct` | 1.5% | Default stop-loss distance |
| `take_profit_pct` | 3.0% | Default take-profit distance |
| `daily_loss_limit_pct` | 5% | Circuit breaker: max daily loss |
| `max_drawdown_pct` | 10% | Circuit breaker: max drawdown from peak |
| `max_open_positions` | 3 | Maximum concurrent positions |
| `max_trades_per_day` | 10 | Maximum trades per session |

## Monitoring

### CLI Dashboard
The Rich-based dashboard shows real-time:
- Portfolio value and P&L
- Open positions with entry prices
- Risk metrics and circuit breaker status
- Recent trade history

### Alerts
Configure email notifications in `config.yaml`:
- Trade execution alerts
- Risk breach warnings
- Daily performance summaries

## Testing

```bash
# Run all tests
pytest tests/ -v

# Unit only (pure logic, fast)
pytest tests/unit/ -v

# Integration only (DB, full pipelines)
pytest tests/integration/ -v

# Coverage
pytest tests/ --cov=core --cov=strategies -v

# Specific module
pytest tests/unit/test_risk_manager.py -v
```

## Logs

All activity is logged to both console and `logs/` directory:
- `logs/trading_agent_YYYY-MM-DD.log` — Agent activity
- `logs/trades.csv` — Complete trade history

## Key Design Decisions

- **Paper-first**: Defaults to paper mode to prevent accidental real trades
- **Dual data source**: AngelOne for live data, Yahoo Finance as fallback for backtesting
- **Rate limiting**: Built-in token-bucket rate limiter for API calls
- **Circuit breakers**: Automatic trading halt on daily loss limit or max drawdown
- **EOD square-off**: Automatically closes all intraday positions at market close
- **Retry logic**: Failed orders are retried with configurable backoff
- **Modular strategies**: Add/remove strategies without touching core logic

## Disclaimer

This software is for educational and research purposes. Trading in financial markets involves substantial risk of loss. Past performance (including backtests) does not guarantee future results. Always start with paper trading and only trade with capital you can afford to lose.
