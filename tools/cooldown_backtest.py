"""
Opening-window cooldown backtest (60-day, 5-min bars)
=====================================================

Runs the actual strategy logic against 60 days of historical 5-min OHLCV
under four "what if" cooldown variants and produces a comparison report.

Why this exists:
- The 7-day live-trade cooldown_simulation showed +Rs 381 swing if we'd
  suppressed `mean_reversion` entries during 09:15-09:30 IST.
- That was a *post-hoc filter* on actual trades — it doesn't model what
  the freed-up capital would have done. This script does, by re-running
  strategy logic over historical bars and tracking entries that the
  cooldown would have blocked plus the trades that would have replaced
  them later in the day.

Variants tested:
  baseline           : current behaviour, no cooldown
  variant_a_all_915  : ALL strategies suppressed 09:15-09:30 IST
  variant_b_mr_915   : mean_reversion only, 09:15-09:30 IST (closest to
                       what the live data flagged)
  variant_c_mr_945   : mean_reversion only, 09:15-09:45 IST (extended)

Output:
  logs/cooldown_backtest_<YYYY-MM-DD>.md     (human-readable comparison)
  logs/cooldown_backtest_<YYYY-MM-DD>.json   (machine-readable raw)
  logs/cooldown_backtest_<variant>_trades.csv (per-trade audit per variant)

Caveats:
- Yfinance 5-min bars have a 60-day rolling window. We pull whatever's
  available — typically the last 60 days.
- Slippage (5 bps) and commission (3 bps) match the live config defaults.
- This backtest does NOT model the ensemble — strategies trade in
  isolation. That's deliberate for cooldown analysis (we want to know
  which strategy is bleeding regardless of ensemble vote).
- XGBoost is excluded (slow + needs trained model + only ~3 weeks of live
  history, not enough for a meaningful 60-day comparison).
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytz
import yaml
from loguru import logger

# Allow running both as `python tools/cooldown_backtest.py` and
# `python -m tools.cooldown_backtest`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.data_handler import DataHandler  # noqa: E402
from core.portfolio import Portfolio  # noqa: E402
from core.risk_manager import RiskManager  # noqa: E402
from strategies import STRATEGY_REGISTRY  # noqa: E402
from strategies.base_strategy import BaseStrategy, Signal  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


# ── Variant configs ──────────────────────────────────────────────────────

@dataclass
class CooldownConfig:
    """Defines a single cooldown experiment.

    `applies_to` empty means "all strategies". Otherwise only the listed
    strategy names are suppressed in the window.
    """
    name: str
    description: str
    start: dtime
    end: dtime
    applies_to: List[str] = field(default_factory=list)

    def is_blocked(self, strategy_name: str, bar_time: dtime) -> bool:
        if self.applies_to and strategy_name not in self.applies_to:
            return False
        return self.start <= bar_time < self.end


VARIANTS: Dict[str, Optional[CooldownConfig]] = {
    "baseline": None,
    "variant_a_all_915": CooldownConfig(
        name="variant_a_all_915",
        description="All strategies suppressed 09:15-09:30 IST",
        start=dtime(9, 15),
        end=dtime(9, 30),
        applies_to=[],
    ),
    "variant_b_mr_915": CooldownConfig(
        name="variant_b_mr_915",
        description="mean_reversion only, 09:15-09:30 IST",
        start=dtime(9, 15),
        end=dtime(9, 30),
        applies_to=["mean_reversion"],
    ),
    "variant_c_mr_945": CooldownConfig(
        name="variant_c_mr_945",
        description="mean_reversion only, 09:15-09:45 IST",
        start=dtime(9, 15),
        end=dtime(9, 45),
        applies_to=["mean_reversion"],
    ),
}


# ── Universe ─────────────────────────────────────────────────────────────

# Pulled from recent live trades + add a few liquid index names so the
# universe isn't entirely concentrated in mid-caps. Keep at 20 for a
# tractable wall-clock; the cooldown signal should be visible across any
# sample of mean-reverting names.
DEFAULT_UNIVERSE: List[str] = [
    # Recently traded (today's positions + this week's trades)
    "POLICYBZR", "ZYDUSWELL", "CROMPTON",
    "MANAPPURAM", "DABUR", "CENTRALBK", "MAHABANK",
    "EXIDEIND", "VEDL", "BANDHANBNK", "INDIANB",
    "HUDCO", "LODHA", "TATATECH", "VBL",
    # Liquid large-caps for control variety
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "AXISBANK",
]


# ── Per-variant trade ledger ─────────────────────────────────────────────

@dataclass
class TradeLedger:
    """Captured trades + summary metrics for a single variant."""
    variant: str
    trades: List[Dict[str, Any]] = field(default_factory=list)
    final_cash: float = 0.0
    initial_capital: float = 0.0
    blocked_signals: int = 0

    def metrics(self) -> Dict[str, Any]:
        if not self.trades:
            return {
                "variant": self.variant,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "total_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "expectancy": 0.0,
                "max_win": 0.0,
                "max_loss": 0.0,
                "blocked_signals": self.blocked_signals,
                "final_cash": self.final_cash,
                "return_pct": 0.0,
            }
        pnls = [t["pnl"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "variant": self.variant,
            "total_trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
            "total_pnl": round(sum(pnls), 2),
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "expectancy": round(sum(pnls) / len(pnls), 2),
            "max_win": round(max(pnls), 2),
            "max_loss": round(min(pnls), 2),
            "blocked_signals": self.blocked_signals,
            "final_cash": round(self.final_cash, 2),
            "return_pct": round(
                (self.final_cash - self.initial_capital) / self.initial_capital * 100, 2
            ) if self.initial_capital else 0.0,
        }


# ── Backtest core ────────────────────────────────────────────────────────

def _to_ist(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalise any timestamp into IST timezone-aware form."""
    if ts.tzinfo is None:
        return IST.localize(ts.to_pydatetime())
    return ts.tz_convert(IST)


def _run_one_variant(
    variant_name: str,
    cooldown: Optional[CooldownConfig],
    market_data: Dict[str, pd.DataFrame],
    strategies: List[Tuple[str, BaseStrategy]],
    config: dict,
) -> TradeLedger:
    """Run a single variant across all symbols × all strategies in isolation.

    "In isolation" means each (symbol, strategy) gets its own portfolio
    snapshot — we don't simulate the ensemble. This is the right
    abstraction for cooldown analysis: we want per-strategy signal stats
    without ensemble interference.
    """
    bt_cfg = config.get("backtest", {})
    initial_capital = float(bt_cfg.get("initial_capital", 25000.0))
    commission_pct = float(bt_cfg.get("commission_pct", 0.03))
    slippage_pct = float(bt_cfg.get("slippage_pct", 0.05))

    ledger = TradeLedger(variant=variant_name, initial_capital=initial_capital)

    for strat_name, strat_class in strategies:
        _progress(f"  [{variant_name}] strategy={strat_name} starting…")
        for sym_idx, (symbol, data) in enumerate(market_data.items(), 1):
            if data.empty:
                continue
            min_bars = strat_class.required_history_bars
            if len(data) <= min_bars + 1:
                continue
            _progress(f"    [{variant_name}/{strat_name}] {sym_idx}/{len(market_data)} {symbol} ({len(data)} bars)…")

            # Per-strategy / per-symbol portfolio
            portfolio = Portfolio(
                initial_balance=initial_capital,
                commission_pct=commission_pct,
                log_dir=os.path.join("logs", f"_bt_tmp_{variant_name}_{strat_name}"),
            )
            risk_manager = RiskManager(config, initial_capital)

            # Cap the window passed to the strategy to just the bars it
            # actually needs. Strategies like `mean_reversion` do
            # `df.copy()` and rolling-window stats over the FULL slice,
            # making per-bar cost O(i) and total cost O(N²) — for 4365
            # bars that's ~9.5M ops/symbol/strategy, which 1) makes the
            # 60-day run take 3+ hours and 2) explains the 1% CPU
            # utilisation we observed (Python's pandas being mostly stuck
            # in copy+roll churn). A bounded window (max needed history
            # + small buffer) caps cost to O(N), giving ~50x speedup
            # without changing strategy semantics — the rolling stats
            # only depend on the last `lookback_period` bars anyway.
            window_size = max(min_bars + 5, 50)
            for i in range(min_bars, len(data)):
                start_idx = max(0, i + 1 - window_size)
                window = data.iloc[start_idx : i + 1]
                bar = data.iloc[i]
                bar_ts = _to_ist(window.index[-1])
                bar_time = bar_ts.time()
                price = float(bar["close"])
                slip = price * (slippage_pct / 100.0)

                # Exit existing position on SL/TP
                if symbol in portfolio.positions:
                    pos = portfolio.positions[symbol]
                    trigger = risk_manager.check_stop_loss_take_profit(
                        pos.entry_price, price, pos.side,
                        pos.stop_loss, pos.take_profit,
                    )
                    if trigger:
                        exit_price = price + slip if pos.side == "SELL" else price - slip
                        record = portfolio.close_position(symbol, exit_price, exit_reason=trigger)
                        if record:
                            ledger.trades.append({
                                "variant": variant_name,
                                "symbol": symbol,
                                "strategy": strat_name,
                                "side": record.side,
                                "entry_price": record.entry_price,
                                "exit_price": record.exit_price,
                                "qty": record.quantity,
                                "pnl": record.pnl,
                                "exit_reason": record.exit_reason,
                                "entry_time": str(record.entry_time),
                                "exit_time": str(record.exit_time),
                            })
                            risk_manager.record_trade(record.pnl)
                        continue

                # Generate strategy signal
                try:
                    signal = strat_class.generate_signal(window, symbol)
                except Exception as e:
                    logger.debug(f"Signal error for {symbol}/{strat_name}: {e}")
                    continue

                # Apply cooldown filter on entries
                is_entry = signal.signal in (Signal.BUY, Signal.SELL)
                if is_entry and cooldown is not None:
                    if cooldown.is_blocked(strat_name, bar_time):
                        ledger.blocked_signals += 1
                        continue

                if signal.signal == Signal.BUY and symbol not in portfolio.positions:
                    can_trade, _ = risk_manager.can_trade()
                    if not can_trade:
                        continue
                    buy_price = price + slip
                    sl = signal.stop_loss or risk_manager.get_stop_loss(buy_price, "BUY")
                    tp = signal.take_profit or risk_manager.get_take_profit(buy_price, "BUY")
                    qty = risk_manager.calculate_position_size(buy_price, sl)
                    if qty > 0:
                        portfolio.open_position(
                            symbol=symbol, side="BUY", price=buy_price,
                            quantity=qty, strategy=strat_name,
                            stop_loss=sl, take_profit=tp,
                        )
                        risk_manager.update_open_positions(portfolio.open_position_count)

                elif signal.signal == Signal.SELL and symbol not in portfolio.positions:
                    # Treat SELL on flat as a SHORT entry (mean_reversion does this).
                    can_trade, _ = risk_manager.can_trade()
                    if not can_trade:
                        continue
                    sell_price = price - slip
                    sl = signal.stop_loss or risk_manager.get_stop_loss(sell_price, "SELL")
                    tp = signal.take_profit or risk_manager.get_take_profit(sell_price, "SELL")
                    qty = risk_manager.calculate_position_size(sell_price, sl)
                    if qty > 0:
                        portfolio.open_position(
                            symbol=symbol, side="SELL", price=sell_price,
                            quantity=qty, strategy=strat_name,
                            stop_loss=sl, take_profit=tp,
                        )
                        risk_manager.update_open_positions(portfolio.open_position_count)

                elif signal.signal == Signal.SELL and symbol in portfolio.positions:
                    # Close LONG on opposite signal.
                    sell_price = price - slip
                    record = portfolio.close_position(symbol, sell_price, exit_reason="signal")
                    if record:
                        ledger.trades.append({
                            "variant": variant_name,
                            "symbol": symbol,
                            "strategy": strat_name,
                            "side": record.side,
                            "entry_price": record.entry_price,
                            "exit_price": record.exit_price,
                            "qty": record.quantity,
                            "pnl": record.pnl,
                            "exit_reason": record.exit_reason,
                            "entry_time": str(record.entry_time),
                            "exit_time": str(record.exit_time),
                        })
                        risk_manager.record_trade(record.pnl)

            # Close any leftover position at the last bar
            if symbol in portfolio.positions and not data.empty:
                last_price = float(data["close"].iloc[-1])
                record = portfolio.close_position(symbol, last_price, exit_reason="backtest_end")
                if record:
                    ledger.trades.append({
                        "variant": variant_name,
                        "symbol": symbol,
                        "strategy": strat_name,
                        "side": record.side,
                        "entry_price": record.entry_price,
                        "exit_price": record.exit_price,
                        "qty": record.quantity,
                        "pnl": record.pnl,
                        "exit_reason": "backtest_end",
                        "entry_time": str(record.entry_time),
                        "exit_time": str(record.exit_time),
                    })

            ledger.final_cash += portfolio.cash - initial_capital  # delta only

    # We started each (strategy, symbol) with `initial_capital`. The
    # final_cash field holds the *delta* aggregated across every per-pair
    # portfolio. Adding back the base gives us the simulated equity if
    # all pairs shared one bankroll (rough approximation; real
    # multi-strat ensemble would behave differently — see caveat in the
    # module docstring).
    ledger.final_cash = initial_capital + ledger.final_cash
    return ledger


def _download_universe(
    config: dict,
    symbols: List[str],
    interval: str,
    days: int,
) -> Dict[str, pd.DataFrame]:
    handler = DataHandler(config)
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    data: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols, 1):
        logger.info(f"[{i}/{len(symbols)}] downloading {sym} ({interval}, {days}d)…")
        try:
            df = handler.get_historical_data(
                symbol=sym, interval=interval,
                start_date=start, end_date=end,
                use_cache=False,
            )
            if df.empty:
                logger.warning(f"  {sym}: empty")
                continue
            # Normalise index to IST
            if df.index.tz is None:
                df.index = pd.to_datetime(df.index).tz_localize("UTC").tz_convert(IST)
            else:
                df.index = df.index.tz_convert(IST)
            data[sym] = df
            logger.info(f"  {sym}: {len(df)} bars [{df.index[0]} → {df.index[-1]}]")
        except Exception as e:
            logger.error(f"  {sym}: {type(e).__name__}: {e}")
    return data


def _build_strategies(config: dict, names: List[str]) -> List[Tuple[str, BaseStrategy]]:
    strat_cfg = config.get("strategies", {})
    out: List[Tuple[str, BaseStrategy]] = []
    for n in names:
        cls = STRATEGY_REGISTRY.get(n)
        if cls is None:
            logger.warning(f"Strategy '{n}' not in registry, skipping")
            continue
        params = strat_cfg.get(n, {})
        out.append((n, cls(params)))
    return out


# ── Reporting ────────────────────────────────────────────────────────────

def _render_markdown(
    started: datetime,
    finished: datetime,
    universe: List[str],
    interval: str,
    days: int,
    strategies: List[str],
    variants: Dict[str, Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append(f"# Opening-window cooldown backtest — {started.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"- **Run started:** {started.strftime('%Y-%m-%d %H:%M:%S')} IST")
    lines.append(f"- **Run finished:** {finished.strftime('%Y-%m-%d %H:%M:%S')} IST")
    lines.append(f"- **Wall clock:** {(finished - started).total_seconds() / 60.0:.1f} min")
    lines.append(f"- **Interval:** {interval}  ·  **Window:** {days} days")
    lines.append(f"- **Universe:** {len(universe)} symbols ({', '.join(universe)})")
    lines.append(f"- **Strategies tested:** {', '.join(strategies)}")
    lines.append("")
    lines.append("## Variant comparison")
    lines.append("")
    headers = [
        "Variant", "Trades", "Wins", "Losses", "WR %",
        "Total PnL", "Avg Win", "Avg Loss", "PF", "Exp/trade",
        "Max Win", "Max Loss", "Blocked", "Final cash", "Return %",
    ]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for variant_name, m in variants.items():
        lines.append("| " + " | ".join([
            variant_name,
            str(m["total_trades"]),
            str(m["wins"]),
            str(m["losses"]),
            f"{m['win_rate_pct']:.1f}",
            f"{m['total_pnl']:+,.2f}",
            f"{m['avg_win']:+,.2f}",
            f"{m['avg_loss']:+,.2f}",
            f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "inf",
            f"{m['expectancy']:+,.2f}",
            f"{m['max_win']:+,.2f}",
            f"{m['max_loss']:+,.2f}",
            str(m["blocked_signals"]),
            f"{m['final_cash']:,.2f}",
            f"{m['return_pct']:+.2f}",
        ]) + " |")
    lines.append("")

    base = variants.get("baseline")
    if base:
        lines.append("## Δ vs baseline")
        lines.append("")
        diff_headers = ["Variant", "ΔTrades", "ΔWR %", "ΔPnL", "ΔPF", "ΔReturn %", "Verdict"]
        lines.append("| " + " | ".join(diff_headers) + " |")
        lines.append("|" + "|".join(["---"] * len(diff_headers)) + "|")
        for variant_name, m in variants.items():
            if variant_name == "baseline":
                continue
            d_trades = m["total_trades"] - base["total_trades"]
            d_wr = m["win_rate_pct"] - base["win_rate_pct"]
            d_pnl = m["total_pnl"] - base["total_pnl"]
            d_pf = m["profit_factor"] - base["profit_factor"] if base["profit_factor"] != float("inf") else 0.0
            d_ret = m["return_pct"] - base["return_pct"]
            if d_pnl > 50 and m["profit_factor"] > base["profit_factor"]:
                verdict = "ship — clear improvement"
            elif d_pnl > 0:
                verdict = "marginal improvement"
            elif d_pnl < -50:
                verdict = "regression — do not ship"
            else:
                verdict = "neutral"
            lines.append("| " + " | ".join([
                variant_name,
                f"{d_trades:+d}",
                f"{d_wr:+.1f}",
                f"{d_pnl:+,.2f}",
                f"{d_pf:+.2f}",
                f"{d_ret:+.2f}",
                verdict,
            ]) + " |")
        lines.append("")

    lines.append("## Methodology")
    lines.append("")
    lines.append("- Each (strategy × symbol) pair runs in isolation with a fresh "
                 "`Rs 25,000` portfolio. Cooldown filter sits between strategy "
                 "signal generation and order placement — entries only.")
    lines.append("- SL / TP / position sizing use the live `RiskManager` with "
                 "live config values. Slippage 5 bps, commission 3 bps.")
    lines.append("- Final cash aggregates the per-pair delta back onto a single "
                 "Rs 25,000 base — it's a comparable scalar, not a faithful "
                 "multi-strat equity curve.")
    lines.append("- The ensemble layer is intentionally bypassed — see module docstring.")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- Yfinance 5-min data has a hard 60-day rolling cap. If the run "
                 "started more than 60 days after a particular bar's date, that "
                 "bar is unavailable and the per-symbol window is shorter.")
    lines.append("- Per-strategy isolation OVER-counts trades vs the live "
                 "ensemble (which would gate via voting). The *direction* and "
                 "*sign* of the cooldown delta is what matters for the ship "
                 "decision; absolute PnL numbers are upper bounds.")
    lines.append("- XGBoost is excluded — see module docstring.")
    return "\n".join(lines)


# ── Public entry point ──────────────────────────────────────────────────

_PROGRESS_PATH = _REPO_ROOT / "logs" / "cooldown_backtest_progress.log"


def _progress(msg: str) -> None:
    """Append a one-line progress marker to the progress log.

    We write directly to a file rather than through loguru / stderr
    because the parent shell's pipe buffering was back-pressure stalling
    the python process when we let strategy / portfolio INFO logs flow
    through. Plain file append → small, atomic, fast.
    """
    ts = datetime.now(IST).strftime("%H:%M:%S")
    try:
        with open(_PROGRESS_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts}  {msg}\n")
    except Exception:
        pass


def _quiet_logging() -> None:
    """Silence ALL loguru output so the parent shell pipe doesn't choke.

    Strategies log INFO on every signal eval, Portfolio logs INFO on
    every open/close, and the backtest itself iterates ~700K bars —
    enough log volume to back-pressure-stall a redirected python process
    on Windows (we observed 2.5s CPU per 5min wall clock with default
    logging). We remove every handler. Progress markers go to
    `_progress()` (file-based) instead.
    """
    logger.remove()


def run_backtest(
    config_path: str = "config.yaml",
    universe: Optional[List[str]] = None,
    strategies: Optional[List[str]] = None,
    interval: str = "5min",
    days: int = 60,
    quiet: bool = True,
) -> Tuple[Path, Path]:
    if quiet:
        _quiet_logging()
    started = datetime.now(IST)
    # Reset the progress log for this run.
    try:
        _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROGRESS_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    universe = universe or DEFAULT_UNIVERSE
    strategies = strategies or ["mean_reversion", "rsi_momentum"]

    logger.info(f"=== Cooldown backtest started @ {started:%Y-%m-%d %H:%M:%S} IST ===")
    logger.info(f"Universe: {len(universe)} symbols / Strategies: {strategies}")
    logger.info(f"Interval: {interval} / Window: {days} days")

    _progress(f"=== Cooldown backtest starting ({len(universe)} symbols, "
              f"{interval}, {days}d) ===")
    market_data = _download_universe(config, universe, interval, days)
    _progress(f"Downloaded {len(market_data)}/{len(universe)} symbols")
    logger.info(f"Downloaded data for {len(market_data)} / {len(universe)} symbols")
    if not market_data:
        raise SystemExit("No market data downloaded — aborting.")

    strat_objs = _build_strategies(config, strategies)
    if not strat_objs:
        raise SystemExit("No strategies built — aborting.")

    results: Dict[str, Dict[str, Any]] = {}
    raw: Dict[str, Any] = {"variants": {}}
    out_dir = _REPO_ROOT / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)

    for variant_name, cooldown in VARIANTS.items():
        v_started = datetime.now(IST)
        _progress(f"=== Variant: {variant_name} starting "
                  f"({cooldown.description if cooldown else 'no cooldown'}) ===")
        logger.info(f"--- Running variant: {variant_name} "
                    f"({cooldown.description if cooldown else 'no cooldown'}) ---")
        # Each variant gets a deepcopy of the strategies so per-strategy
        # internal state (e.g. _is_ready) doesn't leak between runs.
        copied = [(n, deepcopy(s)) for n, s in strat_objs]
        ledger = _run_one_variant(variant_name, cooldown, market_data, copied, config)
        m = ledger.metrics()
        results[variant_name] = m
        raw["variants"][variant_name] = {"metrics": m, "trades": ledger.trades}
        v_finished = datetime.now(IST)
        elapsed = (v_finished - v_started).total_seconds()
        _progress(f"=== Variant: {variant_name} done in {elapsed:.1f}s "
                  f"(trades={m['total_trades']}, pnl={m['total_pnl']:+,.2f}, "
                  f"wr={m['win_rate_pct']:.1f}%, pf={m['profit_factor']:.2f}, "
                  f"blocked={m['blocked_signals']}) ===")
        logger.info(f"    {variant_name}: {m['total_trades']} trades, "
                    f"PnL={m['total_pnl']:+,.2f}, WR={m['win_rate_pct']:.1f}%, "
                    f"PF={m['profit_factor']:.2f}, blocked={m['blocked_signals']} "
                    f"(elapsed {elapsed:.1f}s)")
        # Per-variant trade audit CSV
        if ledger.trades:
            df = pd.DataFrame(ledger.trades)
            csv_path = out_dir / f"cooldown_backtest_{variant_name}_trades.csv"
            df.to_csv(csv_path, index=False)
            logger.info(f"    wrote {csv_path.name}")

    finished = datetime.now(IST)
    raw.update({
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "wall_clock_minutes": round((finished - started).total_seconds() / 60.0, 1),
        "universe": universe,
        "strategies": strategies,
        "interval": interval,
        "days": days,
    })

    md = _render_markdown(started, finished, universe, interval, days, strategies, results)
    md_path = out_dir / f"cooldown_backtest_{started:%Y-%m-%d}.md"
    json_path = out_dir / f"cooldown_backtest_{started:%Y-%m-%d}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")

    logger.info(f"=== Done. Wrote {md_path.name} + {json_path.name} ===")
    logger.info(f"Wall clock: {(finished - started).total_seconds() / 60.0:.1f} min")
    return md_path, json_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Override DEFAULT_UNIVERSE")
    parser.add_argument("--strategies", nargs="+", default=None,
                        help="Override default ['mean_reversion','rsi_momentum']")
    parser.add_argument("--interval", default="5min")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--verbose", action="store_true",
                        help="Don't suppress strategy/portfolio per-bar logs")
    args = parser.parse_args()
    run_backtest(
        config_path=args.config,
        universe=args.symbols,
        strategies=args.strategies,
        interval=args.interval,
        days=args.days,
        quiet=not args.verbose,
    )
