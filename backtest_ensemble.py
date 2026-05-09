"""
Ensemble Backtest Engine
────────────────────────
Full-fidelity backtester that mirrors the live agent's decision pipeline:

  1. Download 5-minute historical bars for each symbol.
  2. At each bar, every ACTIVE strategy votes.
  3. EnsembleModel aggregates votes with current regime-aware weights.
  4. If confidence >= threshold, run every gate the live agent runs:
       - expected-profit gate (charges-aware)
       - min_entry_atr_pct gate
       - dead-hour blocks
       - circuit-proximity check
       - max positions / max losses per stock
  5. Trade charges are computed from core.charges (exact live math).
  6. Full equity curve, per-strategy attribution, and config suggestions.

Run:
  python backtest_ensemble.py --symbols RELIANCE.NS TCS.NS --days 30
  python backtest_ensemble.py --interval 5m --days 14 --report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml
from loguru import logger

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.charges import compute_round_trip
from core.data_handler import DataHandler
from core.ensemble import EnsembleModel
from core.features import FeatureEngine
from core.portfolio import Portfolio
from core.regime import classify_regime
from core.risk_manager import RiskManager
from strategies import STRATEGY_REGISTRY
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


DEAD_HOUR_BLOCKS = [(12, 0, 13, 0)]  # inclusive start, exclusive end


@dataclass
class BacktestConfig:
    initial_capital: float = 10000.0
    commission_pct: float = 0.03
    slippage_pct: float = 0.05
    confidence_threshold: float = 0.55
    min_entry_atr_pct: float = 0.8
    min_profit_to_charges_ratio: float = 2.5
    min_absolute_reward_rs: float = 20.0
    max_positions: int = 3
    max_losses_per_stock: int = 2
    apply_dead_hour: bool = True
    apply_expected_profit_gate: bool = True
    apply_regime_filter: bool = True
    product_type: str = "INTRADAY"


@dataclass
class GateStats:
    total_signals: int = 0
    dead_hour: int = 0
    atr_too_low: int = 0
    expected_profit: int = 0
    insufficient_cash: int = 0
    max_positions_reached: int = 0
    stock_blacklisted: int = 0
    executed: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_charges: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    rr_ratio: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    final_equity: float = 0.0
    return_pct: float = 0.0
    trades: List[dict] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    gate_stats: GateStats = field(default_factory=GateStats)
    strategy_pnl: Dict[str, float] = field(default_factory=dict)
    regime_pnl: Dict[str, float] = field(default_factory=dict)


class EnsembleBacktester:
    """Mirrors the live pipeline end-to-end, on historical data."""

    def __init__(self, config: dict, bt_cfg: BacktestConfig):
        self.config = config
        self.bt = bt_cfg
        self.data_handler = DataHandler(config)
        self.feature_engine = FeatureEngine()

    # ─────────────────────────────────────────────────────
    # Public run
    # ─────────────────────────────────────────────────────

    _INTERVAL_ALIASES = {
        "5m": "5min", "15m": "15min", "30m": "30min", "1m": "1min",
        "5min": "5min", "15min": "15min", "30min": "30min", "1min": "1min",
        "1h": "1h", "1d": "1d",
    }

    def run(
        self,
        symbols: List[str],
        interval: str = "5m",
        days: int = 30,
        strategies: Optional[List[str]] = None,
        market_data: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> BacktestResult:
        """Run a backtest. If `market_data` is provided (pre-downloaded +
        feature-enriched), skip the download/compute step. This lets a
        battery runner reuse the same data across many config variants
        without hitting yfinance for each run.
        """
        # Normalize interval, strip any user-supplied .NS suffix (data handler adds it)
        interval = self._INTERVAL_ALIASES.get(interval, interval)
        symbols = [s[:-3] if s.upper().endswith(".NS") else s for s in symbols]

        if market_data is None:
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)

            logger.info(
                f"Downloading {interval} bars for {len(symbols)} symbols "
                f"({start_date} -> {end_date})..."
            )
            market_data = self.data_handler.download_historical_for_backtest(
                symbols=symbols,
                interval=interval,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )
            market_data = {s: df for s, df in market_data.items() if not df.empty}
            if not market_data:
                logger.error("No market data available.")
                return BacktestResult()

            for s in list(market_data.keys()):
                market_data[s] = self.feature_engine.compute_all(market_data[s])

        strategy_objs = self._build_strategies(strategies)
        ensemble = EnsembleModel(self.config)
        ensemble.confidence_threshold = self.bt.confidence_threshold

        portfolio = Portfolio(
            initial_balance=self.bt.initial_capital,
            commission_pct=self.bt.commission_pct,
            log_dir=os.path.join("logs", "backtest_ensemble"),
            database=None,
            product_type=self.bt.product_type,
        )
        risk_cfg = dict(self.config)
        risk_cfg.setdefault("risk", {})
        risk_cfg["risk"]["min_profit_to_charges_ratio"] = self.bt.min_profit_to_charges_ratio
        risk_cfg["risk"]["min_absolute_reward_rs"] = self.bt.min_absolute_reward_rs
        rm = RiskManager(risk_cfg, self.bt.initial_capital)

        gate_stats = GateStats()
        trades: List[dict] = []
        equity_curve: List[float] = [self.bt.initial_capital]
        losses_per_stock: Dict[str, int] = {}

        # Build a unified, time-ordered event stream across symbols so
        # portfolio constraints (max positions, cash) are enforced chronologically.
        events = self._merge_bars(market_data)

        for ts, symbol, bar, df_slice in events:
            close = float(bar["close"])

            # Check SL/TP exits for any open position on this symbol
            if symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                trigger = rm.check_stop_loss_take_profit(
                    pos.entry_price, close, pos.side, pos.stop_loss, pos.take_profit
                )
                if trigger:
                    exit_price = self._apply_slippage(close, pos.side, exit=True)
                    record = portfolio.close_position(
                        symbol, exit_price, exit_reason=trigger
                    )
                    if record:
                        rm.record_trade(record.pnl)
                        trades.append(self._trade_to_dict(record, trigger))
                        if record.pnl <= 0:
                            losses_per_stock[symbol] = losses_per_stock.get(symbol, 0) + 1

            if symbol in portfolio.positions:
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Per-strategy signals
            strat_signals: List[TradeSignal] = []
            for strat in strategy_objs:
                if len(df_slice) < strat.required_history_bars:
                    continue
                try:
                    sig = strat.generate_signal(df_slice, symbol)
                except Exception:
                    continue
                if sig and sig.signal != Signal.HOLD:
                    strat_signals.append(sig)

            if not strat_signals:
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            gate_stats.total_signals += 1

            # Regime from the most recent bar (Nifty data not available per-symbol
            # here; use a default unknown so regime-aware weights still apply a
            # neutral multiplier. Real live path queries market_context.)
            regime = "unknown"

            agg = ensemble.aggregate(strat_signals, symbol, close, regime=regime)
            if agg is None or agg.signal == Signal.HOLD:
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Gate: dead-hour
            if self.bt.apply_dead_hour and self._in_dead_hour(ts):
                gate_stats.dead_hour += 1
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Gate: ATR%
            atr_pct = self._atr_pct(df_slice)
            if atr_pct is not None and atr_pct < self.bt.min_entry_atr_pct:
                gate_stats.atr_too_low += 1
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Gate: max positions
            if portfolio.open_position_count >= self.bt.max_positions:
                gate_stats.max_positions_reached += 1
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Gate: stock blacklisted after N losses
            if losses_per_stock.get(symbol, 0) >= self.bt.max_losses_per_stock:
                gate_stats.stock_blacklisted += 1
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Sizing
            atr_val = self._latest_atr(df_slice)
            entry_price = self._apply_slippage(close, agg.signal.name, exit=False)
            sl = agg.stop_loss or rm.get_stop_loss(entry_price, agg.signal.name, atr_val)
            tp = agg.take_profit or rm.get_take_profit(
                entry_price, agg.signal.name, atr_val, regime=regime
            )
            qty = rm.calculate_position_size(entry_price, sl, atr_val)

            # Cash gate
            max_affordable = int(portfolio.cash // (entry_price * 1.01)) if entry_price > 0 else 0
            if qty > max_affordable:
                qty = max_affordable
            if qty <= 0:
                gate_stats.insufficient_cash += 1
                equity_curve.append(portfolio.get_total_value({symbol: close}))
                continue

            # Expected-profit gate
            if self.bt.apply_expected_profit_gate:
                worth, _ = rm.is_trade_worth_taking(
                    entry_price=entry_price,
                    take_profit=tp,
                    stop_loss=sl,
                    quantity=qty,
                    side=agg.signal.name,
                    product=self.bt.product_type,
                )
                if not worth:
                    gate_stats.expected_profit += 1
                    equity_curve.append(portfolio.get_total_value({symbol: close}))
                    continue

            # Execute
            portfolio.open_position(
                symbol=symbol,
                side=agg.signal.name,
                price=entry_price,
                quantity=qty,
                strategy=agg.strategy_name,
                stop_loss=sl,
                take_profit=tp,
                regime=regime,
                contributing_strategies=agg.contributing_strategies,
            )
            gate_stats.executed += 1
            equity_curve.append(portfolio.get_total_value({symbol: close}))

        # Close any still-open positions at the final bar of each symbol
        for symbol, df in market_data.items():
            if symbol in portfolio.positions and not df.empty:
                last_close = float(df["close"].iloc[-1])
                record = portfolio.close_position(symbol, last_close, exit_reason="backtest_end")
                if record:
                    rm.record_trade(record.pnl)
                    trades.append(self._trade_to_dict(record, "backtest_end"))

        return self._build_result(trades, equity_curve, gate_stats)

    # ─────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────

    def _build_strategies(self, names: Optional[List[str]]) -> List[BaseStrategy]:
        strat_cfg = self.config.get("strategies", {})
        if names is None:
            names = strat_cfg.get("active", [])
        built = []
        for n in names:
            cls = STRATEGY_REGISTRY.get(n)
            if cls is None:
                continue
            built.append(cls(strat_cfg.get(n, {})))
        return built

    def _merge_bars(self, market_data: Dict[str, pd.DataFrame]):
        """Yield (timestamp, symbol, bar_row, slice_up_to_and_including_bar) events
        in global chronological order so cross-symbol constraints are enforced."""
        events: List[tuple] = []
        for symbol, df in market_data.items():
            for i in range(len(df)):
                events.append((df.index[i], symbol, i))
        events.sort(key=lambda t: t[0])
        for ts, symbol, i in events:
            df = market_data[symbol]
            yield ts, symbol, df.iloc[i], df.iloc[: i + 1]

    def _apply_slippage(self, price: float, side: str, *, exit: bool) -> float:
        slip = price * (self.bt.slippage_pct / 100)
        if side == "BUY":
            return price + slip if not exit else price - slip
        return price - slip if not exit else price + slip

    def _in_dead_hour(self, ts) -> bool:
        try:
            hhmm = (ts.hour, ts.minute)
        except Exception:
            return False
        for sh, sm, eh, em in DEAD_HOUR_BLOCKS:
            if (hhmm >= (sh, sm)) and (hhmm < (eh, em)):
                return True
        return False

    def _atr_pct(self, df: pd.DataFrame) -> Optional[float]:
        if df.empty or "atr" not in df.columns:
            return None
        atr = df["atr"].iloc[-1]
        price = df["close"].iloc[-1]
        if pd.isna(atr) or pd.isna(price) or price <= 0:
            return None
        return float(atr / price * 100)

    def _latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        if df.empty or "atr" not in df.columns:
            return None
        v = df["atr"].iloc[-1]
        return None if pd.isna(v) else float(v)

    def _trade_to_dict(self, record, exit_reason: str) -> dict:
        return {
            "symbol": record.symbol,
            "side": record.side,
            "entry_price": record.entry_price,
            "exit_price": record.exit_price,
            "quantity": record.quantity,
            "pnl": record.pnl,
            "commission": getattr(record, "commission", 0),
            "strategy": record.strategy,
            "exit_reason": exit_reason,
            "regime": getattr(record, "regime", None),
            "entry_time": getattr(record, "entry_time", None).isoformat()
                if getattr(record, "entry_time", None) else None,
            "exit_time": getattr(record, "exit_time", None).isoformat()
                if getattr(record, "exit_time", None) else None,
        }

    def _build_result(
        self,
        trades: List[dict],
        equity_curve: List[float],
        gate_stats: GateStats,
    ) -> BacktestResult:
        r = BacktestResult(trades=trades, equity_curve=equity_curve, gate_stats=gate_stats)
        r.total_trades = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        r.wins = len(wins)
        r.losses = len(losses)
        r.total_pnl = sum(t["pnl"] for t in trades)
        r.total_charges = sum(t.get("commission", 0) or 0 for t in trades)
        r.avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        r.avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        r.win_rate = (len(wins) / len(trades) * 100) if trades else 0
        pf_w = sum(t["pnl"] for t in wins)
        pf_l = abs(sum(t["pnl"] for t in losses)) or 1.0
        r.profit_factor = pf_w / pf_l
        r.rr_ratio = abs(r.avg_win / r.avg_loss) if r.avg_loss else 0
        r.expectancy = r.total_pnl / r.total_trades if r.total_trades else 0
        r.final_equity = equity_curve[-1] if equity_curve else self.bt.initial_capital
        r.return_pct = ((r.final_equity - self.bt.initial_capital) / self.bt.initial_capital * 100) \
            if self.bt.initial_capital else 0
        if len(equity_curve) >= 2:
            peak = equity_curve[0]
            mdd = 0.0
            for v in equity_curve:
                peak = max(peak, v)
                mdd = max(mdd, peak - v)
            r.max_drawdown = mdd
            r.max_drawdown_pct = (mdd / peak * 100) if peak else 0
            returns = pd.Series(equity_curve).pct_change().dropna()
            if len(returns) > 1 and returns.std() > 0:
                r.sharpe = float((returns.mean() / returns.std()) * (252 ** 0.5))
        for t in trades:
            r.strategy_pnl[t["strategy"]] = r.strategy_pnl.get(t["strategy"], 0) + t["pnl"]
            rg = t.get("regime") or "unknown"
            r.regime_pnl[rg] = r.regime_pnl.get(rg, 0) + t["pnl"]
        return r


# ─────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────


def print_result(result: BacktestResult, bt: BacktestConfig) -> None:
    print("\n" + "=" * 78)
    print(" ENSEMBLE BACKTEST SUMMARY")
    print("=" * 78)
    print(f"  Initial capital:      Rs {bt.initial_capital:,.2f}")
    print(f"  Final equity:         Rs {result.final_equity:,.2f}")
    print(f"  Return:               {result.return_pct:+.2f}%")
    print(f"  Total P&L:            Rs {result.total_pnl:+,.2f}")
    print(f"  Trades:               {result.total_trades}  (wins: {result.wins}, losses: {result.losses})")
    print(f"  Win rate:             {result.win_rate:.1f}%")
    print(f"  R:R ratio:            1 : {result.rr_ratio:.2f}")
    print(f"  Profit factor:        {result.profit_factor:.2f}")
    print(f"  Expectancy/trade:     Rs {result.expectancy:+.2f}")
    print(f"  Max drawdown:         Rs {result.max_drawdown:,.2f} ({result.max_drawdown_pct:.2f}%)")
    print(f"  Sharpe (annualized):  {result.sharpe:.2f}")
    print(f"  Total charges:        Rs {result.total_charges:,.2f}")
    print()
    print("  Gate statistics:")
    for k, v in result.gate_stats.as_dict().items():
        if v:
            print(f"    {k:<28} {v}")
    if result.strategy_pnl:
        print("\n  P&L by lead strategy:")
        for s, v in sorted(result.strategy_pnl.items(), key=lambda kv: -kv[1]):
            print(f"    {s:<28} Rs {v:+,.2f}")
    print("=" * 78)


def export_result(result: BacktestResult, out_dir: str = "logs") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"backtest_ensemble_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    payload = {
        "summary": {k: getattr(result, k) for k in [
            "total_trades", "wins", "losses", "total_pnl", "total_charges",
            "win_rate", "profit_factor", "rr_ratio", "expectancy",
            "max_drawdown", "max_drawdown_pct", "sharpe", "final_equity",
            "return_pct",
        ]},
        "gate_stats": result.gate_stats.as_dict(),
        "strategy_pnl": result.strategy_pnl,
        "regime_pnl": result.regime_pnl,
        "trades": result.trades,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


# ─────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--symbols", nargs="+", default=None,
                   help="Symbols to backtest (defaults to config.market.instruments)")
    p.add_argument("--strategies", nargs="+", default=None,
                   help="Strategies to include (defaults to config.strategies.active)")
    p.add_argument("--interval", default="5m", help="5m / 15m / 1h / 1d")
    p.add_argument("--days", type=int, default=30, help="Days of history (default 30)")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--report", action="store_true", help="Export detailed JSON report")
    p.add_argument("--no-dead-hour", action="store_true")
    p.add_argument("--no-profit-gate", action="store_true")
    args = p.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    bt = BacktestConfig(
        initial_capital=args.capital or config.get("backtest", {}).get("initial_capital", 10000.0),
        commission_pct=config.get("backtest", {}).get("commission_pct", 0.03),
        slippage_pct=config.get("backtest", {}).get("slippage_pct", 0.05),
        confidence_threshold=config.get("ensemble", {}).get("confidence_threshold", 0.55),
        min_entry_atr_pct=config.get("robustness", {}).get("min_entry_atr_pct", 0.8),
        min_profit_to_charges_ratio=config.get("risk", {}).get("min_profit_to_charges_ratio", 2.5),
        min_absolute_reward_rs=config.get("risk", {}).get("min_absolute_reward_rs", 20.0),
        max_positions=config.get("risk", {}).get("max_positions", 3),
        max_losses_per_stock=config.get("robustness", {}).get("max_losses_per_stock_per_day", 2),
        apply_dead_hour=not args.no_dead_hour,
        apply_expected_profit_gate=not args.no_profit_gate,
    )

    symbols = args.symbols
    if symbols is None:
        symbols = [i["symbol"] for i in config.get("market", {}).get("instruments", [])]
        if not symbols:
            # Fall back to scanner universe via a small representative slice
            symbols = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "SBIN.NS"]

    engine = EnsembleBacktester(config, bt)
    result = engine.run(symbols=symbols, interval=args.interval,
                        days=args.days, strategies=args.strategies)
    print_result(result, bt)

    if args.report:
        path = export_result(result)
        print(f"\n  Detailed report: {path}")


if __name__ == "__main__":
    main()
