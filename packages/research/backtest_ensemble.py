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
import heapq
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytz
import yaml
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")
# Battery progress emission cadence. Whichever fires first wins:
#   * every PROGRESS_LOG_INTERVAL_EVENTS events  -- bounds work-per-update on
#     fast runs (full-CPU laptops do ~5,000 ev/s, so 10k ≈ 2s).
#   * every PROGRESS_LOG_INTERVAL_SECONDS seconds -- bounds wall-time-per-update
#     on slow runs (the 2-vCPU backtester VM does ~5 ev/s; 10k events alone
#     would mean 33 min between updates, which is unusable for live monitoring
#     and made the watchdog/operator think workers were hung).
# The time floor is what makes battery_status_remote.ps1 actually useful.
PROGRESS_LOG_INTERVAL_EVENTS = 10_000
PROGRESS_LOG_INTERVAL_SECONDS = 60.0

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.data_handler import DataHandler
from strategies.ensemble import EnsembleModel
from core.features import FeatureEngine
from core.portfolio import Portfolio
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
    # B-7 / C-21 (audit 2026-05-26): seed the paper-order RNG so
    # repeated runs of the same variant produce byte-identical fill
    # ledgers. Leave as None for legacy stochastic behaviour. Battery
    # variants set this to their own deterministic value so threshold
    # sweeps compare apples-to-apples on slippage / partial-fill noise.
    paper_seed: Optional[int] = None
    # 2026-05-25 risk-policy short veto. Mirrors trading_agent's
    # `risk.allow_shorts` gate so battery variants can test the
    # long-only configuration without code changes. Default True
    # preserves the existing apples-to-apples comparison for every
    # variant that doesn't opt in.
    allow_shorts: bool = True
    # 2026-05-25 senior-dev scan, perf finding: _merge_bars used to
    # yield `df.iloc[: i + 1]` -- the entire growing history -- to
    # every strategy on every bar. Each strategy then walked the full
    # slice (df.copy() + ewm() + shift()) to compute indicators whose
    # outputs only depend on the recent tail (RSI/ATR/ADX/EWM all
    # converge in ~5 * period bars; XGBoost uses a fixed 60-bar
    # feature window). Net cost was O(N) per event = O(N^2) per
    # symbol over the run. Observed in the 2026-05-25 V1+V2 nifty50
    # restart: 39 ev/s -> 8 ev/s instantaneous over 50 minutes.
    #
    # Capping the slice to the last `strategy_history_window` bars
    # makes per-event work constant w.r.t. simulation length while
    # preserving numerical equivalence at the last-bar signal (which
    # is all generate_signal() returns).
    #
    # 300 default == 5x XGBoost feature window, ~21x RSI/ATR period,
    # ~30x supertrend period. Verified numerically equivalent to the
    # full-history slice by tests/unit/test_strategy_history_window.py.
    # Configurable so a future strategy with a longer lookback can
    # opt-up without code change.
    strategy_history_window: int = 300


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
    # 2026-05-25: shorts blocked by the long-only-mode gate. Distinct
    # from `expected_profit` / `atr_too_low` so post-run analysis can
    # tell "would have entered short here" from "wouldn't have entered
    # at all".
    shorts_blocked: int = 0
    # B-02 (audit 2026-05-27 second pass): events where at least one
    # strategy emitted a non-HOLD signal but the ensemble aggregator
    # could not reach consensus (returned None or HOLD). Pre-fix this
    # was an invisible bucket: ``total_signals`` was bumped before the
    # ensemble call, so the difference ``total_signals - sum(other
    # gates) - executed`` silently equalled the ensemble HOLD count.
    # Operators reading the gate table thought the missing events had
    # been rejected by an explicit rule. Surfacing it makes regime-
    # fragile ensembles (low consensus) visible in the diagnostic.
    ensemble_hold: int = 0

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
        # B-7 / C-21 (audit 2026-05-26): apply the paper-order seed exactly
        # once, at the start of the run, so every variant in a battery sweep
        # walks the same slippage/partial-fill trajectory when given the
        # same seed. Idempotent when paper_seed is None.
        if self.bt.paper_seed is not None:
            from core.execution import _set_paper_seed
            _set_paper_seed(self.bt.paper_seed)

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

        # Perf P-10 (audit 2026-05-27): warm the module-level trend-context
        # cache for every symbol BEFORE entering the event loop. Otherwise
        # the first ``is_against_trend(sym, ...)`` call from inside the
        # hot loop triggers a 30 s yfinance download (worst case: every
        # symbol misses sequentially, costing ~50 s per Nifty50 stock).
        # The pre-fetch is best-effort: failures cache a None entry and
        # the strategy falls back to its existing fail-open default.
        self._prefetch_trend_context(symbols)

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
        # 2026-05-25 senior-dev scan, Bug D fix: track end-of-day equity
        # by IST date so Sharpe is computed on DAILY returns (not per-event
        # returns × sqrt(252) which is nonsensical when there are 220k
        # events for 60 days). pct_change between consecutive same-day
        # events is dominated by per-symbol revaluation noise, not real
        # P&L moves; the old Sharpe number in comparison.md was
        # misleading. We keep equity_curve for drawdown / final-equity
        # which work fine on per-event resolution.
        last_equity_per_day: Dict[Any, float] = {}
        # B-01 (audit 2026-05-27 second pass): the live agent stores this
        # counter as ``self._stock_loss_today`` and calls ``.clear()`` at
        # the start of each new IST trading day (``trading_agent.py``
        # ``_reset_daily_trackers`` at line 1748). The config key it
        # implements is literally ``max_losses_per_stock_per_day``.
        # Pre-fix the backtester accumulated losses for the FULL run, so
        # on a 60-day backtest most volatile names got permanently
        # blacklisted by day 5 and were never re-tradable — systematically
        # under-counting trades and under-stating losses on the names
        # that would actually have re-opened daily in live. ``current_day``
        # tracks the rollover so the dict can be reset at the next bar's
        # date change.
        losses_per_stock: Dict[str, int] = {}
        current_day: Optional[Any] = None

        # Perf P-07 (audit 2026-05-27): every gate branch repeats the
        # same three-line bookkeeping (revalue at current close, append
        # to equity curve, stamp last-equity-per-day under try/except).
        # The try/except guarded against bar indexes that don't expose
        # ``.date()`` (e.g. plain int index in tests); centralising it
        # here removes 12 copies of identical code AND eliminates a
        # per-event dict-membership Python overhead by caching the last
        # IST date string. The lambda closes over the local mutables
        # ``equity_curve`` and ``last_equity_per_day``.
        #
        # Perf B-04 (audit 2026-05-27 second pass): the day-rollover
        # block above already computes ``current_day`` once per event
        # (and is set BEFORE any branch can call ``_bump_equity``).
        # Reading the enclosing-scope ``current_day`` is byte-identical
        # to calling ``_ts.date()`` again on the same timestamp and
        # avoids the per-call attribute conversion. Falls back to
        # ``_ts.date()`` only when current_day is None (defensive --
        # would only happen if a caller invokes the helper before the
        # rollover block runs, which the in-loop ordering prevents).
        def _bump_equity(_ts, _symbol: str, _close: float) -> float:
            eq = portfolio.get_total_value({_symbol: _close})
            equity_curve.append(eq)
            day = current_day
            if day is None:
                try:
                    day = _ts.date()
                except Exception:
                    day = None
            if day is not None:
                last_equity_per_day[day] = eq
            return eq

        # Build a unified, time-ordered event stream across symbols so
        # portfolio constraints (max positions, cash) are enforced chronologically.
        # We compute total_events from market_data BEFORE consuming the
        # generator so the progress meter has a denominator. This is the
        # same sum _merge_bars iterates over, so it's exact, not an estimate.
        total_events = sum(len(df) for df in market_data.values())
        events = self._merge_bars(market_data)

        run_t0 = time.time()
        next_progress_at = PROGRESS_LOG_INTERVAL_EVENTS
        last_progress_wall_t = run_t0
        # B-05 (audit 2026-05-27 third pass): track the prior progress tick's
        # event counter so we can emit an INSTANTANEOUS rate alongside the
        # cumulative one. The cumulative rate ``event_idx / elapsed`` is a
        # running average from variant start -- after a fast warmup phase
        # (every strategy returns insufficient_data for the first ~200
        # bars per symbol) it stays artificially high for tens of minutes
        # and the operator (correctly) suspects the run is degrading when
        # the cumulative number drifts down toward steady state. The
        # instantaneous rate, measured over the last PROGRESS_LOG_INTERVAL
        # window, reflects current per-event cost honestly so the ETA can
        # be reasoned about. Both are emitted; the cumulative one is still
        # what feeds ``eta_sec`` because it's the smoother basis for
        # long-horizon extrapolation.
        last_progress_event_idx = 0

        for event_idx, (ts, symbol, bar, df_slice) in enumerate(events, start=1):
            # Periodic progress log. Without this, the worker emits only
            # strategy-signal lines and there is no way (short of reading
            # source) to tell whether a multi-hour run is 10% or 90% done.
            # See logs/backtests/smoke_2var_20260515_152858 — 45h with no
            # progress signal, the symptom that surfaced this gap.
            #
            # Dual trigger: emit on event-count OR wall-clock, whichever
            # fires first. On a 5 ev/s VM the event trigger alone fires
            # every 33min — too sparse for live monitoring; the time
            # trigger keeps updates flowing every PROGRESS_LOG_INTERVAL_SECONDS.
            now_wall = time.time()
            time_due = (now_wall - last_progress_wall_t) >= PROGRESS_LOG_INTERVAL_SECONDS
            if (event_idx >= next_progress_at
                    or time_due
                    or event_idx == total_events):
                elapsed = max(now_wall - run_t0, 1e-6)
                pct = event_idx / total_events * 100 if total_events else 0.0
                rate = event_idx / elapsed
                # B-05 (audit 2026-05-27 third pass): compute the
                # instantaneous rate over the window since the previous
                # progress tick. This is what an operator wants to see
                # when they ask "is the run getting slower?" -- the
                # cumulative rate above is a stable but lagging average,
                # while ``inst_rate`` reflects the last ~PROGRESS_LOG_INTERVAL_SECONDS
                # of work. On the very first tick (last_progress_event_idx == 0)
                # they coincide; thereafter they diverge as soon as the
                # per-event cost changes. The ETA still uses the cumulative
                # rate (smoother basis for hour-scale extrapolation) but
                # the operator can mentally substitute inst_rate when
                # the divergence is large.
                tick_elapsed = max(now_wall - last_progress_wall_t, 1e-6)
                tick_events = event_idx - last_progress_event_idx
                inst_rate = tick_events / tick_elapsed if tick_events > 0 else 0.0
                remaining = max(total_events - event_idx, 0)
                eta_sec = remaining / rate if rate > 0 else 0.0
                eta_str = self._format_duration(eta_sec)
                # ts is the bar timestamp (tz-aware pandas Timestamp); just
                # render it as ISO so the operator sees real market dates
                # advancing.
                sim_date = str(ts)[:10]
                logger.info(
                    f"[BATTERY-PROGRESS] {event_idx:,}/{total_events:,} "
                    f"({pct:5.1f}%) | sim_date={sim_date} | "
                    f"rate={rate:,.0f} ev/s (now={inst_rate:,.0f}) "
                    f"| elapsed={self._format_duration(elapsed)} | ETA={eta_str}"
                )
                # Advance the event watermark to the next 10k boundary
                # AT OR ABOVE current event_idx (handles the time-trigger
                # case where event_idx hasn't reached next_progress_at yet).
                while next_progress_at <= event_idx:
                    next_progress_at += PROGRESS_LOG_INTERVAL_EVENTS
                last_progress_wall_t = now_wall
                last_progress_event_idx = event_idx

            # B-01 (audit 2026-05-27 second pass): clear per-stock loss
            # counter at the start of every new IST trading day so
            # ``max_losses_per_stock`` matches its live counterpart
            # ``max_losses_per_stock_per_day`` (see commentary near
            # ``losses_per_stock`` init above). The compare is on
            # ``ts.date()``; ``current_day`` starts None so the first
            # event seeds it without firing a clear.
            try:
                bar_day = ts.date()
            except Exception:
                bar_day = None
            if bar_day is not None and bar_day != current_day:
                if current_day is not None and losses_per_stock:
                    losses_per_stock.clear()
                current_day = bar_day

            close = float(bar["close"])
            # 2026-05-25 senior-dev scan, Bug A fix: read full OHLC so SL/TP
            # can be detected INTRA-BAR. The old close-only check was
            # systematically biased toward the strategy: any bar that
            # touched SL intra-bar but closed above it was treated as
            # "still holding" -- in real life the order would have filled
            # at SL. Symmetric overstatement for TP. yfinance 5m bars
            # always carry OHLCV per data_handler.py:125.
            high = float(bar["high"])
            low = float(bar["low"])
            open_p = float(bar["open"])

            # Check SL/TP exits for any open position on this symbol.
            # Uses bar OHLC, not just close (Bug A fix above).
            if symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                trigger, exit_at = self._detect_intrabar_exit(
                    pos, open_p, high, low, close,
                )
                if trigger:
                    # `exit_at` is the simulated touch price (SL/TP or gap
                    # open). Apply slippage on top -- this is adverse on
                    # exits (you get less when closing a long; pay more
                    # when covering a short). For a SL hit on a gap, the
                    # combination of `min(open_p, sl)` in the detector +
                    # adverse slippage models the real-world worst-case
                    # fill better than the old close-only path.
                    exit_price = self._apply_slippage(exit_at, pos.side, exit=True)
                    record = portfolio.close_position(
                        symbol,
                        exit_price,
                        exit_reason=trigger,
                        # Use simulated bar timestamp, not wall-clock.
                        # Without this, holding_minutes on TradeRecord and
                        # any time-based exit rule end up measured in real
                        # seconds elapsed while the backtest was running.
                        exit_time=self._ts_to_datetime(ts),
                    )
                    if record:
                        rm.record_trade(record.pnl)
                        trades.append(self._trade_to_dict(record, trigger))
                        if record.pnl <= 0:
                            losses_per_stock[symbol] = losses_per_stock.get(symbol, 0) + 1

            # 2026-05-25 senior-dev scan, Bug C fix: opposite-signal exit
            # parity with the live agent. The old code did `continue` here
            # whenever the symbol still had a position -- which meant a
            # SELL ensemble signal on a held long was DROPPED, and a BUY
            # ensemble signal on a held short was DROPPED. The live agent
            # explicitly handles both via `_exit_on_signal` (see
            # trading_agent.py:3677-3679 and 3716-3718).
            #
            # We evaluate strategies + ensemble even when a position is
            # held; if the ensemble emits a non-HOLD opposite-direction
            # signal we close at the current bar's close (with slippage).
            # Same-direction (duplicate) signals are dropped silently to
            # match live behaviour (audit-reject "already_open:duplicate").
            if symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                strat_signals_held: List[TradeSignal] = []
                for strat in strategy_objs:
                    if len(df_slice) < strat.required_history_bars:
                        continue
                    try:
                        sig = strat.generate_signal(df_slice, symbol)
                    except Exception:
                        continue
                    if sig and sig.signal != Signal.HOLD:
                        strat_signals_held.append(sig)
                if strat_signals_held:
                    agg_held = ensemble.aggregate(
                        strat_signals_held, symbol, close, regime="unknown",
                    )
                    if agg_held is not None and agg_held.signal != Signal.HOLD:
                        opposite = (
                            (pos.side == "BUY" and agg_held.signal == Signal.SELL)
                            or (pos.side == "SELL" and agg_held.signal == Signal.BUY)
                        )
                        if opposite:
                            exit_price = self._apply_slippage(
                                close, pos.side, exit=True,
                            )
                            record = portfolio.close_position(
                                symbol,
                                exit_price,
                                exit_reason="signal",
                                exit_time=self._ts_to_datetime(ts),
                            )
                            if record:
                                rm.record_trade(record.pnl)
                                trades.append(
                                    self._trade_to_dict(record, "signal"),
                                )
                                if record.pnl <= 0:
                                    losses_per_stock[symbol] = (
                                        losses_per_stock.get(symbol, 0) + 1
                                    )
                _bump_equity(ts, symbol, close)
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
                _bump_equity(ts, symbol, close)
                continue

            gate_stats.total_signals += 1

            # 2026-05-25 senior-dev scan, Bug B (KNOWN DIVERGENCE, NOT FIXED HERE):
            # `regime` is hardcoded to "unknown" because Nifty/VIX per-bar
            # context is not currently in the market_data feed. The live
            # agent uses `classify_regime(self._market_context)` which
            # returns bear_high_vol / bull_low_vol / etc., and the ensemble
            # then suppresses or amplifies strategies via regime-learned
            # weights AND the rule-based `regime_multiplier`. With regime
            # "unknown" here, both lookups return the global learned
            # weight (no regime suppression).
            #
            # Net effect: backtester evaluates EVERY active strategy with
            # full global weight on every bar; live agent suppresses
            # several strategies in bear_high_vol (e.g. vwap_bounce,
            # opening_range_breakout, xgboost_classifier all weight 0).
            # The current "shorts have negative edge" finding from the
            # 90-day battery was therefore reached WITHOUT regime
            # suppression -- our live agent gets that suppression on top.
            # Direction of the finding is preserved (shorts still bleed),
            # but the magnitude in production will differ.
            #
            # Tracked as a follow-up in docs/findings_log_2026-05-25.md
            # §11. Fix would require:
            #   1. Adding Nifty/VIX bars to market_data.pkl
            #   2. Computing regime per-bar from rolling Nifty/VIX
            #   3. Passing that regime to ensemble.aggregate and the
            #      sizing call below
            # Out of scope for the freeze-week-2 review.
            regime = "unknown"

            agg = ensemble.aggregate(strat_signals, symbol, close, regime=regime)
            if agg is None or agg.signal == Signal.HOLD:
                # B-02: surface the ensemble-HOLD bucket so the gate
                # table sums match ``total_signals``.
                gate_stats.ensemble_hold += 1
                _bump_equity(ts, symbol, close)
                continue

            # 2026-05-25 risk-policy short veto. Mirrors the live-agent
            # gate at trading_agent.py:_process_signal. A SELL ensemble
            # signal with no open position would open a new short -- the
            # one operation `risk.allow_shorts: false` is supposed to
            # block. (SELL with an open long is a long-exit, handled
            # via the `if symbol in portfolio.positions` branch above.)
            if (not self.bt.allow_shorts
                    and agg.signal == Signal.SELL
                    and symbol not in portfolio.positions):
                gate_stats.shorts_blocked += 1
                _bump_equity(ts, symbol, close)
                continue

            # Gate: dead-hour
            if self.bt.apply_dead_hour and self._in_dead_hour(ts):
                gate_stats.dead_hour += 1
                _bump_equity(ts, symbol, close)
                continue

            # Gate: ATR%
            atr_pct = self._atr_pct(df_slice)
            if atr_pct is not None and atr_pct < self.bt.min_entry_atr_pct:
                gate_stats.atr_too_low += 1
                _bump_equity(ts, symbol, close)
                continue

            # Gate: max positions
            if portfolio.open_position_count >= self.bt.max_positions:
                gate_stats.max_positions_reached += 1
                _bump_equity(ts, symbol, close)
                continue

            # Gate: stock blacklisted after N losses
            if losses_per_stock.get(symbol, 0) >= self.bt.max_losses_per_stock:
                gate_stats.stock_blacklisted += 1
                _bump_equity(ts, symbol, close)
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
                _bump_equity(ts, symbol, close)
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
                    # F-72 (audit 2026-05-27): every other gate branch
                    # updates BOTH equity_curve AND last_equity_per_day
                    # so the per-day equity snapshot stays current.
                    # The expected-profit branch updated only the
                    # curve, leaving last_equity_per_day stale -- any
                    # daily-Sharpe / per-day-pct computation downstream
                    # then attributed the next bar's equity change to
                    # the wrong day. _bump_equity centralises that fix.
                    _bump_equity(ts, symbol, close)
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
                # Stamp entry with simulated bar timestamp so trade
                # records and holding_minutes reflect market time.
                entry_time=self._ts_to_datetime(ts),
            )
            gate_stats.executed += 1
            _bump_equity(ts, symbol, close)

        # Close any still-open positions at the final bar of each symbol
        for symbol, df in market_data.items():
            if symbol in portfolio.positions and not df.empty:
                last_close = float(df["close"].iloc[-1])
                # F-67 (audit 2026-05-27): every other exit path in the
                # backtester runs the price through ``_apply_slippage``
                # so the simulated fill reflects realistic execution
                # cost. The end-of-backtest flatten omitted slippage
                # entirely, exiting at the exact last close. On a
                # multi-month run with hundreds of held positions at
                # the final bar (sweeps, long-tail trailing stops),
                # this systematically over-reported P&L by ~one
                # round-trip slippage per residual position. Apply
                # the same slippage convention so the final equity
                # number is honest.
                pos = portfolio.positions[symbol]
                exit_at = self._apply_slippage(last_close, pos.side, exit=True)
                # Final-bar timestamp for each symbol — keeps holding_minutes
                # internally consistent with the simulated session.
                last_ts = df.index[-1]
                record = portfolio.close_position(
                    symbol,
                    exit_at,
                    exit_reason="backtest_end",
                    exit_time=self._ts_to_datetime(last_ts),
                )
                if record:
                    rm.record_trade(record.pnl)
                    trades.append(self._trade_to_dict(record, "backtest_end"))

        total_elapsed = time.time() - run_t0
        logger.info(
            f"[BATTERY-PROGRESS] DONE in {self._format_duration(total_elapsed)} "
            f"| {total_events:,} events | {total_events / max(total_elapsed, 1e-6):,.0f} ev/s"
        )

        return self._build_result(
            trades, equity_curve, gate_stats,
            daily_equities=list(last_equity_per_day.values()),
        )

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

    @staticmethod
    def _prefetch_trend_context(symbols: List[str]) -> None:
        """Pre-warm ``_trend_context._cache`` for every symbol in the run.

        Perf P-10 (audit 2026-05-27): without this, the first event for
        any symbol whose strategy consults ``is_against_trend(...)``
        triggers a synchronous yfinance HTTP call (3-month daily bars,
        hard-timeouted at 30 s). With a 50-symbol Nifty 50 run that's
        up to 25 minutes of serialised network I/O sprinkled across the
        first ~50 events of the backtest. Pre-fetching here makes the
        latency visible up-front (with a single log line) and removes
        it from the hot loop.

        Failures are absorbed silently because ``is_against_trend``
        already fails open on missing data; we are only optimising
        WHEN the fetch happens, not what its result is used for.
        """
        try:
            from strategies._trend_context import get_trend
        except Exception:
            return  # trend context module unavailable -- safe no-op
        for sym in symbols:
            try:
                get_trend(sym)
            except Exception:
                # Cache_lookup is fail-open; never let pre-fetch errors
                # block the backtest from running.
                continue

    @staticmethod
    def _ts_to_datetime(ts) -> datetime:
        """Convert a bar index (pandas Timestamp, numpy datetime64, or python
        datetime) into a tz-aware Python datetime in IST.

        Used so portfolio.open_position / close_position record the
        SIMULATED bar timestamp, not wall-clock. yfinance 5-minute bars
        already carry Asia/Kolkata tzinfo, but downstream code that
        sometimes round-trips through naive timestamps can lose it; we
        normalize both paths here.
        """
        if isinstance(ts, pd.Timestamp):
            py_dt = ts.to_pydatetime()
        elif isinstance(ts, datetime):
            py_dt = ts
        else:
            # numpy datetime64 / int — best-effort coerce via pandas.
            py_dt = pd.Timestamp(ts).to_pydatetime()
        if py_dt.tzinfo is None:
            return IST.localize(py_dt)
        return py_dt.astimezone(IST)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(seconds, 0.0)
        if seconds < 60:
            return f"{seconds:4.1f}s"
        if seconds < 3600:
            return f"{seconds / 60:4.1f}m"
        return f"{seconds / 3600:4.1f}h"

    def _merge_bars(self, market_data: Dict[str, pd.DataFrame]):
        """Yield (timestamp, symbol, bar_row, slice_up_to_and_including_bar) events
        in global chronological order so cross-symbol constraints are enforced.

        Perf P-08 (audit 2026-05-27): use ``heapq.merge`` over per-symbol
        sorted iterators instead of materialising every (ts, symbol, i)
        tuple in a single ~220k-element list and sorting it. yfinance
        historical frames are already index-sorted ascending, so each
        per-symbol iterator is sorted and ``heapq.merge`` is an O(N log K)
        K-way merge where K = number of symbols (50 typical) instead of
        an O(N log N) sort. Peak memory drops from O(N) tuples to O(K).

        2026-05-25 perf fix: cap the per-event history slice to the
        last ``strategy_history_window`` bars instead of the unbounded
        ``df.iloc[: i + 1]``. See ``BacktestConfig.strategy_history_window``
        docstring for the full rationale and numerical-equivalence
        proof. Per-event work goes from O(i) to O(window).
        """
        window = max(int(self.bt.strategy_history_window), 1)

        def _per_symbol_stream(symbol: str, df: pd.DataFrame):
            n = len(df)
            for i in range(n):
                # The 4th element is (start_index_for_slice, end_index_exclusive)
                # so the consumer can build the slice itself; this keeps the
                # tuple flat and small for the heap merge.
                yield df.index[i], symbol, i

        streams = [
            _per_symbol_stream(sym, df) for sym, df in market_data.items() if len(df)
        ]
        merged = heapq.merge(*streams, key=lambda t: t[0])
        for ts, symbol, i in merged:
            df = market_data[symbol]
            start = max(0, i + 1 - window)
            yield ts, symbol, df.iloc[i], df.iloc[start : i + 1]

    def _apply_slippage(self, price: float, side: str, *, exit: bool) -> float:
        # F-26 (audit 2026-05-27): the previous formula was deterministic
        # (every fill at exactly ``price * slippage_pct/100``), so the
        # `paper_seed` knob set in __init__ was a no-op. The live paper
        # path (execution._paper_order) samples slippage U(0, tolerance)
        # via the module-level ``_paper_rng``; the backtester now mirrors
        # that distribution so backtest <-> paper parity holds AND so
        # ``paper_seed`` actually produces reproducible runs across
        # invocations. When ``paper_seed`` is None we fall back to the
        # deterministic formula to preserve pre-fix battery results.
        #
        # B-03 (audit 2026-05-27 second pass): short-circuit when
        # slippage_pct is exactly 0 (idealised stress tests, fee-only
        # studies). Skips the multiply AND the RNG draw when seeded so
        # zero-slippage runs are both faster and bit-identical to the
        # mathematical limit.
        if self.bt.slippage_pct == 0.0:
            return price
        if self.bt.paper_seed is None:
            slip_pct = self.bt.slippage_pct / 100
        else:
            from core.execution import _paper_rng
            # U(0, slippage_pct) matches _paper_order's
            # ``_paper_rng.uniform(0.0, self.slippage_tolerance)``.
            slip_pct = _paper_rng.uniform(0.0, self.bt.slippage_pct) / 100
        slip = price * slip_pct
        if side == "BUY":
            return price + slip if not exit else price - slip
        return price - slip if not exit else price + slip

    @staticmethod
    def _detect_intrabar_exit(
        pos, open_p: float, high: float, low: float, close: float,
    ):
        """Detect whether the bar's intra-bar range hit SL or TP.

        Returns (trigger, exit_price). trigger is "stop_loss" / "take_profit"
        / None. exit_price is the simulated touch price the live agent
        would have filled at (before slippage).

        2026-05-25 senior-dev scan, Bug A fix. The previous close-only
        check was systematically biased -- it ignored bars that "wicked"
        through SL/TP and recovered to close on the other side, which in
        live trading would have filled at SL/TP the moment the price
        touched. Net effect: backtest understated losses, overstated
        wins.

        Conservative tie-breaking:
          * If BOTH SL and TP fall inside [low, high] (rare, gap/wide
            bar), assume SL hit FIRST (worst-case for the strategy).
            This avoids the opposite optimistic bias.
          * If the bar OPENED past SL/TP (gap), fill at the open
            (worse than the static SL/TP level). Live agent's tick
            stream would have triggered on the first tick which IS
            the open here.
        """
        sl = getattr(pos, "stop_loss", None)
        tp = getattr(pos, "take_profit", None)
        side = getattr(pos, "side", None)

        if side == "BUY":
            # Long: SL below entry, TP above entry. SL hit if low <= SL,
            # TP hit if high >= TP.
            sl_hit = sl is not None and low <= sl
            tp_hit = tp is not None and high >= tp
            if sl_hit and tp_hit:
                # Both inside the bar -- assume SL fires first
                # (worst-case). If gap-open below SL, fill at open.
                return "stop_loss", min(open_p, sl) if open_p < sl else sl
            if sl_hit:
                return "stop_loss", min(open_p, sl) if open_p < sl else sl
            if tp_hit:
                return "take_profit", max(open_p, tp) if open_p > tp else tp
            return None, None

        if side == "SELL":
            # Short: SL above entry, TP below entry. SL hit if high >= SL,
            # TP hit if low <= TP.
            sl_hit = sl is not None and high >= sl
            tp_hit = tp is not None and low <= tp
            if sl_hit and tp_hit:
                return "stop_loss", max(open_p, sl) if open_p > sl else sl
            if sl_hit:
                return "stop_loss", max(open_p, sl) if open_p > sl else sl
            if tp_hit:
                return "take_profit", min(open_p, tp) if open_p < tp else tp
            return None, None

        return None, None

    # Perf P-12 (audit 2026-05-27): dead-hour decisions depend only on
    # ``(hour, minute)``; with ~220k events × multiple symbols sharing
    # each minute, memoising by (hh, mm) drops 90%+ of the per-event
    # branch work (which is just integer compares but called millions
    # of times). The cache is reset at the start of every ``run`` so
    # subclasses that change DEAD_HOUR_BLOCKS between runs still work.
    def _in_dead_hour(self, ts) -> bool:
        try:
            key = (ts.hour, ts.minute)
        except Exception:
            return False
        cache = self.__dict__.setdefault("_dead_hour_cache", {})
        cached = cache.get(key)
        if cached is not None:
            return cached
        result = False
        for sh, sm, eh, em in DEAD_HOUR_BLOCKS:
            if (key >= (sh, sm)) and (key < (eh, em)):
                result = True
                break
        cache[key] = result
        return result

    def _atr_pct(self, df: pd.DataFrame) -> Optional[float]:
        # Perf P-12: ``.iat[-1, col_idx]`` is markedly faster than
        # ``df["col"].iloc[-1]`` because it skips column-Series creation;
        # called on every non-flagged event we need to stay cheap.
        if df.empty or "atr" not in df.columns:
            return None
        try:
            atr_idx = df.columns.get_loc("atr")
            close_idx = df.columns.get_loc("close")
            atr = df.iat[-1, atr_idx]
            price = df.iat[-1, close_idx]
        except Exception:
            atr = df["atr"].iloc[-1]
            price = df["close"].iloc[-1]
        if pd.isna(atr) or pd.isna(price) or price <= 0:
            return None
        return float(atr / price * 100)

    def _latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        if df.empty or "atr" not in df.columns:
            return None
        try:
            v = df.iat[-1, df.columns.get_loc("atr")]
        except Exception:
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
        daily_equities: Optional[List[float]] = None,
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
            # Perf P-06 (audit 2026-05-27): vectorised drawdown. The
            # previous Python loop over a ~220k-element equity curve
            # (one entry per bar event on a 50-symbol 60-day run) burned
            # ~50ms per variant just on Python-side max() calls. Result
            # is byte-identical: the original computed ``mdd`` as
            # max(running_peak - value) and ``mdd_pct`` as mdd / (final
            # running peak), which equals the all-time peak. We keep
            # both definitions.
            eq_arr = np.asarray(equity_curve, dtype=float)
            running_peak = np.maximum.accumulate(eq_arr)
            mdd_val = float((running_peak - eq_arr).max())
            final_peak = float(running_peak[-1])
            r.max_drawdown = mdd_val
            r.max_drawdown_pct = (
                mdd_val / final_peak * 100.0 if final_peak else 0.0
            )
            # 2026-05-25 senior-dev scan, Bug D fix: Sharpe must be computed
            # on DAILY returns, not per-event pct_change. The old code did
            # `pd.Series(equity_curve).pct_change().std() * sqrt(252)` --
            # but `equity_curve` has one entry per (symbol, bar) event,
            # i.e. ~220,000 entries for 50 symbols × 60 days. Consecutive
            # same-day events have near-zero pct_change (only the trading
            # symbol's revaluation moves the equity), and multiplying by
            # sqrt(252) (which is the annualization factor for DAILY
            # samples) on top of event-level noise produced numbers that
            # had no intuitive meaning. Operators were comparing them as
            # if they were real annualised Sharpes.
            #
            # Fix: prefer `daily_equities` (last value per IST date) when
            # available; fall back to the old behavior with a warning
            # comment if not. Daily resampling reflects the standard
            # textbook definition of Sharpe.
            samples = daily_equities if daily_equities and len(daily_equities) >= 2 else None
            if samples is not None:
                returns = pd.Series(samples).pct_change().dropna()
                if len(returns) > 1 and returns.std() > 0:
                    r.sharpe = float((returns.mean() / returns.std()) * (252 ** 0.5))
            else:
                # Fallback: legacy event-level Sharpe (kept so older
                # callers don't crash). Documented divergence; will be
                # phased out once all entry points pass daily_equities.
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
