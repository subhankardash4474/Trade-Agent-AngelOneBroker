"""
Backtesting Engine
Runs trading strategies against historical data and produces comprehensive
performance metrics including Sharpe ratio, max drawdown, win rate, and
profit factor. Supports side-by-side strategy comparison.
"""

import argparse
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml
from loguru import logger
from tabulate import tabulate

from core.data_handler import DataHandler
from core.portfolio import Portfolio, TradeRecord
from core.risk_manager import RiskManager
from strategies import STRATEGY_REGISTRY
from strategies.base_strategy import BaseStrategy, Signal


class BacktestEngine:
    """
    Event-driven backtesting engine that simulates trading on historical data.

    Processes bars sequentially, feeds them to strategies, and tracks
    simulated positions with realistic commission and slippage modelling.
    """

    def __init__(self, config: dict):
        self.config = config
        bt_cfg = config.get("backtest", {})
        self.start_date = bt_cfg.get("start_date", "2025-01-01")
        self.end_date = bt_cfg.get("end_date", "2026-03-29")
        self.initial_capital = bt_cfg.get("initial_capital", 10000.0)
        self.commission_pct = bt_cfg.get("commission_pct", 0.03)
        self.slippage_pct = bt_cfg.get("slippage_pct", 0.05)

        self.data_handler = DataHandler(config)
        self._results: Dict[str, dict] = {}

    def run(
        self,
        symbols: List[str],
        strategy_names: Optional[List[str]] = None,
        interval: str = "1d",
    ) -> Dict[str, dict]:
        """
        Run backtest for each strategy across the given symbols.

        Returns:
            Dict mapping strategy name to its performance results.
        """
        strat_cfg = self.config.get("strategies", {})
        if strategy_names is None:
            strategy_names = strat_cfg.get("active", [])

        # Download historical data once
        logger.info(f"Downloading historical data for {symbols}...")
        market_data = self.data_handler.download_historical_for_backtest(
            symbols=symbols,
            interval=interval,
            start_date=self.start_date,
            end_date=self.end_date,
        )

        if not market_data:
            logger.error("No historical data available for backtesting")
            return {}

        for strat_name in strategy_names:
            cls = STRATEGY_REGISTRY.get(strat_name)
            if cls is None:
                logger.warning(f"Strategy '{strat_name}' not found, skipping")
                continue

            params = strat_cfg.get(strat_name, {})
            strategy = cls(params)
            logger.info(f"\n{'='*60}\nBacktesting: {strat_name}\n{'='*60}")
            result = self._run_strategy(strategy, market_data)
            self._results[strat_name] = result

        return self._results

    def _run_strategy(
        self, strategy: BaseStrategy, market_data: Dict[str, pd.DataFrame]
    ) -> dict:
        """Simulate a single strategy across all symbols."""
        portfolio = Portfolio(
            initial_balance=self.initial_capital,
            commission_pct=self.commission_pct,
            log_dir=os.path.join("logs", f"backtest_{strategy.name}"),
        )
        risk_manager = RiskManager(self.config, self.initial_capital)

        equity_curve: List[float] = [self.initial_capital]
        timestamps: List[pd.Timestamp] = []

        for symbol, data in market_data.items():
            if data.empty:
                continue

            min_bars = strategy.required_history_bars
            logger.info(f"  Processing {symbol}: {len(data)} bars (need {min_bars} min)")

            for i in range(min_bars, len(data)):
                window = data.iloc[:i + 1]
                current_bar = data.iloc[i]
                current_price = float(current_bar["close"])
                current_time = window.index[-1]

                # Apply slippage for simulation realism
                slippage = current_price * (self.slippage_pct / 100)

                # Check stop-loss / take-profit on open positions
                if symbol in portfolio.positions:
                    pos = portfolio.positions[symbol]
                    trigger = risk_manager.check_stop_loss_take_profit(
                        pos.entry_price, current_price, pos.side,
                        pos.stop_loss, pos.take_profit,
                    )
                    if trigger:
                        exit_price = current_price + slippage if pos.side == "SELL" else current_price - slippage
                        record = portfolio.close_position(symbol, exit_price, exit_reason=trigger)
                        if record:
                            risk_manager.record_trade(record.pnl)
                        continue

                # Generate strategy signal
                signal = strategy.generate_signal(window, symbol)

                if signal.signal == Signal.BUY and symbol not in portfolio.positions:
                    can_trade, _ = risk_manager.can_trade()
                    if not can_trade:
                        continue

                    buy_price = current_price + slippage
                    sl = signal.stop_loss or risk_manager.get_stop_loss(buy_price, "BUY")
                    tp = signal.take_profit or risk_manager.get_take_profit(buy_price, "BUY")
                    qty = risk_manager.calculate_position_size(buy_price, sl)
                    if qty > 0:
                        portfolio.open_position(
                            symbol=symbol, side="BUY", price=buy_price,
                            quantity=qty, strategy=strategy.name,
                            stop_loss=sl, take_profit=tp,
                        )
                        risk_manager.update_open_positions(portfolio.open_position_count)

                elif signal.signal == Signal.SELL and symbol in portfolio.positions:
                    sell_price = current_price - slippage
                    record = portfolio.close_position(symbol, sell_price, exit_reason="signal")
                    if record:
                        risk_manager.record_trade(record.pnl)
                    risk_manager.update_open_positions(portfolio.open_position_count)

                prices = {symbol: current_price}
                equity_curve.append(portfolio.get_total_value(prices))
                timestamps.append(current_time)

        # Close any remaining positions at last available prices
        for symbol in list(portfolio.positions.keys()):
            if symbol in market_data and not market_data[symbol].empty:
                last_price = float(market_data[symbol]["close"].iloc[-1])
                record = portfolio.close_position(symbol, last_price, exit_reason="backtest_end")
                if record:
                    risk_manager.record_trade(record.pnl)

        metrics = portfolio.get_performance_metrics()
        metrics["equity_curve"] = equity_curve
        metrics["timestamps"] = timestamps
        metrics["final_value"] = portfolio.get_total_value({})
        metrics["return_pct"] = round(
            (metrics["final_value"] - self.initial_capital) / self.initial_capital * 100, 2
        )
        return metrics

    def print_results(self):
        """Print a formatted comparison table of all backtested strategies."""
        if not self._results:
            print("No backtest results available.")
            return

        headers = [
            "Metric",
            *list(self._results.keys()),
        ]
        rows = [
            ("Total Trades", *[r.get("total_trades", 0) for r in self._results.values()]),
            ("Winning Trades", *[r.get("winning_trades", 0) for r in self._results.values()]),
            ("Losing Trades", *[r.get("losing_trades", 0) for r in self._results.values()]),
            ("Win Rate (%)", *[f"{r.get('win_rate', 0):.1f}" for r in self._results.values()]),
            ("Total PnL (₹)", *[f"{r.get('total_pnl', 0):,.2f}" for r in self._results.values()]),
            ("Return (%)", *[f"{r.get('return_pct', 0):.2f}" for r in self._results.values()]),
            ("Avg PnL (₹)", *[f"{r.get('avg_pnl', 0):,.2f}" for r in self._results.values()]),
            ("Max Win (₹)", *[f"{r.get('max_win', 0):,.2f}" for r in self._results.values()]),
            ("Max Loss (₹)", *[f"{r.get('max_loss', 0):,.2f}" for r in self._results.values()]),
            ("Profit Factor", *[f"{r.get('profit_factor', 0):.2f}" for r in self._results.values()]),
            ("Sharpe Ratio", *[f"{r.get('sharpe_ratio', 0):.2f}" for r in self._results.values()]),
            ("Max Drawdown (₹)", *[f"{r.get('max_drawdown', 0):,.2f}" for r in self._results.values()]),
            ("Max Drawdown (%)", *[f"{r.get('max_drawdown_pct', 0):.2f}" for r in self._results.values()]),
            ("Final Value (₹)", *[f"{r.get('final_value', 0):,.2f}" for r in self._results.values()]),
        ]

        print("\n" + "=" * 80)
        print("BACKTEST RESULTS COMPARISON")
        print(f"Period: {self.start_date} to {self.end_date}")
        print(f"Initial Capital: ₹{self.initial_capital:,.2f}")
        print("=" * 80)
        print(tabulate(rows, headers=headers, tablefmt="grid"))
        print()

    def export_results(self, output_dir: str = "logs"):
        """Export backtest results to CSV."""
        os.makedirs(output_dir, exist_ok=True)
        for name, result in self._results.items():
            equity = result.get("equity_curve", [])
            if equity:
                df = pd.DataFrame({"equity": equity})
                path = os.path.join(output_dir, f"backtest_{name}_equity.csv")
                df.to_csv(path, index=True)
                logger.info(f"Exported equity curve: {path}")

        # Summary CSV
        summary_rows = []
        for name, r in self._results.items():
            row = {k: v for k, v in r.items() if k not in ("equity_curve", "timestamps")}
            row["strategy"] = name
            summary_rows.append(row)
        if summary_rows:
            df = pd.DataFrame(summary_rows)
            path = os.path.join(output_dir, "backtest_summary.csv")
            df.to_csv(path, index=False)
            logger.info(f"Exported summary: {path}")


def main():
    parser = argparse.ArgumentParser(description="Run backtests on trading strategies")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to backtest (default: from config)",
    )
    parser.add_argument(
        "--strategies", nargs="+", default=None,
        help="Strategies to test (default: from config)",
    )
    parser.add_argument("--interval", default="1d", help="Data interval (default: 1d)")
    parser.add_argument("--export", action="store_true", help="Export results to CSV")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    symbols = args.symbols
    if symbols is None:
        symbols = [i["symbol"] for i in config.get("market", {}).get("instruments", [])]

    engine = BacktestEngine(config)
    engine.run(symbols=symbols, strategy_names=args.strategies, interval=args.interval)
    engine.print_results()

    if args.export:
        engine.export_results()


if __name__ == "__main__":
    main()
