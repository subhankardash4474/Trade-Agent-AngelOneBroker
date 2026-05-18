"""
Trading Agent - Main Orchestrator (v2)
Coordinates WebSocket data, tick aggregation, feature engineering,
ensemble strategy execution, risk management, and order placement
into a continuous, production-grade trading loop.
"""

import os
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytz
import yaml
from loguru import logger

from core.cooldown_persistence import load_cooldown_state, save_cooldown_state
from core.runtime_state_persistence import load_runtime_state, save_runtime_state
from core.trailing_stop_persistence import (
    load_trailing_states,
    restore_trailing_state,
    save_trailing_states,
)
from core.data_handler import DataHandler
from core.database import Database
from strategies.ensemble import EnsembleModel
from core.execution import ExecutionEngine
from core.features import FeatureEngine
from core.portfolio import Portfolio
from core.market_safety import (
    check_circuit_risk,
    check_data_quality,
    check_sector_exposure,
    get_sector,
)
from core.regime import classify_regime, regime_multiplier
from core.risk_manager import RiskManager
from core.secrets import apply_env_to_config, load_dotenv, warn_if_secrets_in_yaml
from core.signal_audit import SignalAudit
from core.stock_scanner import StockScanner
from core.trade_analyzer import TradeAnalyzer
from core.tick_aggregator import TickAggregator
from core.websocket_client import WebSocketClient
from monitoring.alerts import AlertManager
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal

IST = pytz.timezone("Asia/Kolkata")

# Full strategy registry (rule-based + ML)
STRATEGY_REGISTRY = {}


def _load_registry():
    global STRATEGY_REGISTRY
    from strategies.moving_average_crossover import MovingAverageCrossover
    from strategies.rsi_momentum import RSIMomentum
    from strategies.mean_reversion import MeanReversion
    from strategies.vwap_bounce import VWAPBounce
    from strategies.opening_range_breakout import OpeningRangeBreakout
    from strategies.supertrend_follow import SupertrendFollow

    STRATEGY_REGISTRY.update({
        "moving_average_crossover": MovingAverageCrossover,
        "rsi_momentum": RSIMomentum,
        "mean_reversion": MeanReversion,
        "vwap_bounce": VWAPBounce,
        "opening_range_breakout": OpeningRangeBreakout,
        "supertrend_follow": SupertrendFollow,
    })

    # ML strategies (optional deps)
    try:
        from strategies.xgboost_classifier import XGBoostClassifier
        STRATEGY_REGISTRY["xgboost_classifier"] = XGBoostClassifier
    except Exception:
        logger.debug("XGBoost strategy not available (missing xgboost package)")

    try:
        from strategies.lstm_model import LSTMPriceModel
        STRATEGY_REGISTRY["lstm_price_model"] = LSTMPriceModel
    except Exception:
        logger.debug("LSTM strategy not available (missing torch package)")


_load_registry()


class TradingAgent:
    """
    Autonomous trading agent for the Indian stock market (v2).

    Architecture:
      WebSocket ticks → Tick Aggregator → Feature Engine → Strategies
      → Ensemble Meta-Model → Risk Manager → Execution Engine → Portfolio

    Supports both polling mode (REST API) and streaming mode (WebSocket).
    """

    # Per-symbol consecutive DQ failures before we escalate to WARNING.
    # Anything below this stays at DEBUG to preserve signal-to-noise ratio.
    _DQ_WARN_AFTER = 5

    def __init__(self, config_path: str = "config.yaml", smart_api=None,
                 reset_balance: bool = False,
                 max_loss_rs: Optional[float] = None,
                 single_shot: bool = False,
                 config: Optional[dict] = None):
        """
        Optional kwargs (driven by run_daemon.py CLI flags):
            max_loss_rs: hard rupee floor on daily realised P&L (Stage 3 safety
                net). When the day's P&L drops to <= -max_loss_rs, RiskManager
                trips its circuit breaker and refuses new entries. None=disabled.
            single_shot: if True, once any symbol has completed a full round-trip
                (entered + exited) within the day it cannot be re-entered. Used
                for Stage 3 live basket runs to bound the maximum number of
                fills per symbol. Existing position management (SL/TP/trailing)
                is unaffected.
            config: pre-loaded config dict. When supplied (by ``run_daemon`` after
                applying ``--config-overlay`` / ``--live`` overrides) we skip the
                disk read so the deltas don't get silently dropped on re-parse.
                The env-var overlay (``apply_env_to_config``) still runs so
                credentials in ``.env`` continue to win over YAML placeholders.
                If None, we fall back to the legacy path: load ``config_path``
                from disk. Backwards-compatible.
        """
        if config is not None:
            # Pre-loaded config (e.g. overlay-merged from run_daemon). Still
            # apply env-var overrides so .env secrets continue to win.
            load_dotenv()
            self.config = apply_env_to_config(dict(config))
        else:
            self.config = self._load_config(config_path)
        self._setup_logging()

        # E2E / Stage 3 safety knobs. Stored before risk_manager build so
        # we can pass max_loss_rs straight through.
        self._single_shot_enabled: bool = bool(single_shot)
        self._max_loss_rs: Optional[float] = (
            float(max_loss_rs) if max_loss_rs is not None else None
        )
        # Set of symbols whose round-trip is complete for today. Reset at
        # day boundary by `_reset_single_shot_state_if_new_day`.
        self._symbols_done_today: set[str] = set()
        self._single_shot_day: Optional[date] = datetime.now(IST).date()

        capital_cfg = self.config.get("capital", {})
        config_initial_balance = capital_cfg.get("initial_balance", 10000.0)

        # Core components (database first — portfolio needs it for position recovery)
        self.database = Database(self.config.get("database", {}).get("path", "data/trading_agent.db"))

        # Decide the effective starting balance:
        #   - Live mode: use broker-reported funds (source of truth).
        #   - Paper mode: continue from last DB equity snapshot unless
        #     --reset-balance is passed or no history exists.
        effective_balance, historical_peak = self._resolve_starting_balance(
            config_initial_balance, reset_balance, smart_api
        )

        self.data_handler = DataHandler(self.config, smart_api=smart_api)
        self.risk_manager = RiskManager(
            self.config,
            initial_balance=effective_balance,
            peak_balance=historical_peak,
            absolute_daily_loss_floor_rs=self._max_loss_rs,
        )
        if self._max_loss_rs is not None:
            logger.warning(
                f"[E2E] Stage 3 absolute daily-loss floor armed: "
                f"-Rs {self._max_loss_rs:,.2f} (in addition to "
                f"the {self.risk_manager.daily_loss_limit_pct:.1f}% "
                f"percentage limit)"
            )
        if self._single_shot_enabled:
            logger.warning(
                "[E2E] Stage 3 single-shot mode armed: each symbol can "
                "round-trip only once per day."
            )
        # Replay today's already-closed trades into the risk counters so a
        # mid-session daemon restart preserves daily_pnl / daily_trades /
        # consecutive_losses. Without this, the EOD email reports Rs +0.00
        # on a restarted daemon even when prior daemons closed real trades
        # (live bug, 2026-05-04: 4 trades = Rs -2.25 shown as Rs +0.00).
        try:
            today_iso = datetime.now(IST).date().isoformat()
            todays_trades = self.database.load_trades_for_day(today_iso)
            self.risk_manager.rehydrate_daily_state(todays_trades)
            # Single-shot mode (Stage 3 e2e): rehydrate the per-symbol
            # round-trip set from today's closed trades. Without this, a
            # daemon restart at 14:00 would let already-round-tripped
            # symbols re-enter -- defeating the kill-switch's purpose.
            for t in todays_trades:
                sym = t.get("symbol")
                if sym:
                    self._symbols_done_today.add(sym)
            if self._single_shot_enabled and self._symbols_done_today:
                logger.info(
                    f"[SINGLE-SHOT] Rehydrated {len(self._symbols_done_today)} "
                    f"symbols from today's closed trades: "
                    f"{sorted(self._symbols_done_today)}"
                )
        except Exception as e:
            logger.warning(f"Could not rehydrate daily risk state from DB: {e}")

        self.execution = ExecutionEngine(self.config, smart_api=smart_api, database=self.database)

        # P2 restart-cluster (2026-05-17) -- LIVE-MODE SAFETY: track when the
        # AngelOne JWT was minted so the main loop can refresh it before it
        # expires. JWT lifetime is 8h; we attempt re-login at 7h. Skipped in
        # paper mode (no smart_api).
        self._broker_session_started_at: Optional[datetime] = (
            datetime.now(IST) if smart_api is not None else None
        )
        self._smart_api = smart_api  # kept for re-login swap
        self._jwt_refresh_attempted_at: Optional[datetime] = None
        self.portfolio = Portfolio(
            initial_balance=effective_balance,
            commission_pct=self.config.get("backtest", {}).get("commission_pct", 0.03),
            log_dir=self.config.get("logging", {}).get("log_dir", "logs"),
            database=self.database,
            product_type=self.config.get("execution", {}).get("product_type", "INTRADAY"),
            reset_balance=reset_balance,
        )

        # P0 #2 (2026-05-15) — LIVE-MODE SAFETY: serialize exit checks.
        # `_check_position_exits` runs from BOTH the main scan loop (fast-
        # exits poll every ~15s) AND the WebSocket background thread (every
        # tick). Without this lock, the same symbol's SL/peak-giveback gate
        # can be tripped concurrently by both paths; both submit flatten
        # orders before either close_position clears the in-memory position.
        # Result: doubled exit orders on a fast move — near-100% probability
        # on the first WS-enabled day with any volatile tick.
        #
        # P0 #2 residual (2026-05-18) — the original Lock was only acquired
        # in `_check_position_exits`, not in `_close_position_safely`. The
        # signal-driven exit path (`_exit_on_signal`), the EOD square-off
        # (`_square_off_all`), and the carryover profit-lock could all call
        # `_close_position_safely` concurrently with the WS-tick exit path
        # and re-open the same double-flatten window we set out to close.
        # Fix: lock moved into `_close_position_safely` (the only place we
        # actually submit a flatten order). Lock upgraded to RLock so the
        # existing `_check_position_exits_locked` body — which already
        # holds the lock and then calls `_close_position_safely` for each
        # decided-to-close symbol — does NOT deadlock on re-entry.
        self._exit_check_lock = threading.RLock()

        # 2026-05-18 (Audit Issue #2): boot-time reconcile fires CRITICAL
        # findings (orphan SL cancel-fail, position MISMATCH) BEFORE
        # ``self.alert_manager`` is constructed a hundred-odd lines below,
        # so the inline ``send_alert`` calls used to raise AttributeError
        # and get swallowed by a bare ``except``. We now buffer the alert
        # payloads here and flush them right after AlertManager exists.
        # List of dicts with keys: ``title``, ``message``, ``level``.
        self._pending_boot_alerts: List[Dict[str, str]] = []

        # P0 #4 (2026-05-15) — LIVE-MODE SAFETY: restart SL reconciliation.
        # If the daemon was restarted mid-session with open positions, the
        # portfolio rehydrates from DB but the fresh ExecutionEngine has an
        # empty `_sl_orders_by_symbol`. The broker still holds the original
        # SL-M legs. Without this step, `update_sl_trigger_for_symbol`
        # silently no-ops (trail SL never propagates after restart) and
        # `cancel_sl_order_for_symbol` silently no-ops (next close leaves
        # an orphaned SL behind that can fire as a reverse trade). Yesterday's
        # SL-tracking PR exactly tried to close this hole — it stayed open
        # for restart paths.
        #
        # P0 #3 residual fix (2026-05-18, Audit Bug #1) — the boot-time call
        # USED to be gated on ``if self.portfolio.positions:`` which defeated
        # the orphan / duplicate SL sweep added on 2026-05-18: the exact
        # crash mode the sweep was designed for is a daemon crash AFTER the
        # entry order filled but BEFORE the DB write, so on restart
        # ``self.portfolio.positions == {}`` while a live SL-M sits on the
        # broker. Removing the caller-side gate is the contract change the
        # new `reconcile_sl_orders_from_broker` requires (it is now
        # contract-bound to sweep orphans even with an empty restored-positions
        # dict; paper mode + paper-engine are still no-ops internally).
        try:
            self.execution.reconcile_sl_orders_from_broker(
                self.portfolio.positions
            )
        except Exception as e:
            logger.error(
                f"[SL-RECONCILE] reconciliation failed unexpectedly ({e}); "
                f"restored positions remain unprotected in tracking and any "
                f"orphan broker SL-M legs were NOT swept. "
                f"Continuing boot — manual SL audit recommended."
            )

        # P2 restart-cluster (2026-05-17) -- LIVE-MODE SAFETY: position
        # reconciliation with the broker. If the broker auto-flattened a
        # position out-of-band (RMS rule violation, margin call, manual
        # operator close via the broker UI) while the daemon was down, the
        # DB / portfolio still shows the trade as open. Trading on a ghost
        # position is bad: we'd hold a phantom seat in the position-cap
        # while having nothing to actually exit. P0 #4 closed the same hole
        # for the SL-order side; this closes it for the underlying position.
        try:
            if self.portfolio.positions:
                # Serialize positions for the reconcile call (just side and
                # quantity needed for the comparison).
                restored_for_reconcile = {
                    sym: {"side": pos.side, "quantity": pos.quantity}
                    for sym, pos in self.portfolio.positions.items()
                }
                report = self.execution.reconcile_positions_with_broker(
                    restored_for_reconcile
                )
                for sym, entry in report.items():
                    status = entry.get("status")
                    if status == "orphan":
                        # Best-effort: close DB-side with a synthetic
                        # zero-pnl entry-price exit so the strategy
                        # attribution doesn't get a misleading P&L
                        # (we don't know the broker's actual fill price).
                        pos = self.portfolio.positions.get(sym)
                        if not pos:
                            continue
                        try:
                            self.portfolio.close_position(
                                symbol=sym,
                                exit_price=pos.entry_price,  # zero PnL
                                exit_time=datetime.now(IST),
                                exit_reason="broker_reconcile_at_boot",
                            )
                            logger.critical(
                                f"[POSITION-RECONCILE] {sym} closed DB-side "
                                f"with reason=broker_reconcile_at_boot. "
                                f"P&L recorded as zero (true broker fill "
                                f"price unknown). Operator action: review "
                                f"broker statement for actual fill."
                            )
                        except Exception as e:
                            logger.critical(
                                f"[POSITION-RECONCILE] {sym} orphan-close "
                                f"failed: {e!r}. Position remains in DB "
                                f"-- manual cleanup required."
                            )
                    elif status == "mismatch":
                        # 2026-05-18 (Regression #3): broker has a position
                        # for this symbol but on the wrong side or wrong
                        # qty (half-fill / flipped side). Don't touch the
                        # DB position (we don't know which side is correct
                        # without operator review), but block new entries
                        # on this symbol for the day by leveraging the
                        # existing per-stock-loss blacklist. Caller of
                        # ``_is_stock_blacklisted`` already gates entries.
                        try:
                            blacklist_threshold = max(
                                self._max_losses_per_stock, 1
                            )
                            self._stock_loss_today[sym] = blacklist_threshold
                            logger.critical(
                                f"[POSITION-RECONCILE] {sym} flagged MISMATCH "
                                f"-- new entries blocked via "
                                f"stock_loss_today={blacklist_threshold}. "
                                f"Reasons: {entry.get('reasons')}. "
                                f"Manual broker-vs-DB audit required."
                            )
                            # 2026-05-18 (Audit Issue #2): the alert call USED
                            # to be inline here, but ``self.alert_manager`` is
                            # constructed several lines below -- the call
                            # always raised AttributeError and was silently
                            # swallowed by the bare ``except Exception``. Plus
                            # ``level=`` was missing so even after the alert
                            # could land it would route through the default
                            # ``info`` channel rather than the critical
                            # channel an op would actually wake up for. Now
                            # we buffer the alert payload and dispatch it
                            # right after AlertManager is constructed (see
                            # ``_dispatch_pending_boot_alerts`` below).
                            self._pending_boot_alerts.append({
                                "title": f"[CRITICAL] {sym} broker/DB mismatch",
                                "message": (
                                    f"Reconciliation found "
                                    f"db={entry.get('db_side')} "
                                    f"qty={entry.get('db_quantity')} vs "
                                    f"broker={entry.get('broker_side')} "
                                    f"qty={entry.get('broker_quantity')}. "
                                    f"Trading blocked on this symbol "
                                    f"for the rest of the session."
                                ),
                                "level": "critical",
                            })
                        except Exception as e:
                            logger.critical(
                                f"[POSITION-RECONCILE] {sym} mismatch-block "
                                f"failed: {e!r}. Manual intervention required."
                            )
        except Exception as e:
            logger.error(
                f"[POSITION-RECONCILE] boot reconcile failed unexpectedly "
                f"({e}); positions left as DB. Manual broker-vs-DB audit "
                f"recommended before resuming trading."
            )
        self.alert_manager = AlertManager(self.config)

        # 2026-05-18 (Audit Issue #2): flush any CRITICAL alerts that the
        # boot-time reconcile blocks queued up before AlertManager existed.
        # These are mostly position MISMATCH and orphan-SL cancel failures
        # detected at startup. We deliberately flush AFTER AlertManager so
        # the alerts land on the configured critical channel rather than
        # being lost to a swallowed AttributeError.
        self._flush_pending_boot_alerts()

        self.feature_engine = FeatureEngine()
        self.ensemble = EnsembleModel(self.config)

        # Signal audit log — records every ensemble signal and whether it
        # was accepted, rejected (with reason), or shadowed. Powers the
        # daily gap-detector and post-hoc gate analysis.
        self.signal_audit = SignalAudit(
            log_dir=self.config.get("logging", {}).get("log_dir", "logs")
        )

        # Self-learning trade analyzer
        self.trade_analyzer = TradeAnalyzer(self.config, self.database)
        if self.trade_analyzer.enabled and self.trade_analyzer.has_enough_data():
            learned = self.trade_analyzer.get_learned_weights()
            if learned:
                self.ensemble.update_weights(learned)
            # Seed regime-specific weights the ensemble can switch in at decision time
            for regime_key in (
                "bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol",
                "sideways", "unknown",
            ):
                rw = self.trade_analyzer.get_regime_weights(regime_key)
                if rw:
                    self.ensemble.update_regime_weights(regime_key, rw)

        # WebSocket + tick aggregation
        self.tick_aggregator = TickAggregator(["1min", "5min", "15min"])
        self.tick_aggregator.on_candle_close = self._on_candle_close
        broker_name = self.config.get("broker", {}).get("name", "angelone")
        self.ws_client = WebSocketClient(broker_name, self.config, smart_api)

        # Strategies
        self.strategies: List[BaseStrategy] = self._load_strategies()

        # 2026-05-14 Event blackout calendar. Skip new entries on stocks
        # within N trading days of a known catalyst (results, dividend
        # ex-date, AGM, board meeting, buyback). Empty file = permissive
        # legacy behaviour, so this is a strict-add (never blocks anything
        # that wasn't blocked before).
        from core.event_calendar import EventCalendar  # local import keeps cycle clean
        ev_cfg = self.config.get("risk", {}).get("event_blackout", {}) or {}
        self.event_calendar = EventCalendar(
            path=ev_cfg.get("calendar_path", "data/event_calendar.csv"),
            blackout_days_before=int(ev_cfg.get("days_before", 1)),
            blackout_days_after=int(ev_cfg.get("days_after", 0)),
            event_types=ev_cfg.get("event_types"),
        )
        self.event_calendar.force_reload()
        self._event_blackout_enabled: bool = bool(ev_cfg.get("enabled", True))

        # 2026-05-14 SELF-SUFFICIENCY TRACKER. Account-owner stated goal:
        # "passive income that covers running costs" (Kite Connect API,
        # cloud VM, CDSL/DP if delivery, Cursor, etc). The tracker
        # persists cumulative realised P&L since deployment to a tiny
        # JSON ledger and exposes a GREEN/YELLOW/RED status that the
        # audit checkpoint surfaces. Disable by setting
        # risk.self_sufficiency.enabled: false in config.yaml.
        from core.self_sufficiency import SelfSufficiencyTracker
        self.self_sufficiency = SelfSufficiencyTracker.from_config(self.config)

        # Stock scanner — auto-discovers what to trade
        self.scanner = StockScanner(self.config)
        scanner_cfg = self.config.get("scanner", {})
        self._auto_scan = scanner_cfg.get("enabled", True)
        # Pre-market warm-up: scan N min before the bell so the watchlist
        # is hot at 09:15 sharp (saves the ~3 min lost to a post-open scan).
        self._premarket_warmup_minutes: int = int(
            scanner_cfg.get("premarket_warmup_minutes", 5)
        )
        self._did_premarket_scan_today: bool = False
        # Cache the configured market open time once — used by both
        # is_market_open and the pre-market warm-up window.
        self._market_open_str: str = (
            self.config.get("market", {})
            .get("trading_hours", {})
            .get("start", "09:15")
        )

        # Instruments: either auto-scanned or from config fallback
        if self._auto_scan:
            self.instruments: List[dict] = []  # populated by first scan
        else:
            self.instruments: List[dict] = self.config.get("market", {}).get("instruments", [])

        # Market context (updated periodically via live data)
        # P1 #11 (2026-05-17): nifty_trend default = 0 (neutral / unknown)
        # rather than 1 (bull). If the very first market-context refresh
        # fails or returns short history, the OLD default silently licensed
        # bullish regime-sizing & short-selling gates. Neutral keeps gates
        # cautious without halting trading.
        self._market_context: Dict = {
            "india_vix": 15.0, "nifty_trend": 0, "sector_momentum": 0.0,
            # 2026-05-14 Intraday regime overlay -- updated each market-context
            # refresh. ``classify_intraday_regime`` reads these to detect
            # mid-session risk-on/risk-off shifts that the daily 200-EMA + VIX
            # classification doesn't see. None on cold start (overlay = unknown
            # = permissive); first refresh populates them.
            "nifty_intraday_pct": None,
            "vix_intraday_delta": None,
            "vix_open": None,
        }
        self._market_ctx_last_refresh: Optional[datetime] = None
        self._market_ctx_refresh_interval = timedelta(minutes=10)

        # Robustness: anti-churn, late-day cutoff, heartbeat
        robust_cfg = self.config.get("robustness", {})
        self._reentry_cooldown = timedelta(minutes=robust_cfg.get("reentry_cooldown_minutes", 30))
        self._max_losses_per_stock = robust_cfg.get("max_losses_per_stock_per_day", 2)
        self._late_entry_cutoff = robust_cfg.get("late_entry_cutoff", "14:30")
        self._heartbeat_interval = robust_cfg.get("heartbeat_interval_cycles", 10)
        # 2026-05-14: wall-clock heartbeat. Cycle-count alone gave a 30-40 min
        # cadence once a single cycle ballooned to 4 min (200-stock LTP scan),
        # which made cloud liveness checks brittle. We now fire whenever EITHER
        # gate trips — cycle-count (legacy) OR `heartbeat_interval_seconds`
        # (new, default 300s). Set the seconds knob to 0 to disable wall-clock
        # gating entirely.
        self._heartbeat_interval_seconds: float = float(
            robust_cfg.get("heartbeat_interval_seconds", 300)
        )
        self._last_heartbeat_ts: float = 0.0
        self._eod_summary_time = robust_cfg.get("eod_summary_time", "15:20")
        self._max_cycle_errors = robust_cfg.get("max_cycle_errors", 5)

        # Post-audit win-rate enhancements (2026-04-28):
        #   dead_hour_blocks: ["HH:MM-HH:MM", ...] windows with no new entries
        #   min_holding_minutes: soft floor on holding time (SL/TP still honoured)
        #   min_entry_atr_pct: skip stocks too quiet to reach TP within session
        self._dead_hour_blocks = self._parse_time_ranges(
            robust_cfg.get("dead_hour_blocks", [])
        )
        self._min_holding_minutes: float = float(
            robust_cfg.get("min_holding_minutes", 0)
        )
        self._min_entry_atr_pct: float = float(
            robust_cfg.get("min_entry_atr_pct", 0.0)
        )
        # Regime-aware overrides: when present, lookup(regime) → threshold.
        # Falls back to the flat value above for regimes not listed.
        raw_map = robust_cfg.get("min_entry_atr_pct_by_regime") or {}
        self._min_entry_atr_pct_by_regime: Dict[str, float] = {
            str(k): float(v) for k, v in raw_map.items()
        }

        # Short-selling controls. Feature-gated: default OFF. When ON, SELL
        # signals on symbols with no open position open a SHORT (intraday
        # MIS only — squared off at intraday_exit_time).
        exec_cfg = self.config.get("execution", {}) or {}
        self._enable_short_selling: bool = bool(exec_cfg.get("enable_short_selling", False))
        # P2 logic-edges (2026-05-17): the OLD ``or [...]`` fallback masked
        # an explicit empty-list config: ``short_selling_regimes: []`` in
        # config.yaml meant "no short selling allowed" but the truthy
        # short-circuit on empty-list turned it into the permissive
        # default. ``is None`` is the correct guard for "key missing"
        # vs "key set to empty".
        _raw_regimes = exec_cfg.get("short_selling_regimes")
        if _raw_regimes is None:
            self._short_selling_regimes: set = {
                "bear_high_vol", "bear_low_vol", "sideways",
            }
        else:
            self._short_selling_regimes = set(_raw_regimes)
            if not self._short_selling_regimes and self._enable_short_selling:
                logger.warning(
                    "[CONFIG] execution.short_selling_regimes is empty BUT "
                    "execution.enable_short_selling=True. No regime will "
                    "ever allow a short. If this was intentional, also set "
                    "enable_short_selling=False to be explicit."
                )

        # Long-entry regime guard (2026-05-05 backtest finding). Mirror of
        # the short-selling regime guard, but for BUY entries. Backtest
        # showed the long side is responsible for most of the loss in a
        # universe-fixed run because it has no validated edge — yet live
        # the regime guard quietly routes everything to shorts in the
        # current bear tape, so longs never fire. When the regime flips,
        # longs will activate for the first time on real money. This guard
        # gives an explicit safety net: BUY entries only fire when regime
        # is in the listed set. Empty list = permissive (legacy default —
        # no restriction, backwards-compatible). Set explicitly to
        # [bull_low_vol, bull_high_vol] to restrict longs to up-trending
        # regimes.
        long_regimes = exec_cfg.get("long_entry_regimes")
        self._long_entry_regimes: set = (
            set(long_regimes) if long_regimes else set()
        )
        # 2026-05-14 Intraday-regime block on new longs. When true (default),
        # `intraday_regime == "risk_off"` (Nifty -0.5% over last 60 min OR
        # VIX spike +1.5) blocks new BUY entries even if the daily regime
        # is bullish. Designed to catch mid-session flash-crash / reversal
        # situations the daily 200-EMA + VIX classification can't see.
        self._intraday_regime_block_longs: bool = bool(
            exec_cfg.get("intraday_regime_block_longs", True)
        )

        self._cooldown_map: Dict[str, datetime] = {}      # symbol → last losing exit time
        # P2 logic-edges (2026-05-17): parallel side tracker. Records the
        # SIDE that experienced the cooldown-triggering exit so the
        # opposite-side edge isn't blocked.  In-memory only (the disk
        # snapshot keeps the legacy bare-symbol format; on restart the
        # side info is lost and we conservatively fall back to bare-symbol
        # blocking until the next exit refreshes the side).
        self._cooldown_side_map: Dict[str, str] = {}
        self._stock_loss_today: Dict[str, int] = {}        # symbol → loss count today
        self._consec_tp_today: Dict[str, int] = {}         # symbol → consecutive TPs today (trend continuation)

        # Per-symbol streak of consecutive data-quality rejections. Routine
        # failures are logged at DEBUG (to cut noise from ~50k WARNINGs/day
        # seen on 2026-04-29). When a symbol crosses _DQ_WARN_AFTER
        # consecutive failures we emit a single WARNING as an escalation so
        # operators still see genuine feed outages.
        self._dq_failure_streak: Dict[str, int] = {}
        self._dq_warned_symbols: set = set()
        self._prev_close_cache: Dict[str, float] = {}      # symbol → yesterday's close (for circuit check)

        # Concentration / safety limits from risk config
        risk_cfg_raw = self.config.get("risk", {})
        self._max_sector_exposure_pct: float = risk_cfg_raw.get("max_sector_exposure_pct", 40.0)
        self._max_symbol_exposure_pct: float = risk_cfg_raw.get("max_symbol_exposure_pct", 30.0)
        self._circuit_proximity_pct: float = risk_cfg_raw.get("circuit_proximity_pct", 8.0)
        # Per-symbol bucketing for unclassified names: prevents one open
        # "UNKNOWN" position from blocking every other unmapped mid-cap.
        # Default True (safer behaviour) — audit 2026-04-30.
        self._unknown_sector_per_symbol: bool = bool(
            risk_cfg_raw.get("unknown_sector_per_symbol", True)
        )
        # 2026-05-14 Supersector roll-up: collapse Banks/NBFC/Insurance/AMC
        # /FinTech into a single "Financials" bucket for the concentration
        # cap. Live evidence (2026-05-14): the SHORT book held 4 of 6
        # positions in financial-sector tickers (CENTRALBK + FEDERALBNK +
        # TATACAP + CHOLAFIN) -- effectively 67% of book in one rate-cycle
        # factor that the per-sector cap let through because each sub-bucket
        # was independently below 40%.
        self._use_supersectors: bool = bool(
            risk_cfg_raw.get("use_supersectors", True)
        )
        # Window cap (2026-05-07): cap how many positions can be opened
        # within a short rolling window so a burst of correlated signals
        # (e.g. opening-bell pile-on or a single sector wave) can't blow
        # past concentration limits before they update. 0 = disabled.
        self._max_opens_per_window: int = int(
            risk_cfg_raw.get("max_opens_per_window", 0)
        )
        self._opens_window_minutes: int = int(
            risk_cfg_raw.get("opens_window_minutes", 5)
        )
        # Rolling deque of (datetime, symbol) for opens within the window.
        # Pruned in `_pre_trade_safety_checks` so we never carry stale entries.
        from collections import deque as _deque
        self._recent_opens: _deque = _deque()
        # 2026-05-14 STRATEGY-CONCURRENCY CAP --------------------------------
        # Live evidence (2026-05-14): 8 SHORT signals fired in 37 min, ALL
        # `supertrend_follow`, ALL in `bear_high_vol`. 4 of them stopped
        # out for -Rs 730 in 40 minutes. The per-strategy circuit-breaker
        # only fires AFTER N losses -- by which time the entries are
        # already in. This cap blocks NEW entries once a single strategy
        # already holds N positions, preventing single-strategy pile-on.
        # 0 = disabled (legacy). Recommended: ceil(max_open_positions/2).
        self._max_positions_per_strategy: int = int(
            risk_cfg_raw.get("max_positions_per_strategy", 0)
        )
        # Per-trade notional floor — commissions eat sub-Rs 6k wins alive.
        self._min_trade_notional: float = float(
            risk_cfg_raw.get("min_trade_notional", 0.0)
        )
        # Minimum SL distance (% of entry) — ATR-only stops on quiet stocks
        # come out < 1 %, which is inside normal intraday noise.
        self._min_stop_loss_pct: float = float(
            risk_cfg_raw.get("min_stop_loss_pct", 0.0)
        )
        # TP ceilings — bound how far the TP can sit from entry so it's
        # reachable intraday. Cap both as multiple of SL distance and as
        # absolute % of entry price. 0 = disabled.
        self._max_tp_to_sl_multiple: float = float(
            risk_cfg_raw.get("max_tp_to_sl_multiple", 0.0)
        )
        self._max_tp_pct: float = float(
            risk_cfg_raw.get("max_tp_pct", 0.0)
        )
        # Per-strategy RR floor for expected-profit gate. Overrides the
        # default 1.2x when the leading strategy is present in this map.
        raw_rr = risk_cfg_raw.get("min_rr_by_strategy") or {}
        self._min_rr_by_strategy: Dict[str, float] = {
            str(k): float(v) for k, v in raw_rr.items()
        }

        # Exit fast-path floor — closing signals on existing positions
        # bypass ensemble consensus (see _trading_cycle). 0 disables the
        # fast path. mean_reversion exits emit at conf=0.45 by design,
        # so the default 0.40 is the natural floor.
        self._signal_exit_min_conf: float = float(
            risk_cfg_raw.get("signal_exit_min_conf", 0.40)
        )

        # Signal-exit unrealized-PnL floor (2026-05-05 backtest finding).
        # The exit fast-path was correctly closing single-strategy "thesis
        # fulfilled" signals — but on near-flat positions the round-trip
        # charges (~Rs 6 on MIS) turned nominal "wins" into net losses.
        # Live evidence: LODHA SELL closed -Rs 2.82 (signal), ITCHOTELS SELL
        # closed +Rs 1.33 (signal). Both were "wins" by exit_reason but
        # net negative once charges cleared. This floor rejects fast-path
        # exits whose unrealized PnL hasn't covered ~1.5x round-trip
        # charges — the trade keeps running until SL/TP/trailing decides.
        # SL and TP exits remain unconditional (this only gates signal
        # exits). Set to 0.0 to disable.
        self._min_holding_pnl_rs: float = float(
            risk_cfg_raw.get("min_holding_pnl_rs", 0.0)
        )
        # 2026-05-14: scale the unrealized-PnL floor by position notional. A
        # flat Rs 15 floor was right-sized for the Rs 5k min_trade_notional
        # but lets a Rs 30k position fast-exit at +Rs 16 -- which doesn't
        # cover the ~Rs 31 round-trip charges on that notional, turning the
        # nominal "win" into a real loss. The runtime floor is now
        # max(min_holding_pnl_rs, charges_multiple * actual_round_trip_charges).
        # Set the multiple to 0 to revert to the flat absolute floor.
        self._min_holding_charges_multiple: float = float(
            risk_cfg_raw.get("min_holding_charges_multiple", 1.5)
        )

        # Rejection cooldown (2026-05-04 part 4). When a (symbol, direction)
        # signal is rejected by a persistent gate (notional floor, safety
        # gate, sector concentration, ATR gate, etc.) it almost always
        # re-fires from the next strategy cycle 60 s later — same gate
        # rejects it again, audit-log row written, repeat. On 2026-05-04
        # BANDHANBNK SELL was rejected 3x, MEESHO SELL 3x, TATACHEM SELL 3x.
        # This cooldown short-circuits re-evaluation for N seconds.
        # 0 disables. Reasons that hinge on portfolio state (already_open,
        # blacklist, cooldown) are excluded from the cooldown map — those
        # change on their own when the underlying state changes.
        self._rejection_cooldown_seconds: int = (
            int(risk_cfg_raw.get("rejection_cooldown_minutes", 5)) * 60
        )
        # Map: (symbol, direction) -> datetime of last persistent rejection.
        # Reset daily in _reset_daily_trackers.
        self._rejection_cooldown_map: Dict[Tuple[str, str], datetime] = {}
        # Reasons that should NOT trigger the cooldown (state-dependent;
        # they'll naturally clear when state changes):
        self._rejection_cooldown_skip_reasons: tuple = (
            "already_open",
            "blacklist",
            "cooldown",  # exit cooldown is its own mechanism
            "shorts_disabled",  # config flag — only changes on config edit
        )

        # 2026-05-15 Cooldown persistence. The three maps above live only in
        # memory. A mid-session container restart wipes them -- live evidence
        # at 14:46 IST today, where the 3 stop-out cooldowns vanished and
        # only `late_entry_cutoff: 14:30` (already past) prevented a
        # FEDERALBNK re-entry on a 2/2-confirmed SELL at 14:58.
        # The persistence layer is fail-soft: load returns empty dicts on
        # any error, save logs WARNING and swallows. Trading must never
        # block on this. We load AFTER constructing the empty dicts so the
        # in-memory state is the source of truth either way.
        try:
            (
                restored_cd,
                restored_loss,
                restored_rej,
                restored_side,
            ) = load_cooldown_state(
                reentry_cooldown=self._reentry_cooldown,
                rejection_cooldown=timedelta(seconds=self._rejection_cooldown_seconds),
            )
            self._cooldown_map.update(restored_cd)
            self._stock_loss_today.update(restored_loss)
            self._rejection_cooldown_map.update(restored_rej)
            # 2026-05-18 regression fix: restore the side tracker too.
            # See cooldown_persistence.save_cooldown_state for the
            # full motivation.
            self._cooldown_side_map.update(restored_side)
        except Exception as exc:  # noqa: BLE001 - persistence must not block startup
            logger.warning(f"[COOLDOWN-PERSIST] load failed at init: {exc!r}")

        # P2 restart-cluster (2026-05-17) -- LIVE-MODE SAFETY: rehydrate the
        # three intraday runtime state buckets so a mid-day restart doesn't
        # silently unsuspend a strategy that hit its circuit-breaker pre-
        # restart, or forget the last N opens used by the global open-rate
        # cap. See runtime_state_persistence.py for the full motivation.
        try:
            restored_strat, restored_opens, restored_tp = load_runtime_state(
                open_rate_window=timedelta(minutes=self._opens_window_minutes),
            )
            for s, v in restored_strat.items():
                self._strategy_state[s] = v
            for ts, sym in restored_opens:
                self._recent_opens.append((ts, sym))
            for sym, c in restored_tp.items():
                self._consec_tp_today[sym] = c
        except Exception as exc:  # noqa: BLE001 - persistence must not block startup
            logger.warning(f"[RUNTIME-PERSIST] load failed at init: {exc!r}")

        # P1 #15 (2026-05-17) -- LIVE-MODE SAFETY: rehydrate TrailingStop
        # state for every restored open position. Without this, a mid-session
        # restart resets `highest_since_entry`, `breakeven_armed`,
        # `peak_giveback_armed`, etc. to defaults -- the position is back at
        # its initial-SL distance even if pre-restart the trail had locked
        # in profit or breakeven had armed. The snapshot is stashed here
        # and consumed by ``risk_manager.create_trailing_stop`` (which we
        # monkey-wrap below) when each restored position is repopulated
        # into the trailing-stop registry. We do NOT call create_trailing_stop
        # from here -- the rest of the restart sequence (e.g. portfolio
        # _restore_positions feeding into trading_agent's own rehydration)
        # owns that. We just make the snapshot available for lookup.
        try:
            self._trailing_state_snapshots: Dict[str, dict] = load_trailing_states()
        except Exception as exc:  # noqa: BLE001 - persistence must not block startup
            logger.warning(f"[TRAIL-PERSIST] load failed at init: {exc!r}")
            self._trailing_state_snapshots = {}

        # Wrap risk_manager.create_trailing_stop so restored snapshots are
        # automatically applied. This is done as a wrap (rather than
        # editing risk_manager) so the persistence concern stays at the
        # daemon layer; RiskManager keeps its narrow responsibility.
        if self._trailing_state_snapshots:
            original_create = self.risk_manager.create_trailing_stop

            def _create_trailing_stop_with_restore(
                symbol, entry_price, initial_sl, side="BUY",
                *,
                _orig=original_create,
                _snaps=self._trailing_state_snapshots,
            ):
                ts = _orig(symbol, entry_price, initial_sl, side)
                snap = _snaps.pop(symbol, None)
                if snap is not None:
                    restore_trailing_state(ts, snap)
                return ts

            self.risk_manager.create_trailing_stop = _create_trailing_stop_with_restore

        # Intraday strategy-contribution tally (reset at new-day boundary).
        # Used for EOD diversity monitor (Fix 6). Key = strategy name,
        # value = summed contribution weight across all ensemble signals
        # emitted today (actioned or rejected).
        self._strategy_contrib_today: Dict[str, float] = {}

        # Per-strategy circuit breaker (2026-05-07). Suspends a single
        # strategy for the rest of the day after `strategy_max_consec_losses`
        # consecutive losses OR daily PnL <= -strategy_daily_loss_pct% of
        # initial capital. Other strategies remain free to trade.
        # Set strategy_max_consec_losses=0 OR strategy_daily_loss_pct=0 to disable.
        self._strategy_max_consec_losses: int = int(
            risk_cfg_raw.get("strategy_max_consec_losses", 3)
        )
        self._strategy_daily_loss_pct: float = float(
            risk_cfg_raw.get("strategy_daily_loss_pct", 1.0)
        )
        # state shape:
        #   strategy_name -> {consec_losses, daily_pnl, suspended, suspended_reason, trades}
        self._strategy_state: Dict[str, Dict] = {}
        self._daily_tracker_date: Optional[datetime] = None
        # 2026-05-13: persist this across daemon restarts via a flag file
        # under ``logs/.eod_sent_<YYYY-MM-DD>.flag``. In-memory only meant
        # that a healthcheck-triggered restart between 15:20 (EOD time)
        # and 15:30 (close) would replay EOD on every boot -- the
        # incident where 11 identical EOD Summary emails went out in
        # 30 minutes. ``_load_eod_sent_flag`` reads the file on init so
        # the post-restart agent already knows EOD is done.
        self._eod_summary_sent = self._load_eod_sent_flag()
        if self._eod_summary_sent:
            logger.info(
                "EOD summary flag found on disk -- already sent for today; "
                "skipping duplicate EOD work this session."
            )
        self._consecutive_cycle_errors = 0

        # Market open / close times — agent gates pre-market and post-market
        # behaviour off these. Comes from market config.
        trading_hours = self.config.get("market", {}).get("trading_hours", {})
        try:
            mo_str = trading_hours.get("start", "09:15")
            h, m = map(int, mo_str.split(":"))
            self._market_open_time = dtime(h, m)
        except Exception:
            self._market_open_time = dtime(9, 15)
        try:
            mc_str = trading_hours.get("end", "15:30")
            h, m = map(int, mc_str.split(":"))
            self._market_close_time = dtime(h, m)
        except Exception:
            self._market_close_time = dtime(15, 30)

        self._running = False
        self._cycle_count = 0
        dp_cfg = self.config.get("data_pipeline", {})
        self._use_websocket = dp_cfg.get("use_websocket", False)
        # 2026-05-14: when True, the WS subscribes ONLY to currently-held
        # symbols (resubscribed every position open/close). Lets us run
        # tick-grade SL/TP enforcement without paying the full 200-stock
        # firehose cost. When False (or use_websocket=False), legacy
        # behaviour: subscribes to the full watchlist on start.
        self._ws_held_only: bool = bool(dp_cfg.get("ws_held_only", True))
        self._ws_subscribed_held: set = set()

        # Hourly audit checkpoint tracker. We fire on the first cycle whose
        # IST hour differs from the last checkpoint's hour, during 09:00-16:00.
        # This avoids both clock-drift (don't try to fire exactly on :00) and
        # unwanted out-of-hours runs.
        self._last_audit_hour: Optional[int] = None

        # ── Opening-bar lockout (2026-05-07) ────────────────────────
        # 30-day post-mortem showed many losing trades opened in the
        # first 5-10 minutes after market open (no proper price
        # discovery yet, wide spreads, gap-up/down-driven false
        # signals). This window blocks NEW position opens; existing
        # positions can still EXIT (SL/TP/signal).  Set to 0 to disable.
        risk_cfg = self.config.get("risk", {})
        self._opening_lockout_minutes: int = int(
            risk_cfg.get("opening_lockout_minutes", 15)
        )

        # ── Carryover profit-locking (2026-05-07) ───────────────────
        # CROMPTON had +Rs 108 unrealized at 15:25 yesterday; we held
        # it overnight and lost Rs 166 today. Lesson: carryover
        # positions in profit should be auto-closed at session end.
        # Fires once per day at `_carryover_lock_time` IST.
        try:
            ct_str = risk_cfg.get("carryover_lock_time", "15:10")
            h, m = map(int, ct_str.split(":"))
            self._carryover_lock_time: dtime = dtime(h, m)
        except Exception:
            self._carryover_lock_time = dtime(15, 10)
        self._carryover_lock_min_profit: float = float(
            risk_cfg.get("carryover_lock_min_profit", 0.0)
        )
        self._carryover_lock_done: bool = False
        self._carryover_lock_done_date: Optional[date] = None

        # Carryover SL recompute (2026-05-07). At the first market-open
        # cycle of the day, tighten the SL on any position held from a
        # prior session to MAX(current_sl, break-even). This is the lesson
        # from the CROMPTON case: a profitable carryover position whose
        # stale yesterday-ATR-based SL let an overnight gap-up turn a
        # +Rs 108 trade into a -Rs 166 trade. Set
        # `carryover_sl_to_breakeven: false` to disable.
        self._carryover_sl_to_breakeven: bool = bool(
            risk_cfg.get("carryover_sl_to_breakeven", True)
        )
        self._carryover_sl_recomputed_date: Optional[date] = None

        # ── Emergency stop (file-based kill switch, 2026-05-06) ──────
        # Operator can halt the agent without hunting PIDs by creating
        # a STOP file in the configured location (default `logs/STOP`).
        # The agent picks it up at the top of every cycle, sends an
        # alert, and exits cleanly via the existing `finally` block
        # (which runs `_shutdown()` and the EOD summary). Existing
        # positions are NOT auto-flattened — closing them is a manual
        # decision since intraday MIS positions auto-square at 15:15
        # anyway.
        ops_cfg = self.config.get("operations", {})
        log_dir = self.config.get("logging", {}).get("log_dir", "logs")
        self._emergency_stop_path: str = ops_cfg.get(
            "emergency_stop_path", os.path.join(log_dir, "STOP")
        )
        # Whether the kill switch should also try to flatten open
        # positions before exiting. Default off — most operators want
        # to inspect state, not panic-close.
        self._emergency_stop_flatten: bool = bool(
            ops_cfg.get("emergency_stop_flatten", False)
        )

        logger.info(
            f"TradingAgent v2 initialized | Mode: {self.execution.mode} | "
            f"Capital: \u20B9{effective_balance:,.2f} "
            f"(config: \u20B9{config_initial_balance:,.2f}, peak: \u20B9{historical_peak or effective_balance:,.2f}) | "
            f"Strategies: {[s.name for s in self.strategies]} | "
            f"Auto-scan: {'ON' if self._auto_scan else 'OFF'} | "
            f"Ensemble threshold: {self.ensemble.confidence_threshold}"
        )

    @staticmethod
    def _load_config(path: str) -> dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        load_dotenv()
        with open(path, "r") as f:
            cfg = yaml.safe_load(f)
        # Check raw yaml BEFORE env merge — catches real secrets committed to
        # the repo. After the merge, env values overlay config and the warning
        # would incorrectly fire for values that are only in the .env file.
        warn_if_secrets_in_yaml(cfg, yaml_path=path)
        cfg = apply_env_to_config(cfg)
        return cfg

    def _resolve_starting_balance(
        self, config_balance: float, reset: bool, smart_api
    ) -> tuple:
        """
        Decide the effective starting balance for today's session.

        Priority:
          1. Live/Kite mode → broker-reported cash funds (ground truth).
          2. Paper mode + DB history + not reset → last equity snapshot.
          3. Otherwise → config `initial_balance` (first-ever run or explicit reset).

        Also returns the historical peak equity so drawdown is tracked correctly
        across restarts.

        Returns:
            (effective_balance, historical_peak) — peak may be None on first run.
        """
        mode = self.config.get("execution", {}).get("mode", "paper").lower()

        # Live mode: query broker funds directly
        if mode == "live" and smart_api is not None:
            try:
                funds = smart_api.funds() if hasattr(smart_api, "funds") else None
                if funds and isinstance(funds, dict):
                    equity = funds.get("equity", {}) or {}
                    available = equity.get("available", {}) or {}
                    cash = available.get("live_balance") or available.get("cash")
                    if cash is not None:
                        cash = float(cash)
                        logger.info(
                            f"Live mode: broker-reported available cash = Rs {cash:,.2f}"
                        )
                        peak = self.database.get_peak_equity()
                        return cash, peak
            except Exception as e:
                logger.warning(
                    f"Could not fetch broker funds ({e}); falling back to config balance."
                )

        # Explicit reset requested
        if reset:
            logger.warning(
                f"--reset-balance specified: starting fresh with config balance "
                f"Rs {config_balance:,.2f} (DB history preserved but ignored)."
            )
            return config_balance, None

        # Paper mode: continue from DB snapshot if available
        try:
            snap = self.database.get_last_equity_point()
            peak = self.database.get_peak_equity()
        except Exception as e:
            logger.warning(f"Could not read equity history from DB: {e}")
            return config_balance, None

        if not snap or snap.get("equity") is None:
            logger.info(
                f"No equity history in DB — seeding with config balance "
                f"Rs {config_balance:,.2f}."
            )
            return config_balance, None

        last_equity = float(snap["equity"])
        last_ts = snap.get("timestamp", "")
        last_positions = int(snap.get("positions", 0))

        # Staleness check: if the last snapshot is older than 14 days, something
        # is off (agent hasn't run in 2 weeks). Warn the user but still continue.
        try:
            last_dt = datetime.fromisoformat(str(last_ts).replace("Z", ""))
            age_days = (datetime.now() - last_dt).days
            if age_days > 14:
                logger.warning(
                    f"Last equity snapshot is {age_days} days old ({last_ts}). "
                    f"Market conditions may have changed materially since then."
                )
        except Exception:
            pass

        # If the snapshot has open positions, _restore_positions inside Portfolio
        # will re-derive cash from cost-basis; we pass `last_equity` here so
        # risk/position-sizing uses a realistic total.
        if last_positions > 0:
            logger.info(
                f"Continuing from DB: last equity Rs {last_equity:,.2f} "
                f"with {last_positions} open position(s) at {last_ts}."
            )
        else:
            logger.info(
                f"Continuing from DB: last equity Rs {last_equity:,.2f} "
                f"(flat, snapshot {last_ts})."
            )
        return last_equity, peak

    def _setup_logging(self):
        log_cfg = self.config.get("logging", {})
        log_level = log_cfg.get("level", "INFO")
        log_dir = log_cfg.get("log_dir", "logs")
        os.makedirs(log_dir, exist_ok=True)

        logger.remove()
        if log_cfg.get("console", True):
            logger.add(sys.stderr, level=log_level,
                       format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
        if log_cfg.get("file", True):
            logger.add(
                os.path.join(log_dir, "trading_agent_{time:YYYY-MM-DD}.log"),
                level=log_level,
                rotation=f"{log_cfg.get('max_file_size_mb', 10)} MB",
                retention=log_cfg.get("backup_count", 5),
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            )

    def _load_strategies(self) -> List[BaseStrategy]:
        strat_cfg = self.config.get("strategies", {})
        active = strat_cfg.get("active", [])
        strategies = []
        for name in active:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                logger.warning(f"Strategy '{name}' not in registry, skipping")
                continue
            params = strat_cfg.get(name, {})
            strategies.append(cls(params))
            logger.debug(f"Loaded strategy: {name}")
        return strategies

    # ── Main Loop ────────────────────────────────────────────

    def run(self, poll_interval: int = 60):
        """Start the trading loop (polling or WebSocket mode).

        Pre-flight: a quick health check runs before the first cycle.
        If anything *critical* fails (DB unwritable, no strategies
        loaded, etc.) we abort early instead of silently running a
        broken agent. Non-critical issues (stale model, no email
        configured) are logged as warnings but don't block.
        """
        if not self._preflight_checks():
            logger.critical(
                "[PREFLIGHT] Critical checks failed — refusing to start. "
                "See log above for details."
            )
            return

        self._running = True

        # Drain any alerts that were spooled to disk during a previous network
        # outage (e.g. VPN flake at EOD). Best-effort: a failure here must NEVER
        # prevent the trading loop from starting.
        try:
            if hasattr(self.alert_manager, "drain_failed_alerts"):
                drain_result = self.alert_manager.drain_failed_alerts()
                if drain_result.get("sent") or drain_result.get("failed"):
                    logger.info(
                        f"[BOOT] alert spool drained: "
                        f"sent={drain_result['sent']} failed={drain_result['failed']}"
                    )
        except Exception as e:
            logger.warning(f"[BOOT] alert spool drain failed (non-fatal): {e}")

        # Auto-scan stocks before first cycle
        if self._auto_scan:
            self._run_scan()

        if self._use_websocket:
            self._start_websocket()

        logger.info(f"Agent started (poll={poll_interval}s, instruments={len(self.instruments)})")

        try:
            while self._running:
                try:
                    # File-based emergency stop. Top of cycle so it always
                    # runs before any new order placement. Doing it inside
                    # the inner try/except means a transient FS error
                    # falls into the normal cycle-error counter and won't
                    # silently swallow the stop request.
                    if self._check_emergency_stop():
                        break

                    # P2 restart-cluster (2026-05-17): refresh AngelOne JWT
                    # before 8h expiry. Cheap (datetime compare) when not
                    # due; full re-login when threshold crossed.
                    self._maybe_refresh_broker_session()

                    # Reset daily trackers at the start of each new day
                    self._reset_daily_trackers()

                    # Pre-market warm-up scan (2026-05-04): the heavy scanner
                    # takes ~3 min over 500 NSE stocks. If we wait until the
                    # bell to start, we miss the most volatile minutes of the
                    # session. Kick off the scan inside the warmup window
                    # (default 5 min before open) so the watchlist is hot at
                    # 09:15 sharp.
                    if (self._auto_scan
                            and not self._did_premarket_scan_today
                            and self._is_premarket_warmup_window()):
                        logger.info(
                            "[PRE-MARKET] Warm-up scan starting "
                            f"({self._premarket_warmup_minutes} min before open)"
                        )
                        self._run_scan()
                        self._did_premarket_scan_today = True

                    # Periodic rescan to rotate into better stocks. Skip if the
                    # market is closed — scans cost ~3 minutes and we'd just
                    # throw the result away when we shut down this cycle.
                    if (self._auto_scan
                            and self.scanner.needs_rescan()
                            and self.data_handler.is_market_open()):
                        self._run_scan()

                    self._trading_cycle()
                    self._consecutive_cycle_errors = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self._consecutive_cycle_errors += 1
                    logger.exception(f"Cycle error ({self._consecutive_cycle_errors}/{self._max_cycle_errors}): {e}")
                    self.alert_manager.send_alert("Cycle Error", str(e), level="error")
                    if self._consecutive_cycle_errors >= self._max_cycle_errors:
                        logger.critical(f"HALTING: {self._consecutive_cycle_errors} consecutive cycle errors")
                        self.alert_manager.send_alert(
                            "AGENT HALTED", f"Too many errors: {e}", level="critical")
                        break

                self._cycle_count += 1

                # Heartbeat -- fire whenever EITHER cycle-count OR wall-clock
                # gate trips. Wall-clock keeps cloud liveness fresh even when
                # a single cycle runs long (200-stock LTP scan can be 3-4 min,
                # which previously made cycle-count cadence ~40 min).
                cycle_due = (self._cycle_count % self._heartbeat_interval == 0)
                clock_due = (
                    self._heartbeat_interval_seconds > 0
                    and (time.monotonic() - self._last_heartbeat_ts)
                        >= self._heartbeat_interval_seconds
                )
                if cycle_due or clock_due:
                    self._log_heartbeat()
                    self._last_heartbeat_ts = time.monotonic()

                # Auto EOD summary
                self._maybe_send_eod_summary()

                # Periodic equity snapshot
                if self._cycle_count % 5 == 0:
                    self._snapshot_equity()

                # Hourly audit checkpoint (2026-05-06).
                # Writes a comprehensive snapshot to logs/audit/<date>/checkpoint_HHMM.md
                # so an external operator/agent can read the latest state on demand
                # without having to re-derive everything from raw logs.
                # Non-blocking: any failure is captured inside the checkpoint
                # itself rather than crashing the trading loop.
                self._maybe_audit_checkpoint()

                # Periodic DB cleanup (once every 100 cycles)
                if self._cycle_count % 100 == 0 and self._cycle_count > 0:
                    self._periodic_cleanup()

                if self._running:
                    # Fast inner poll: between heavy scan-cycles, watch open
                    # positions at finer granularity so SL/TP/trail/peak-
                    # giveback fire on intra-bar MFE that would otherwise
                    # be invisible to the 5-min scan boundary. Only fetches
                    # LTPs for currently-held symbols (cheap), and only
                    # runs while we actually hold positions.
                    self._fast_exits_sleep(poll_interval)

        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            self._shutdown()

    def _fast_exits_sleep(self, total_seconds: int) -> None:
        """Sleep for `total_seconds` while polling open-position exits at
        a finer cadence than the full scan-cycle.

        Behaviour:
          - If no open positions, just sleep (old behaviour, no extra cost).
          - Otherwise, slice the sleep into ~15s chunks and after each chunk
            run `_check_position_exits` on JUST the held symbols. This means
            SL/TP/trail/peak-giveback see prices roughly every 15s instead
            of every 5+ minutes (median measured gap on 2026-05-08).
          - Any exception in the fast path is logged but never blocks the
            outer loop — a transient LTP failure must not crash the agent.
        """
        slice_seconds = 15
        if total_seconds <= slice_seconds or not self.portfolio.positions:
            time.sleep(total_seconds)
            return

        slept = 0
        while slept < total_seconds and self._running:
            chunk = min(slice_seconds, total_seconds - slept)
            time.sleep(chunk)
            slept += chunk
            if not self.portfolio.positions:
                continue
            try:
                held = [s for s in self.portfolio.positions.keys()]
                if not held:
                    continue
                held_instruments = [
                    {"symbol": s, "token": self._get_token(s)} for s in held
                ]
                fast_prices = self.data_handler.get_multiple_ltp(held_instruments)
                self._check_position_exits(fast_prices)
            except Exception as e:
                logger.debug(f"[FAST-EXITS] poll failed (non-fatal): {e}")

    def _run_scan(self):
        """Run the stock scanner and update the instrument watchlist."""
        logger.info("Running stock scanner...")
        try:
            scanned = self.scanner.scan()
            if scanned:
                # Don't remove stocks we currently hold — keep them in the list until closed
                held_symbols = set(self.portfolio.positions.keys())
                scanned_symbols = {s["symbol"] for s in scanned}

                # Merge: scanned stocks + any stocks we currently hold.
                # Lookup table for existing instrument dicts so we can
                # preserve broker tokens / exchange info when re-merging.
                existing_by_symbol = {
                    inst["symbol"]: inst for inst in self.instruments
                }

                merged = list(scanned)
                # 2026-05-04 fix: iterate held_symbols directly instead of
                # self.instruments. On a fresh boot from DB-restored positions
                # self.instruments was [] before the first scan, so positions
                # like RAILTEL/NIVABUPA silently dropped off the watchlist —
                # which broke strategy evaluation, EXIT signals, and the
                # exit-fast-path for those positions.
                for symbol in sorted(held_symbols):
                    if symbol in scanned_symbols:
                        continue  # already in scan results
                    inst = existing_by_symbol.get(symbol)
                    if inst is None:
                        # No prior watchlist entry — construct a minimal
                        # instrument dict from the held position. Token is
                        # broker-specific and only needed for AngelOne/Kite
                        # LTP paths; Yahoo derives ticker from symbol so an
                        # empty token is fine in paper / Yahoo modes.
                        inst = {"symbol": symbol, "token": ""}
                    merged.append(inst)
                    logger.info(f"Keeping {symbol} in watchlist (open position)")

                self.instruments = merged
                logger.info(f"Watchlist updated: {[i['symbol'] for i in self.instruments]}")
                symbol_list = [i["symbol"] for i in self.instruments]
                total = len(symbol_list)
                # Show all names, but format in rows of 10 for readability when the
                # list is large (was capped at 10 prior to 2026-05-04 which made it
                # impossible to verify top_n changes from the email alone).
                rows = [
                    ", ".join(symbol_list[i : i + 10])
                    for i in range(0, total, 10)
                ]
                body = f"Watchlist ({total} stocks):\n" + "\n".join(rows)
                self.alert_manager.send_alert(
                    f"Scanner Update ({total} stocks)",
                    body,
                    level="info",
                )

                # Resubscribe WebSocket if active.
                # 2026-05-18 (P1): use set_subscriptions (wholesale
                # replace) instead of the upsert-only subscribe(). The
                # OLD path left stocks that fell off the scanner result
                # subscribed forever -- the broker kept sending ticks
                # for them, wasting bandwidth and the WS thread's CPU.
                # The held-only path at line ~1440 already uses the
                # right call; this scanner-refresh path was the leak.
                if self._use_websocket:
                    self.ws_client.set_subscriptions(self.instruments)
            else:
                logger.warning("Scanner returned no results, keeping current instruments")
        except Exception as e:
            logger.error(f"Scan failed: {e}")

    def _start_websocket(self):
        """Initialize WebSocket feed for real-time ticks."""
        # 2026-05-18 (P1): set_subscriptions(...) on initial wire-up so a
        # restart (where ``self.instruments`` is the freshly-scanned set)
        # establishes a clean subscription state and doesn't inherit any
        # leftover entries from a stale in-memory state (shouldn't happen
        # at fresh start, but defensive in case the agent is restarted
        # within the same Python process e.g. via test harness).
        self.ws_client.on_tick = self._on_tick
        self.ws_client.set_subscriptions(self.instruments)
        self.ws_client.start()

    def _on_tick(self, tick: dict):
        """Process a raw tick from WebSocket.

        2026-05-14: tick stream now drives the *exit* path too, not just
        trail-SL updates. Previously a tick only nudged the trailing SL
        in memory; the actual SL/TP/peak-giveback check still ran on
        the 15-second polling cadence, so a price spike that briefly
        crossed the trail and reverted in <15s was invisible. Wiring
        ticks into `_check_position_exits` closes that hole when WS is
        on. Wrapped in try/except so a bad tick can't blow up the WS
        background thread (which would silently kill all subsequent
        ticks).
        """
        try:
            symbol = tick["symbol"]
            price = tick["ltp"]
            volume = tick.get("volume", 0)
        except KeyError:
            return

        # P2 tick-path cluster (2026-05-17): pass exchange-side timestamp
        # through (set by websocket_client from message.exchange_timestamp)
        # so candle bucketing uses the real event time, not local wall-clock.
        tick_ts = tick.get("timestamp")
        self.tick_aggregator.process_tick(symbol, price, volume, timestamp=tick_ts)
        try:
            self.database.store_tick(symbol, price, volume,
                                     bid=tick.get("bid", 0), ask=tick.get("ask", 0))
        except Exception:
            pass

        # Only act on ticks for symbols we actually hold -- saves CPU when
        # the watchlist is wider than the position book.
        if symbol not in self.portfolio.positions:
            return

        ts = self.risk_manager.get_trailing_stop(symbol)
        if ts:
            new_sl = self.risk_manager.update_trailing_stop(symbol, price)
            if ts.trailing_active:
                logger.debug(f"Trailing SL for {symbol}: \u20B9{new_sl:.2f}")

        # Drive the exit check off this single-symbol price snapshot.
        # `_check_position_exits` is idempotent and handles the fact that
        # `to_close` may be empty.
        try:
            self._check_position_exits({symbol: price})
        except Exception as e:
            logger.warning(f"[WS-EXIT] check_position_exits({symbol}) failed: {e}")

    def _ws_subscribed_held_set(self) -> set:
        """Helper for the held-only WebSocket subscription strategy."""
        return getattr(self, "_ws_subscribed_held", set())

    def _resubscribe_ws_to_held(self) -> None:
        """When `data_pipeline.ws_held_only` is true, keep the WebSocket
        subscription tight to the symbols we actually hold. Reduces
        message volume by 30-100x vs subscribing to the entire 200-stock
        watchlist, and means we never miss a tick on a symbol we care
        about because the broker rate-limited the firehose.

        Best-effort: if the WS object doesn't expose a re-subscription
        API, this is a no-op."""
        if not self._use_websocket or not self._ws_held_only:
            return
        held = set(self.portfolio.positions.keys())
        prev = self._ws_subscribed_held_set()
        if held == prev:
            return
        # P2 tick-path cluster (2026-05-17): the OLD filter silently dropped
        # any held position whose token resolved to empty. The position then
        # never got tick-driven exits and the operator had no signal. Now we
        # surface those cases via a CRITICAL log + alert; the subscription
        # itself still skips the empty-token entries (the broker rejects
        # them anyway).
        missing_token = sorted(s for s in held if not self._get_token(s))
        if missing_token:
            logger.critical(
                f"[WS-HELD-RESUBSCRIBE] {missing_token}: no token resolved; "
                f"these positions will receive NO WS ticks and rely on the "
                f"15s polling loop for exits. Operator action: refresh the "
                f"instruments universe."
            )
            try:
                self.alert_manager.send_alert(
                    "WS Resubscribe Missing Tokens",
                    f"No WS subscription for held positions: {missing_token}",
                    level="critical",
                )
            except Exception:
                pass
        held_instruments = [
            {"symbol": s, "token": self._get_token(s)} for s in held if self._get_token(s)
        ]
        try:
            # P1 #14 (2026-05-17): use set_subscriptions so closed-position
            # symbols actually get dropped on next reconnect. The OLD
            # subscribe() call was upsert-only, so a position that closed
            # at 10:30 stayed in _subscriptions forever; on the next WS
            # reconnect we'd re-subscribe the broker to the closed name's
            # ticks and waste CPU parsing them.
            self.ws_client.set_subscriptions(held_instruments)
            self._ws_subscribed_held = held
            logger.info(
                f"[WS] held-only resubscribe: {sorted(held)} "
                f"(was {sorted(prev)})"
            )
        except Exception as e:
            logger.warning(f"[WS] resubscribe failed: {e}")

    def _on_candle_close(self, symbol: str, interval: str, candle: dict):
        """Callback when a candle completes from tick aggregation."""
        logger.debug(f"Candle closed: {symbol}/{interval} C={candle['close']:.2f}")
        # Store to database
        import pandas as pd
        df = pd.DataFrame([candle])
        df["timestamp"] = pd.to_datetime(candle["timestamp"])
        df.set_index("timestamp", inplace=True)
        self.database.store_candles(symbol, interval, df[["open", "high", "low", "close", "volume"]])

    # ── Robustness Helpers ─────────────────────────────────────

    def _flush_pending_boot_alerts(self) -> None:
        """Drain ``self._pending_boot_alerts`` through ``self.alert_manager``.

        2026-05-18 (Audit Issue #2): the boot-time reconcile blocks
        (``reconcile_sl_orders_from_broker`` orphan-cancel failures and
        ``reconcile_positions_with_broker`` mismatches) used to call
        ``self.alert_manager.send_alert(...)`` inline. But those reconcile
        blocks run BEFORE ``self.alert_manager`` is constructed in
        ``__init__`` -- so every send_alert raised ``AttributeError`` and
        was silently swallowed by a bare ``except``. Plus the call was
        missing ``level=`` so even after the AttributeError was fixed the
        message would route via the default INFO channel rather than the
        critical channel an operator would actually be paged on.

        New contract: reconcile blocks APPEND a dict
        ``{"title": ..., "message": ..., "level": ...}`` to
        ``self._pending_boot_alerts``; this method flushes them right
        after ``AlertManager`` is constructed. Idempotent (clears the
        buffer at the end so re-calling this method is a no-op). Tolerant
        of a missing buffer (legacy path) and of individual send failures
        (the CRITICAL log was already fired at queue time, so the
        forensic trail is intact even if the alert dispatch fails)."""
        pending = getattr(self, "_pending_boot_alerts", None)
        if not pending:
            return
        for entry in pending:
            try:
                self.alert_manager.send_alert(
                    title=entry.get("title", "[CRITICAL] boot reconcile"),
                    message=entry.get("message", ""),
                    level=entry.get("level", "critical"),
                )
            except Exception as exc:
                logger.error(
                    f"[BOOT-ALERT] dispatch failed for "
                    f"{entry.get('title', '?')!r}: {exc!r}"
                )
        self._pending_boot_alerts = []

    def _persist_cooldown_state(self) -> None:
        """Atomically snapshot the three cooldown maps to data/cooldowns.json.

        Called from every mutation point (exit recording, blacklist bump,
        rejection-cooldown seed, daily reset). Failure is logged WARNING
        and swallowed -- a missed snapshot can never block trading."""
        try:
            save_cooldown_state(
                self._cooldown_map,
                self._stock_loss_today,
                self._rejection_cooldown_map,
                cooldown_side_map=self._cooldown_side_map,
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not block trading
            logger.warning(f"[COOLDOWN-PERSIST] save failed: {exc!r}")

    def _persist_trailing_states(self) -> None:
        """P1 #15 (2026-05-17): atomically snapshot all live TrailingStops.

        Called from the SL/TP/trailing/peak-giveback check path (every
        cycle the trail mutates) and after creation/removal. Failure is
        logged WARNING and swallowed."""
        try:
            save_trailing_states(self.risk_manager._trailing_stops)
        except Exception as exc:  # noqa: BLE001 - persistence must not block trading
            logger.warning(f"[TRAIL-PERSIST] save failed: {exc!r}")

    def _maybe_refresh_broker_session(self) -> None:
        """P2 restart-cluster (2026-05-17): refresh the AngelOne JWT before
        the 8h lifetime expires.

        Cheap pre-check (datetime comparison) runs every cycle. The actual
        re-login is gated to once-per-hour to avoid hammering AngelOne if
        login keeps failing. Re-login swaps ``self.execution._api`` and
        ``self.data_handler._api`` so the next API call uses the fresh JWT.

        Skipped entirely in paper mode (``_smart_api`` is None)."""
        if self._smart_api is None or self._broker_session_started_at is None:
            return
        age = datetime.now(IST) - self._broker_session_started_at
        # Refresh threshold: 7h. JWT expires at 8h, so we have a 1h grace
        # window in case the re-login itself takes a few attempts.
        if age < timedelta(hours=7):
            return
        # Don't retry more than once an hour
        if self._jwt_refresh_attempted_at is not None:
            if (datetime.now(IST) - self._jwt_refresh_attempted_at) < timedelta(hours=1):
                return
        self._jwt_refresh_attempted_at = datetime.now(IST)

        broker_cfg = self.config.get("broker", {})
        if broker_cfg.get("mode") == "paper":
            return
        try:
            from SmartApi import SmartConnect  # type: ignore
            import pyotp  # type: ignore
            api = SmartConnect(api_key=broker_cfg["api_key"])
            totp = pyotp.TOTP(broker_cfg["totp_secret"]).now()
            session = api.generateSession(
                broker_cfg["client_id"], broker_cfg["password"], totp
            )
            if not session or not session.get("status"):
                msg = (session or {}).get("message", "no message")
                logger.critical(
                    f"[JWT-REFRESH] re-login failed (age={age}): {msg}. "
                    f"Trading will start failing once the old JWT expires. "
                    f"Operator action: investigate immediately."
                )
                self.alert_manager.send_alert(
                    "JWT Refresh Failed",
                    f"AngelOne re-login at session age {age} failed: {msg}",
                    level="critical",
                )
                return
            # Swap the API objects everywhere the daemon holds a reference.
            self._smart_api = api
            self.execution._api = api
            self.data_handler._api = api
            self._broker_session_started_at = datetime.now(IST)
            logger.warning(
                f"[JWT-REFRESH] AngelOne session refreshed at "
                f"{self._broker_session_started_at.isoformat()} "
                f"(old session age was {age}). Trading continues."
            )
        except Exception as exc:  # noqa: BLE001 - must not crash trading
            logger.critical(
                f"[JWT-REFRESH] re-login raised {exc!r}. Next attempt in "
                f"~1h. Old JWT will expire at "
                f"{(self._broker_session_started_at + timedelta(hours=8)).isoformat()}."
            )

    def _persist_runtime_state(self) -> None:
        """P2 restart-cluster (2026-05-17): atomically snapshot the three
        intraday runtime maps to data/runtime_state.json. Called from every
        mutation point (strategy outcome record, open-window append, TP
        streak bump, daily reset). Failure is logged and swallowed."""
        try:
            save_runtime_state(
                self._strategy_state,
                self._recent_opens,
                self._consec_tp_today,
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not block trading
            logger.warning(f"[RUNTIME-PERSIST] save failed: {exc!r}")

    def _reset_daily_trackers(self):
        """Reset per-day trackers at the start of each new trading day."""
        today = datetime.now(IST).date()
        if self._daily_tracker_date != today:
            self._stock_loss_today.clear()
            self._cooldown_map.clear()
            # 2026-05-18 regression fix: parallel side tracker must reset
            # alongside the bare-symbol cooldown_map. Without this, a
            # stale side leaked across days and selectively blocked one
            # side of the symbol for the entire next session.
            self._cooldown_side_map.clear()
            self._consec_tp_today.clear()
            self._strategy_contrib_today.clear()
            self._strategy_state.clear()
            self._recent_opens.clear()
            self._rejection_cooldown_map.clear()
            self._eod_summary_sent = False
            self._did_premarket_scan_today = False
            self._daily_tracker_date = today
            # 2026-05-15: write the cleared snapshot so a same-day restart
            # picks up the wipe (otherwise yesterday's stale snapshot would
            # be loaded on next boot).
            self._persist_cooldown_state()
            # P2 restart-cluster (2026-05-17): same rationale for the
            # strategy_state / recent_opens / consec_tp_today snapshot.
            self._persist_runtime_state()
            # GC the previous day's EOD flag so logs/ doesn't accumulate
            # one .flag file per calendar day forever. We deliberately
            # don't touch today's flag here (it would only exist if the
            # daemon restarted across midnight, which is a different
            # situation we don't want to nuke).
            try:
                yesterday = (today - timedelta(days=1)).isoformat()
                stale = self._eod_flag_path(yesterday)
                if os.path.exists(stale):
                    os.remove(stale)
            except Exception as e:
                logger.debug(f"Could not GC stale EOD flag: {e}")
            logger.info("Daily trackers reset for new trading day")

    def _preflight_checks(self) -> bool:
        """Quick boot-time sanity check.

        Returns True if every CRITICAL check passes. Critical = without
        this, trades would either fail or be silently wrong. Non-critical
        items (stale model, missing email config) are logged as
        warnings but don't fail the check.

        Each check catches its own exceptions so a single broken probe
        doesn't crash the agent before it even starts.
        """
        logger.info("=" * 60)
        logger.info("[PREFLIGHT] Running boot-time health checks")
        logger.info("=" * 60)

        checks: List[Tuple[str, str, str]] = []
        critical_ok = True

        # 1. At least one active strategy loaded.
        try:
            n = len(self.strategies) if self.strategies else 0
            ok = n > 0
            critical_ok &= ok
            checks.append((
                "strategies_loaded",
                "PASS" if ok else "FAIL",
                f"{n} active",
            ))
        except Exception as e:
            critical_ok = False
            checks.append(("strategies_loaded", "FAIL", f"{type(e).__name__}: {e}"))

        # 2. Database is reachable. A read-only probe is enough to
        # catch the common-case failures (file gone, locked, schema
        # missing). Writes are exercised the moment the first trade
        # closes; if the FS is read-only, that error will surface
        # there, which we already alert on.
        try:
            _ = self.database.load_open_positions()
            checks.append(("database_reachable", "PASS", "ok"))
        except Exception as e:
            critical_ok = False
            checks.append(
                ("database_reachable", "FAIL", f"{type(e).__name__}: {e}")
            )

        # 3. Risk manager is alive (current_balance > 0). This catches
        # silent rehydration failures.
        try:
            bal = float(self.risk_manager.state.current_balance)
            ok = bal > 0
            critical_ok &= ok
            checks.append((
                "risk_state_alive",
                "PASS" if ok else "FAIL",
                f"balance={bal:.2f}",
            ))
        except Exception as e:
            critical_ok = False
            checks.append(("risk_state_alive", "FAIL", f"{type(e).__name__}: {e}"))

        # 4. ML model health (non-critical — strategy auto-degrades
        # to HOLD if unhealthy, so the agent can still run).
        try:
            xgb_strats = [
                s for s in self.strategies
                if s.__class__.__name__ == "XGBoostClassifier"
            ]
            if xgb_strats:
                strat = xgb_strats[0]
                healthy = (
                    strat.is_healthy()
                    if hasattr(strat, "is_healthy")
                    else (getattr(strat, "_model", None) is not None)
                )
                if healthy:
                    checks.append(("xgboost_model", "PASS", "loaded"))
                else:
                    reason = getattr(strat, "_unhealthy_reason", "unknown")
                    checks.append(
                        ("xgboost_model", "WARN", f"unhealthy: {reason}")
                    )
            else:
                checks.append(("xgboost_model", "SKIP", "not enabled"))
        except Exception as e:
            checks.append(("xgboost_model", "WARN", f"{type(e).__name__}: {e}"))

        # 5. Emergency-stop file MUST NOT pre-exist on boot. If it
        # does, the operator left it behind from a previous halt and
        # we'd shut down on cycle 1. Surface this clearly so they
        # remove it before launching.
        try:
            stop_path = getattr(self, "_emergency_stop_path", None)
            if stop_path and os.path.exists(stop_path):
                critical_ok = False
                checks.append((
                    "emergency_stop_clean",
                    "FAIL",
                    f"stale stop file at {stop_path} — `rm` it before starting",
                ))
            else:
                checks.append(("emergency_stop_clean", "PASS", "no stale stop"))
        except Exception as e:
            checks.append(
                ("emergency_stop_clean", "WARN", f"{type(e).__name__}: {e}")
            )

        # 6. Alerting (non-critical — the agent runs without email).
        try:
            alerts_enabled = bool(
                getattr(self, "alert_manager", None)
                and getattr(self.alert_manager, "enabled", False)
            )
            checks.append((
                "alerts",
                "PASS" if alerts_enabled else "WARN",
                "enabled" if alerts_enabled else "disabled (no email config)",
            ))
        except Exception as e:
            checks.append(("alerts", "WARN", f"{type(e).__name__}: {e}"))

        # Pretty-print summary
        for name, status, detail in checks:
            symbol = {"PASS": "OK ", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}[
                status
            ]
            level_fn = {
                "PASS": logger.info,
                "FAIL": logger.error,
                "WARN": logger.warning,
                "SKIP": logger.debug,
            }[status]
            level_fn(f"[PREFLIGHT] [{symbol}] {name:<24s} {detail}")
        logger.info("=" * 60)

        if not critical_ok:
            try:
                fails = [
                    f"{name}: {detail}"
                    for name, status, detail in checks
                    if status == "FAIL"
                ]
                self.alert_manager.send_alert(
                    "Preflight Failed — Agent Did Not Start",
                    "Boot health checks failed:\n\n" + "\n".join(fails),
                    level="critical",
                )
            except Exception:
                pass
        return critical_ok

    def _check_emergency_stop(self) -> bool:
        """File-based kill switch.

        Returns True if the configured stop file exists, in which case
        the caller MUST break out of the run loop. The method itself
        never raises — file-system errors are logged and treated as a
        no-op so a flaky FS doesn't accidentally shut us down.

        Operator UX:
            $ touch logs/STOP        # halt at start of next cycle
            $ rm logs/STOP           # remove before next start
        """
        path = getattr(self, "_emergency_stop_path", None)
        if not path:
            return False
        try:
            if not os.path.exists(path):
                return False
        except OSError as e:
            logger.debug(f"emergency_stop check FS error (ignored): {e}")
            return False

        # Found. Log + alert + (optionally) flatten + flag exit.
        logger.critical(
            f"[EMERGENCY-STOP] Stop file detected at {path}. "
            f"Halting agent at end of this cycle."
        )
        try:
            self.alert_manager.send_alert(
                "Emergency Stop Triggered",
                f"Stop file detected at {path}. Agent is shutting down. "
                f"Open positions will NOT be auto-flattened unless "
                f"`operations.emergency_stop_flatten` is true in config.\n\n"
                f"To re-arm the agent: delete the stop file and restart "
                f"the daemon.",
                level="critical",
            )
        except Exception as e:
            logger.warning(f"Failed to send emergency-stop alert: {e}")

        if getattr(self, "_emergency_stop_flatten", False):
            try:
                # Delegate to the EOD square-off path which already has
                # battle-tested order/portfolio/risk integration.
                self._square_off_all(reason="emergency_stop")
            except Exception as e:
                logger.error(f"Flatten on emergency stop failed: {e}")

        # Set _running so the inner while-loop terminates cleanly. The
        # `finally` block in `run()` will still run `_shutdown()` which
        # handles EOD summary + persistence. We DO NOT call _shutdown
        # here directly to avoid double-shutdown.
        self._running = False
        return True

    def _is_premarket_warmup_window(self) -> bool:
        """
        True iff we're in the [market_open - warmup, market_open) IST window
        on a trading weekday. Used to kick off a pre-market scan so the
        watchlist is fresh at 09:15 sharp.
        """
        if self._premarket_warmup_minutes <= 0:
            return False
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        try:
            open_h, open_m = map(int, self._market_open_str.split(":"))
        except (ValueError, AttributeError):
            return False
        market_open = now.replace(
            hour=open_h, minute=open_m, second=0, microsecond=0
        )
        warmup_start = market_open - timedelta(minutes=self._premarket_warmup_minutes)
        return warmup_start <= now < market_open

    def _build_strategy_mix_report(self) -> str:
        """Summarise today's ensemble strategy contribution mix.

        Returns a short multi-line string suitable for appending to the EOD
        report. Flags monoculture days (any single strategy >70 % of total
        contributions) so we can spot regimes where the ensemble collapses
        to a single model. Returns '' when no signals have been recorded.

        Two data sources, in priority order:
        1. Live in-memory `_strategy_contrib_today` (populated cycle-by-cycle
           by the running daemon — captures all *signals*, including those
           that didn't open a trade).
        2. DB-derived trade strategy attribution (post-hoc fallback, used
           when this method is called from a freshly-instantiated agent
           after a daemon crash — captures *opened trades* by lead strategy).

        The fallback is less granular but ensures the EOD email never has
        a silently-empty strategy section after a daemon restart.
        """
        # Live path: in-memory contributions tracked across the day.
        if sum(self._strategy_contrib_today.values()) > 0:
            return self._format_strategy_mix(
                self._strategy_contrib_today, label="Strategy mix today (signal weight):"
            )

        # Fallback: derive from today's closed-trade lead strategies.
        try:
            day_iso = datetime.now(IST).strftime("%Y-%m-%d")
            rows = self.database.load_trades_for_day(day_iso) or []
        except Exception:
            return ""
        counts: Dict[str, float] = {}
        for r in rows:
            strat = r.get("strategy") or "unknown"
            counts[strat] = counts.get(strat, 0.0) + 1.0
        if sum(counts.values()) <= 0:
            return ""
        return self._format_strategy_mix(
            counts, label="Strategy mix today (closed trades by lead strategy):"
        )

    @staticmethod
    def _format_strategy_mix(weights: Dict[str, float], label: str) -> str:
        """Render a strategy-weight dict as a fixed-width text block.

        Shared by both the live and DB-fallback paths so monoculture
        warnings and formatting stay consistent regardless of source.
        """
        total = sum(weights.values())
        if total <= 0:
            return ""
        ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
        top_strategy, top_weight = ranked[0]
        top_share = top_weight / total * 100
        lines = ["", label]
        for strat, weight in ranked:
            share = weight / total * 100
            lines.append(f"  {strat:<28} {share:>5.1f} %")
        if top_share >= 70.0:
            lines.append(
                f"NOTE: monoculture — {top_strategy} contributed "
                f"{top_share:.0f} % of signals. Ensemble diversity is broken "
                f"(other strategies muted or below threshold). Check regime "
                f"weighting / strategy warmup."
            )
        return "\n".join(lines)

    def _is_in_cooldown(self, symbol: str, side: Optional[str] = None) -> bool:
        """Check re-entry cooldown after a recent losing exit.

        P2 logic-edges (2026-05-17): legacy behavior cooled down BOTH sides
        of a symbol after a loss on either side. The audit pointed out that
        long stop-outs and short edges are independent -- a HDFCBANK long
        stop-out at 11:00 shouldn't block a HDFCBANK short signal that has
        nothing to do with the previous trade's thesis. We now key the
        cooldown by (symbol, side) when ``side`` is provided. When the
        caller doesn't pass a side (legacy code paths), the strict
        bare-symbol semantics are retained for back-compat.
        """
        last_exit = self._cooldown_map.get(symbol)
        if last_exit is None:
            return False
        # If a side is provided, only same-side stops should block re-entry.
        if side is not None:
            stored_side = self._cooldown_side_map.get(symbol)
            if stored_side is not None and stored_side != side:
                return False
        elapsed = datetime.now(IST) - last_exit
        return elapsed < self._reentry_cooldown

    def _is_stock_blacklisted(self, symbol: str) -> bool:
        """Check if a stock has hit its daily loss limit."""
        return self._stock_loss_today.get(symbol, 0) >= self._max_losses_per_stock

    @staticmethod
    def _parse_time_ranges(ranges: List[str]) -> List[Tuple[dtime, dtime]]:
        """Parse ['12:00-13:00', '14:30-14:45'] into [(time, time), ...]."""
        parsed: List[Tuple[dtime, dtime]] = []
        for r in ranges or []:
            try:
                s, e = r.split("-")
                sh, sm = map(int, s.split(":"))
                eh, em = map(int, e.split(":"))
                parsed.append((dtime(sh, sm), dtime(eh, em)))
            except Exception:
                logger.warning(f"Bad time range in config: '{r}' (expected 'HH:MM-HH:MM')")
        return parsed

    def _is_in_dead_hour(self) -> Tuple[bool, str]:
        """Return (True, 'HH:MM-HH:MM') if current time is in a configured
        dead-hour block where new entries are suppressed."""
        now_t = datetime.now(IST).time()
        for start, end in self._dead_hour_blocks:
            if start <= now_t < end:
                return True, f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        return False, ""

    def _is_past_late_cutoff(self) -> bool:
        """Check if we're past the late-day entry cutoff."""
        now = datetime.now(IST)
        h, m = map(int, self._late_entry_cutoff.split(":"))
        return now.hour > h or (now.hour == h and now.minute >= m)

    def _record_exit(self, symbol: str, pnl: float, exit_reason: str = "",
                     side: Optional[str] = None):
        """
        Record exit for cooldown and daily loss tracking.

        IMPORTANT: We only impose a re-entry cooldown after LOSING exits
        (stop_loss / negative signal). Profitable take_profit exits do NOT
        cool down the symbol — if the stock keeps trending, we want to be
        able to re-enter immediately and ride more of the move (Apr-28
        ADANIENSOL case: 3x TPs at +Rs 8 each but day high was +Rs 48).
        """
        is_loss = pnl < 0
        is_take_profit = exit_reason == "take_profit"
        # Trailing-stop hits in profit are also "won" exits — the trade
        # delivered, the trail just caught the reversal. Treat them like
        # TPs for cooldown purposes when comfortably positive (>= Rs 5).
        is_trailing_win = exit_reason == "trailing_stop" and pnl >= 5.0

        mutated = False
        if is_loss or (not (is_take_profit or is_trailing_win) and pnl < 5.0):
            # Only cool down on losses or low-conviction breakeven exits.
            self._cooldown_map[symbol] = datetime.now(IST)
            # P2 logic-edges (2026-05-17): record the side too so the
            # opposite-direction edge can still fire. ``side`` is the
            # POSITION's entry side ("BUY" for longs, "SELL" for shorts).
            if side:
                self._cooldown_side_map[symbol] = side
            mutated = True

        if is_loss:
            self._stock_loss_today[symbol] = self._stock_loss_today.get(symbol, 0) + 1
            mutated = True
            if self._stock_loss_today[symbol] >= self._max_losses_per_stock:
                logger.warning(f"[BLACKLIST] {symbol} blacklisted for today ({self._stock_loss_today[symbol]} losses)")

        # 2026-05-15 Persist cooldown / blacklist mutations so a container
        # restart between 09:30 and 14:30 doesn't expose ex-stop-out stocks
        # to re-entry. The save is fail-soft (logs WARNING, never raises).
        if mutated:
            self._persist_cooldown_state()
        else:
            # Track winning trail-runs: count consecutive TPs on the same stock today
            if is_take_profit:
                self._consec_tp_today[symbol] = self._consec_tp_today.get(symbol, 0) + 1
                logger.info(
                    f"[TP-STREAK] {symbol}: {self._consec_tp_today[symbol]} consecutive TPs today "
                    f"(no cooldown — letting trend continuation re-enter immediately)"
                )
                # P2 restart-cluster (2026-05-17): persist TP streak so a
                # restart between an entry and the next trend-continuation
                # entry doesn't forget that we've already won twice.
                self._persist_runtime_state()

    def _effective_signal_exit_floor(self, position, current_price: float) -> float:
        """Notional-aware unrealized-PnL floor for the signal-exit fast-path.

        Returns ``max(min_holding_pnl_rs, charges_multiple * round-trip-charges)``
        so that small positions are protected by the absolute floor (Rs 15
        avoids 1-rupee-win churn on the smallest trades) while large positions
        are protected by the relative floor (a Rs 30k position needs ~Rs 47
        unrealized to clear 1.5x its ~Rs 31 charges, not Rs 15).

        Falls back gracefully:
          - If charges module import fails, returns the absolute floor only.
          - If `min_holding_charges_multiple` is 0, returns the absolute floor
            (legacy behaviour).
          - If `min_holding_pnl_rs` is 0, the gate is fully disabled.
        """
        absolute_floor = self._min_holding_pnl_rs
        if absolute_floor <= 0:
            return 0.0
        if self._min_holding_charges_multiple <= 0:
            return absolute_floor
        try:
            from core.charges import compute_round_trip
            entry = float(position.entry_price)
            qty = int(position.quantity)
            if entry <= 0 or qty <= 0 or current_price <= 0:
                return absolute_floor
            # Use entry == exit for a charges-only round-trip estimate. This
            # over-estimates STT marginally but never under-estimates -- safer
            # for the floor we're computing.
            charges = compute_round_trip(entry, current_price, qty, product="INTRADAY")
            relative_floor = self._min_holding_charges_multiple * charges.total
            return max(absolute_floor, relative_floor)
        except Exception:
            return absolute_floor

    def _log_heartbeat(self):
        """Periodic health summary for remote monitoring."""
        now = datetime.now(IST)
        risk = self.risk_manager.get_risk_summary()
        positions = list(self.portfolio.positions.keys())
        logger.info(
            f"[HEARTBEAT] {now.strftime('%H:%M')} | Cycle={self._cycle_count} | "
            f"Positions={len(positions)} {positions} | "
            f"Cash=₹{self.portfolio.cash:,.0f} | "
            f"DayPnL=₹{risk['daily_pnl']:+,.0f} | "
            f"Trades={risk['daily_trades']} | "
            f"ConsecLoss={risk['consecutive_losses']} | "
            f"Cooldowns={list(self._cooldown_map.keys())} | "
            f"Blacklisted={[s for s, c in self._stock_loss_today.items() if c >= self._max_losses_per_stock]}"
        )
        # Best-effort: write JSON snapshot for the watchdog/preflight to read.
        try:
            self._write_health_json(now, risk, positions)
        except Exception as e:
            logger.warning(f"[HEARTBEAT] health.json write failed: {e}")

    def _write_health_json(self, now, risk: dict, positions: list) -> None:
        """Write a lightweight JSON health snapshot to `logs/health.json`.

        Designed to be cheap (small payload, atomic write) so it can run
        every heartbeat. Watchdogs / preflight should treat a stale `ts_unix`
        (older than ~3x heartbeat_interval x poll_seconds) as a hung daemon.
        """
        import json
        from pathlib import Path

        log_dir = Path(self.config.get("logging", {}).get("log_dir", "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "health.json"
        tmp = log_dir / "health.json.tmp"

        payload = {
            "ts": now.isoformat(timespec="seconds"),
            "ts_unix": int(now.timestamp()),
            "pid": os.getpid(),
            "mode": (self.config.get("broker", {}) or {}).get("mode", "unknown"),
            "cycle_count": int(self._cycle_count),
            "running": bool(self._running),
            "open_positions": positions,
            "open_position_count": len(positions),
            "cash": round(float(self.portfolio.cash), 2),
            "daily_pnl": round(float(risk.get("daily_pnl", 0.0)), 2),
            "daily_trades": int(risk.get("daily_trades", 0)),
            "consecutive_losses": int(risk.get("consecutive_losses", 0)),
            "drawdown_pct": round(float(risk.get("drawdown_pct", 0.0)), 2),
            "drawdown_tier": risk.get("drawdown_tier", "NORMAL"),
            "cooldowns": list(self._cooldown_map.keys()),
            "blacklisted": [
                s for s, c in self._stock_loss_today.items()
                if c >= self._max_losses_per_stock
            ],
        }
        # Atomic write: write to .tmp then rename so concurrent readers
        # never see a half-written file.
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _maybe_audit_checkpoint(self) -> None:
        """Write an audit checkpoint at most once per IST hour during market hours.

        Fires on the first cycle whose hour-of-day (IST) differs from the last
        write. Output goes to ``logs/audit/<YYYY-MM-DD>/checkpoint_HHMM.md`` and
        a sibling ``.json``. Failure here must never break the trading loop.
        """
        now = datetime.now(IST)
        # Active window: 09:00–16:00 IST. We extend slightly past close so the
        # final post-market checkpoint is captured.
        if not (9 <= now.hour <= 15) and not (now.hour == 16 and now.minute < 5):
            return
        if self._last_audit_hour == now.hour:
            return
        try:
            # Local import to avoid forcing the module load at daemon startup
            # if the optional dependency is missing.
            from tools.audit_checkpoint import run_and_save  # type: ignore
            db_path = self.config.get("database", {}).get("path", "data/trading_agent.db")
            md_path, _ = run_and_save(db_path=db_path, daemon_pid=os.getpid(), now=now)
            self._last_audit_hour = now.hour
            logger.info(f"[AUDIT-CHECKPOINT] {md_path.name} written ({now.strftime('%H:%M')})")
        except Exception as e:
            # Never fatal. Log once per cycle attempt and try again next hour.
            logger.warning(f"[AUDIT-CHECKPOINT-FAILED] {type(e).__name__}: {e}")

    def _build_daily_diagnostics(self, day_iso: str) -> str:
        """
        Build a detailed daily diagnostic report covering:
          - Win rate, R:R, profit factor, expectancy
          - Exit-reason breakdown (SL hit rate is the biggest lever)
          - By-hour P&L (spot loss-prone windows)
          - Symbol-level churn (repeated losers)
        Returns a multi-line string. Empty if no trades today.
        """
        try:
            rows = self.database.load_trades_for_day(day_iso)
        except Exception as e:
            logger.debug(f"Could not load today's trades: {e}")
            return ""

        if not rows:
            return ""

        wins = [r for r in rows if r.get("pnl", 0) > 0]
        losses = [r for r in rows if r.get("pnl", 0) <= 0]
        n = len(rows)
        total = sum(r.get("pnl", 0) for r in rows)
        avg_win = (sum(r["pnl"] for r in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(r["pnl"] for r in losses) / len(losses)) if losses else 0.0
        win_rate = len(wins) / n * 100 if n else 0.0
        rr = abs(avg_win / avg_loss) if avg_loss else 0.0
        pf_wins = sum(r["pnl"] for r in wins)
        pf_losses = abs(sum(r["pnl"] for r in losses)) or 1.0
        profit_factor = pf_wins / pf_losses if pf_losses else 0.0
        breakeven = (1.0 / (1.0 + rr) * 100) if rr else 100.0

        # Exit-reason bucket
        from collections import defaultdict
        exits: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
        for r in rows:
            b = exits[r.get("exit_reason", "?")]
            b["n"] += 1
            b["pnl"] += r.get("pnl", 0)
            if r.get("pnl", 0) > 0:
                b["wins"] += 1

        # By-hour
        hour_stats: Dict[int, Dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
        for r in rows:
            try:
                hr = int(r["entry_time"][11:13])
                b = hour_stats[hr]
                b["n"] += 1
                b["pnl"] += r.get("pnl", 0)
                if r.get("pnl", 0) > 0:
                    b["wins"] += 1
            except Exception:
                pass

        lines = [
            "",
            "--- DAILY DIAGNOSTICS ---",
            f"Trades: {n}  |  Wins: {len(wins)}  |  Losses: {len(losses)}",
            f"Win rate: {win_rate:.1f}%  |  R:R = 1:{rr:.2f}  |  Breakeven WR needed: {breakeven:.0f}%",
            f"Avg win: Rs {avg_win:+.2f}  |  Avg loss: Rs {avg_loss:.2f}  |  Profit factor: {profit_factor:.2f}",
            f"Expectancy per trade: Rs {total/n:+.2f}",
        ]

        if exits:
            lines.append("")
            lines.append("Exit mix:")
            for reason, b in sorted(exits.items(), key=lambda kv: -kv[1]["n"]):
                wr = b["wins"] / b["n"] * 100 if b["n"] else 0
                lines.append(
                    f"  {reason:<22} n={int(b['n']):<3} wins={int(b['wins']):<3} "
                    f"win%={wr:>4.0f}  pnl=Rs{b['pnl']:+8.2f}"
                )

        if hour_stats:
            lines.append("")
            lines.append("By-hour P&L:")
            for hr in sorted(hour_stats):
                b = hour_stats[hr]
                wr = b["wins"] / b["n"] * 100 if b["n"] else 0
                flag = "  <-- weak" if wr < 30 and b["n"] >= 2 else ""
                lines.append(
                    f"  {hr:02d}:00-{hr:02d}:59   n={int(b['n']):<3} win%={wr:>4.0f}  "
                    f"pnl=Rs{b['pnl']:+8.2f}{flag}"
                )

        # Actionable hint if clearly bleeding
        if rr and rr < 1.0 and win_rate < breakeven:
            lines.append("")
            lines.append(
                f"NOTE: avg loss > avg win. Either widen TP or tighten entries. "
                f"Need {breakeven:.0f}% WR at current R:R to break even."
            )

        # Plain-English glossary so the email is self-explanatory for anyone
        # reading it without context. Added 2026-05-06 after user request.
        lines.extend([
            "",
            "--- WHAT THESE METRICS MEAN ---",
            "- Win rate: % of trades that closed profitable. Higher is better,",
            "  but doesn't tell the whole story without R:R.",
            "- R:R (Risk:Reward): For every Rs 1 lost on a losing trade, how",
            "  many rupees you make on a winning trade. Healthy systems aim",
            "  for at least 1:1, ideally 1:2 or 1:3. Today's 1:0.40 means",
            "  winners are tiny vs losers - core problem when this is < 1:1.",
            "- Breakeven WR: At your current R:R, the win rate needed to",
            "  break even. If actual WR < breakeven WR, system loses over time.",
            "- Avg win / Avg loss: Average rupees made/lost per winning/",
            "  losing trade. When |avg loss| > avg win, you need a high WR",
            "  to compensate.",
            "- Profit factor (PF): gross profit / gross loss.",
            "    PF > 1.0  = profitable system",
            "    PF >= 1.5 = strong system",
            "    PF < 1.0  = bleeding (today: 0.61)",
            "- Expectancy per trade: Average expected rupees per trade.",
            "  Negative expectancy compounds with volume - more trades =",
            "  more loss.",
            "- Drawdown: Current pullback from your highest equity peak.",
            "  Agent halts trading for the day when drawdown hits 20%.",
            "- Exit mix: How trades exited. stop_loss having win%=0 is",
            "  expected (SL means predefined loss). The signal we want to",
            "  see is take_profit and trailing_stop having high win%.",
            "- By-hour P&L: Best/worst times of day. Helps spot when the",
            "  system bleeds vs prints (e.g. 09:00 hour today: -Rs 455).",
        ])

        return "\n".join(lines)

    def _build_open_positions_section(self) -> str:
        """Build a markdown-ish text block listing all currently-open positions
        with invested amount, current LTP, unrealised P&L, and capital
        deployment percentage. Used in the EOD summary so the recipient
        knows exactly what's carrying overnight and how each position is
        sitting at the close. Returns an empty string if no positions are
        open.

        LTP is fetched via the data handler. If LTP retrieval fails for a
        symbol (network blip, missing token), we fall back to the entry
        price so the row still renders cleanly with a 0 unrealised — the
        position table is best-effort, never a hard failure for the EOD
        email.
        """
        positions = list(getattr(self.portfolio, "positions", {}).values())
        if not positions:
            return ""

        ltps: Dict[str, float] = {}
        for p in positions:
            try:
                token = self._get_token(p.symbol) if hasattr(self, "_get_token") else ""
                ltp = self.data_handler.get_ltp(p.symbol, token or "")
                if ltp and ltp > 0:
                    ltps[p.symbol] = float(ltp)
            except Exception:
                continue

        lines = [
            "",
            "--- OPEN POSITIONS (carrying overnight) ---",
        ]
        total_invested = sum(
            (p.entry_price or 0.0) * (p.quantity or 0) for p in positions
        )
        total_unrealised = 0.0
        for p in positions:
            ltp = ltps.get(p.symbol, p.entry_price or 0.0)
            qty = p.quantity or 0
            entry = p.entry_price or 0.0
            if (p.side or "").upper() == "LONG":
                unr = (ltp - entry) * qty
            else:
                unr = (entry - ltp) * qty
            total_unrealised += unr

        try:
            cash = float(getattr(self.portfolio, "cash", 0) or 0)
        except Exception:
            cash = 0.0
        equity = cash + total_invested
        deploy_pct = (total_invested / equity * 100) if equity > 0 else 0.0

        lines.append(
            f"  {'Symbol':<12} {'Side':<5} {'Qty':>4} {'Entry':>9} "
            f"{'LTP':>9} {'Invested':>11} {'Unrl P&L':>10} "
            f"{'SL':>9} {'TP':>9}  Strategy"
        )
        for p in positions:
            invested = (p.entry_price or 0.0) * (p.quantity or 0)
            ltp = ltps.get(p.symbol, p.entry_price or 0.0)
            qty = p.quantity or 0
            entry = p.entry_price or 0.0
            if (p.side or "").upper() == "LONG":
                unr = (ltp - entry) * qty
            else:
                unr = (entry - ltp) * qty
            ltp_str = f"{ltp:>9.2f}" if p.symbol in ltps else f"{ltp:>8.2f}*"
            sl = p.stop_loss if p.stop_loss is not None else 0.0
            tp = p.take_profit if p.take_profit is not None else 0.0
            lines.append(
                f"  {p.symbol:<12} {p.side:<5} {p.quantity:>4} "
                f"{p.entry_price:>9.2f} {ltp_str} {invested:>11,.2f} "
                f"{unr:>+10,.2f} "
                f"{sl:>9.2f} {tp:>9.2f}  {p.strategy or ''}"
            )
        lines.append(
            f"  {'':<12} {'':<5} {'':>4} {'TOTAL':>9} {'':>9} "
            f"{total_invested:>11,.2f} {total_unrealised:>+10,.2f}"
        )
        # Asterisk legend only printed if any LTP fetch fell back to entry.
        if any(p.symbol not in ltps for p in positions):
            lines.append("  * = LTP unavailable, showing entry price (unrealised P&L treated as 0)")
        lines.append(
            f"  Capital deployed: {deploy_pct:.1f}% "
            f"(invested Rs {total_invested:,.2f}, cash Rs {cash:,.2f})"
        )
        lines.append(
            f"  Total unrealised P&L: Rs {total_unrealised:+,.2f} "
            f"(adds to realised at next close)"
        )
        return "\n".join(lines)

    def _eod_flag_path(self, day_iso: Optional[str] = None) -> str:
        """File-flag path used to make EOD-send idempotent across daemon
        restarts. We deliberately use a hidden file under ``logs/`` so the
        Cursor / VM log-puller doesn't accidentally surface it as audit
        data, and a date-stamped name so the flag self-expires at the
        next-day rollover.
        """
        if day_iso is None:
            day_iso = datetime.now(IST).strftime("%Y-%m-%d")
        log_dir = self.config.get("logging", {}).get("log_dir", "logs")
        return os.path.join(log_dir, f".eod_sent_{day_iso}.flag")

    def _load_eod_sent_flag(self) -> bool:
        """Return True if today's EOD has already been sent (flag exists).
        Returns False on any IO error -- erring on the side of "send
        anyway" rather than "silently miss the EOD email"."""
        try:
            return os.path.exists(self._eod_flag_path())
        except OSError:
            return False

    def _save_eod_sent_flag(self) -> None:
        """Touch the flag file after a successful EOD send so a subsequent
        daemon restart in the same calendar day finds it and short-circuits
        ``_maybe_send_eod_summary``. Silent on error -- the in-memory
        ``_eod_summary_sent`` flag is still set, so the current session is
        protected even if the disk write fails (only a restart could
        cause the duplicate, and that's a degraded-failure mode we accept
        rather than aborting the EOD pipeline)."""
        path = self._eod_flag_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(datetime.now(IST).isoformat(timespec="seconds"))
        except OSError as e:
            logger.warning(
                f"Could not persist EOD-sent flag at {path}: {e}. "
                f"A daemon restart before midnight could resend EOD."
            )

    def _maybe_send_eod_summary(self):
        """Auto-send end-of-day summary at the configured time."""
        if self._eod_summary_sent:
            return
        now = datetime.now(IST)
        h, m = map(int, self._eod_summary_time.split(":"))
        if now.hour > h or (now.hour == h and now.minute >= m):
            # P2 restart-cluster (2026-05-17): set the in-memory flag
            # BEFORE building the report (cheap dedupe against the
            # *same* in-process retry within the next minute) but DEFER
            # the on-disk flag write until AFTER ``send_alert`` succeeds.
            # The OLD code wrote the flag first, so any exception in
            # build_diagnostics / send_alert left the flag on disk and
            # the email never went out on restart. See [3] in the audit.
            self._eod_summary_sent = True
            summary = self.portfolio.get_summary()
            risk = self.risk_manager.get_risk_summary()

            day_iso = now.strftime("%Y-%m-%d")
            diag = self._build_daily_diagnostics(day_iso)
            strategy_mix = self._build_strategy_mix_report()
            open_pos_block = self._build_open_positions_section()

            # Compute today's WR directly from DB rather than from
            # `portfolio.metrics`. The portfolio object's win-rate field is
            # session-cumulative (resets when the daemon restarts) and was
            # showing 0% in post-hoc EOD runs after a daemon crash. The DB
            # query is the single source of truth for "what happened today".
            try:
                _today_rows = self.database.load_trades_for_day(day_iso) or []
                _wins = sum(1 for r in _today_rows if (r.get("pnl", 0) or 0) > 0)
                _wr = (_wins / len(_today_rows) * 100) if _today_rows else 0.0
            except Exception:
                _wr = 0.0

            # Equity context for drawdown: showing the peak alongside the
            # current equity and the halt threshold makes the percentage
            # meaningful at a glance.
            _peak = float(risk.get("peak_equity") or risk.get("peak", 0) or 0)
            _equity_now = float(summary.get("total_value", summary.get("cash", 0)) or 0)

            report = (
                f"EOD Report {day_iso}\n"
                f"Day PnL: Rs {risk['daily_pnl']:+,.2f}\n"
                f"Trades: {risk['daily_trades']}\n"
                f"Win Rate: {_wr:.0f}%\n"
                f"Cash: Rs {summary['cash']:,.2f}\n"
                f"Equity (mark-to-market): Rs {_equity_now:,.2f}"
                + (f"  (peak Rs {_peak:,.2f})" if _peak else "") + "\n"
                # P3 polish (2026-05-17): the OLD "[agent halts at 20%]"
                # was hardcoded; the actual halt threshold comes from
                # config.risk.drawdown_halt_pct (which the operator might
                # have raised/lowered). The hardcoded 20% was confusingly
                # close to but NOT equal to the configured value during
                # half the 2026 trading days. Render the configured value.
                f"Drawdown: {risk['drawdown_pct']:.1f}%   "
                f"[agent halts at {self.risk_manager.drawdown_halt_pct:.0f}%]"
                f"{open_pos_block}"
                f"{diag}"
                f"{strategy_mix}"
            )
            logger.info(f"[EOD SUMMARY]\n{report}")
            # 2026-05-04: Send a SINGLE consolidated EOD email. We used to
            # call both `send_alert("EOD Summary", ...)` AND
            # `send_daily_report(...)` here, which produced two near-
            # identical emails 1-2 seconds apart (verified in today's run:
            # 15:20:36 EOD Summary + 15:20:37 Daily Report). The
            # `EOD Summary` body is a strict superset of the Daily Report
            # (it includes the same metrics plus daily diagnostics and
            # strategy-mix breakdown), so the Daily Report call is pure
            # duplicate noise. Removed.
            # P2 restart-cluster (2026-05-17): only persist the EOD flag
            # AFTER send_alert returns without raising. If the SMTP call
            # blows up (timeout, auth fail, transient DNS), we want the
            # next restart to retry the email rather than silently
            # eat it.
            try:
                self.alert_manager.send_alert("EOD Summary", report, level="info")
                self._save_eod_sent_flag()
            except Exception as exc:  # noqa: BLE001
                # Reset in-memory flag so the next cycle in this same
                # process retries. The on-disk flag was not written so
                # a restart will also retry.
                self._eod_summary_sent = False
                logger.error(
                    f"[EOD] send_alert failed: {exc!r}. Flag NOT persisted "
                    f"to disk; next cycle will retry. (was: report-build "
                    f"succeeded but email transport failed)"
                )
                # Skip the post-mortem / journal blocks below since the
                # EOD email is what triggers them by convention.
                return

            # Self-learning daily journal
            try:
                self.trade_analyzer.write_daily_journal(
                    day_iso=now.strftime("%Y-%m-%d"),
                    market_summary=self._market_context,
                )
            except Exception as e:
                logger.error(f"Daily journal write failed: {e}")

            # Trade post-mortem (2026-05-07). Auto-runs against today's
            # closed trades, computes MFE / MAE / capture% / flag-summary
            # per trade, and emits a separate "Trade Post-Mortem" email
            # alongside the EOD summary. Failures are non-fatal — the EOD
            # email already went out.
            try:
                self._send_postmortem_email(day_iso)
            except Exception as e:
                logger.error(f"Post-mortem email failed: {e}")

            # Profit diagnostic (2026-05-09). Empirical edge analysis on
            # the last 7 days of closed trades — outputs Profit Factor,
            # Win Rate vs breakeven, Kelly fraction, and KILL/WATCH/KEEP/
            # SCALE verdict per strategy. Powers the daily "are we still
            # making money?" sanity check without manual re-runs. Same
            # subprocess-isolation pattern as post-mortem so any failure
            # is non-fatal. Loops 7d, not just today, because verdicts
            # need a sample of >=10 trades to be statistically meaningful.
            try:
                self._send_profit_diagnostic_email(day_iso)
            except Exception as e:
                logger.error(f"Profit diagnostic email failed: {e}")

    def _send_postmortem_email(self, day_iso: str) -> None:
        """Run tools/trade_postmortem.py for `day_iso` and send the
        resulting markdown report as an email.

        The post-mortem itself does network I/O (yfinance) for MFE/MAE
        computation, so it can take 5-15s for a typical day. Since it
        runs once per day inside the EOD path, latency is acceptable.
        Caller wraps in try/except so a post-mortem failure can't
        block the EOD email.
        """
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent
        report_path = repo_root / "logs" / "postmortem" / f"{day_iso}.md"
        try:
            subprocess.run(
                [sys.executable, str(repo_root / "tools" / "trade_postmortem.py"), day_iso],
                cwd=str(repo_root), check=False, capture_output=True, timeout=120,
            )
        except Exception as e:
            logger.warning(f"Post-mortem subprocess failed: {e}")
            return

        if not report_path.exists():
            logger.info(f"No post-mortem report at {report_path} (no trades?)")
            return

        body = report_path.read_text(encoding="utf-8")
        # Trim to email-friendly size (a 30-trade day generates ~6k chars).
        if len(body) > 50_000:
            body = body[:50_000] + "\n\n... (truncated, see full report on disk)"
        try:
            self.alert_manager.send_alert(
                f"Trade Post-Mortem {day_iso}",
                body,
                level="info",
            )
            logger.info(f"Post-mortem email sent ({len(body)} chars)")
        except Exception as e:
            logger.error(f"Post-mortem email send failed: {e}")

    def _send_profit_diagnostic_email(self, day_iso: str) -> None:
        """Run packages/research/diagnostic.py and email the rolling-7d verdict.

        Why 7 days, not 1?
          - profit_diagnostic produces KILL / WATCH / KEEP / SCALE verdicts
            using Profit Factor + Kelly fraction. Both are noisy on small
            samples (1 day = 5-15 trades). 7 days = 30-100 trades is the
            minimum sample where the verdict is statistically meaningful.
          - This means the email surfaces a STRATEGY edge view, not a daily
            P&L view (which the main EOD summary email already covers).
            They're complementary: EOD = "what happened today",
            profit-diagnostic = "is each strategy still earning its weight".

        Why subprocess vs in-process call?
          - Same isolation rationale as `_send_postmortem_email`: a numpy
            crash, DB connection issue, or pandas exception in the
            diagnostic must not poison the agent's main loop or block
            the next-cycle scan. Subprocess gives us a hard wall.
        """
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent
        diag_path = repo_root / "packages" / "research" / "diagnostic.py"
        # Pin output path so we can predict where to read it back, instead
        # of scanning logs/diagnostics/ for the newest file (race-free).
        out_path = repo_root / "logs" / "diagnostics" / f"eod_{day_iso}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(
                [sys.executable, str(diag_path), "--days", "7",
                 "--out", str(out_path)],
                cwd=str(repo_root), check=False, capture_output=True, timeout=60,
            )
        except Exception as e:
            logger.warning(f"Profit diagnostic subprocess failed: {e}")
            return

        if not out_path.exists():
            logger.info(
                f"No profit-diagnostic report at {out_path} "
                f"(insufficient trades in last 7 days?)"
            )
            return

        body = out_path.read_text(encoding="utf-8")
        if len(body) > 50_000:
            body = body[:50_000] + "\n\n... (truncated, see full report on disk)"

        # Pull the headline verdict from the report so the email subject
        # is actionable at a glance ("KILL/WATCH/SCALE detected" vs a
        # generic "Profit Diagnostic" prefix that's easy to skim past).
        verdict_tag = self._extract_verdict_tag(body)
        subject = f"Profit Diagnostic {day_iso} {verdict_tag}".strip()

        try:
            self.alert_manager.send_alert(subject, body, level="info")
            logger.info(f"Profit diagnostic email sent ({len(body)} chars){' ' + verdict_tag if verdict_tag else ''}")
        except Exception as e:
            logger.error(f"Profit diagnostic email send failed: {e}")

    @staticmethod
    def _extract_verdict_tag(report: str) -> str:
        """Scan a profit_diagnostic markdown report for the worst verdict
        present and return a short tag for the email subject.

        Priority (worst-first): KILL > WATCH > SCALE > KEEP.
        Returns "" when no verdict tokens are present (e.g. all strategies
        are INSUFFICIENT_DATA, which is an action-needed but ambiguous
        signal — the report body explains).

        The diagnostic renders the verdict as a markdown table cell:
          `| <strategy> | <trades> | <pf> | KILL | <pnl> |`
        so we anchor on the leading `| ` + space + tag + space + `|`. That
        avoids false matches on words like "KILL_FACTOR" anywhere in prose.
        """
        for tag in ("KILL", "WATCH", "SCALE", "KEEP"):
            if f"| {tag} |" in report:
                return f"[{tag}]"
        return ""

    # ── Market Context ────────────────────────────────────────

    def _refresh_market_context(self):
        """Fetch live India VIX and Nifty 50 trend from Yahoo Finance."""
        now = datetime.now(IST)
        if (self._market_ctx_last_refresh
                and now - self._market_ctx_last_refresh < self._market_ctx_refresh_interval):
            return

        try:
            import requests as _req
            sess = _req.Session()
            sess.verify = False
            sess.headers.update({"User-Agent": "Mozilla/5.0"})
            _chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

            def _fetch_close(ticker: str, range_str: str = "5d", interval: str = "1d"):
                try:
                    resp = sess.get(_chart_url.format(ticker=ticker),
                                    params={"range": range_str, "interval": interval},
                                    timeout=8)
                    if resp.status_code != 200:
                        return None
                    result = resp.json().get("chart", {}).get("result", [])
                    if not result:
                        return None
                    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    return [c for c in closes if c is not None] if closes else None
                except Exception:
                    return None

            # India VIX
            vix_closes = _fetch_close("^INDIAVIX", range_str="2d")
            if vix_closes and len(vix_closes) > 0:
                self._market_context["india_vix"] = round(vix_closes[-1], 2)
                logger.info(f"India VIX updated: {self._market_context['india_vix']}")

            # Nifty 50 vs 200 EMA
            nifty_closes = _fetch_close("^NSEI", range_str="1y", interval="1d")
            if nifty_closes and len(nifty_closes) >= 200:
                import pandas as _pd
                s = _pd.Series(nifty_closes)
                ema200 = s.ewm(span=200, adjust=False).mean().iloc[-1]
                current_nifty = s.iloc[-1]
                self._market_context["nifty_trend"] = 1 if current_nifty >= ema200 else -1
                logger.info(
                    f"Nifty trend: {'ABOVE' if self._market_context['nifty_trend'] == 1 else 'BELOW'} "
                    f"200 EMA (Nifty={current_nifty:.0f}, EMA200={ema200:.0f})"
                )
            elif nifty_closes:
                # P1 #11 (2026-05-17) -- LIVE-MODE SAFETY: when Yahoo returns
                # SOME Nifty data but fewer than 200 closes (transient API
                # truncation / weekend gap on a fresh boot), the OLD code pinned
                # nifty_trend = 1 (bull). That silently bypassed every bear-
                # regime defense (regime-sizing, short-selling gate, can_trade
                # 200-EMA filter). Bear-day false-positive cost is bigger than
                # missing one cycle of trend signal, so default to 0 (neutral
                # / unknown) which keeps the filters cautious without halting.
                logger.warning(
                    f"Nifty trend: insufficient history "
                    f"({len(nifty_closes)} closes < 200); defaulting to 0 "
                    f"(neutral). Regime-dependent gates will be cautious."
                )
                self._market_context["nifty_trend"] = 0

            # 2026-05-14 Intraday overlay -- pull a 1-min Nifty bar to compute
            # ~60-min momentum, and snapshot VIX vs morning open. Best-effort:
            # any failure leaves the overlay at "unknown" (permissive).
            try:
                intraday = _fetch_close("^NSEI", range_str="1d", interval="1m")
                if intraday and len(intraday) >= 60:
                    last = intraday[-1]
                    sixty_back = intraday[-60]
                    if sixty_back > 0:
                        pct = (last - sixty_back) / sixty_back * 100.0
                        self._market_context["nifty_intraday_pct"] = round(pct, 3)
            except Exception:
                pass
            try:
                vix_intraday = _fetch_close("^INDIAVIX", range_str="1d", interval="5m")
                if vix_intraday and len(vix_intraday) >= 1:
                    if self._market_context.get("vix_open") is None:
                        # First refresh of the session captures the open print
                        # so subsequent deltas are vs the morning baseline.
                        self._market_context["vix_open"] = round(vix_intraday[0], 2)
                    open_vix = self._market_context.get("vix_open") or vix_intraday[0]
                    delta = vix_intraday[-1] - open_vix
                    self._market_context["vix_intraday_delta"] = round(delta, 3)
            except Exception:
                pass

            from core.regime import classify_intraday_regime
            intraday_regime = classify_intraday_regime(self._market_context)
            self._market_context["intraday_regime"] = intraday_regime
            if intraday_regime in ("risk_on", "risk_off"):
                logger.info(
                    f"[INTRADAY-REGIME] {intraday_regime.upper()} | "
                    f"nifty_60m={self._market_context.get('nifty_intraday_pct')}% | "
                    f"vix_delta={self._market_context.get('vix_intraday_delta')}"
                )

            self._market_ctx_last_refresh = now
            # 2026-05-14 Push the freshly refreshed market context into any
            # strategy that opted in (`set_market_context`). This is how
            # the XGBoost classifier sees the live regime features that
            # the training pipeline injected (nifty_trend, india_vix).
            for strat in self.strategies:
                setter = getattr(strat, "set_market_context", None)
                if callable(setter):
                    try:
                        setter(self._market_context)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Market context refresh failed: {e}")

    # ── Trading Cycle ────────────────────────────────────────

    def _trading_cycle(self):
        """Single iteration of the trading loop."""
        now = datetime.now(IST)
        logger.debug(f"--- Cycle #{self._cycle_count} @ {now.strftime('%H:%M:%S')} ---")

        # Refresh VIX / Nifty trend periodically
        self._refresh_market_context()

        if not self.data_handler.is_market_open():
            # Pre-market guard (CRITICAL — 2026-05-07 incident). Before this
            # gate, ANY cycle that ran while market was closed would
            # `_square_off_all("market_close")` — including cycles that
            # ran BEFORE market opened. On 2026-05-07 the watchdog launched
            # the daemon at 08:56 IST (19 min before open); the very first
            # cycle saw 3 carryover positions and "squared them off" at
            # paper fills using yesterday's close LTPs (because real LTP
            # is unavailable when market is shut). Net hit: ₹-2.39 (a near-
            # wash by luck), but conceptually wrong: a real broker can't
            # fill orders pre-market, and these positions should either
            # have been auto-squared yesterday at 15:15 (MIS rule) or be
            # held until today's open and managed normally.
            #
            # New behaviour: in pre-market, do NOTHING. Don't touch
            # positions, don't run EOD, just wait. The carryover positions
            # will be picked up by the normal trading loop once market
            # opens at 09:15 — their SLs/TPs/trailing stops will apply
            # against real prices.
            if now.time() < self._market_open_time:
                # Pre-market: positions ride; daemon idles until 09:15.
                logger.debug(
                    f"Pre-market ({now.strftime('%H:%M:%S')} < "
                    f"{self._market_open_time}) — holding "
                    f"{self.portfolio.open_position_count} position(s) "
                    f"until market opens"
                )
                return

            # Post-market: square off any remaining positions, run EOD,
            # then stop. We do all three in the SAME cycle so the agent
            # exits within ~1s of market close, not 5+ minutes later.
            if self.portfolio.open_position_count > 0:
                self._square_off_all("market_close")

            # Run EOD summary (idempotent — only sends once per day)
            self._maybe_send_eod_summary()

            # Stop the agent as soon as market is closed. We use a proper time()
            # comparison (the old `hour>=15 and minute>=35` condition broke
            # after 16:00 because e.g. 16:05 has minute=5 which is not >=35).
            if now.time() >= self._market_close_time:
                logger.info(
                    f"Market closed ({now.strftime('%H:%M:%S')} >= {self._market_close_time}) "
                    f"— stopping agent (daemon will handle restart)"
                )
                self._running = False
            return

        # Carryover SL recompute (2026-05-07). On the first market-open
        # cycle of the day, tighten any overnight position's SL to at least
        # break-even so a profitable carryover can't gap-bust into a loss.
        # CROMPTON yesterday: peak +Rs 108, held overnight, gap-up cost
        # Rs 166 — a break-even SL would have made it a Rs 0 trade.
        if (self._carryover_sl_to_breakeven
                and self.portfolio.open_position_count > 0
                and self._carryover_sl_recomputed_date != now.date()):
            try:
                self._maybe_recompute_carryover_sl(now)
            except Exception as e:
                logger.error(f"Carryover SL recompute error: {e}")

        # Carryover profit-locking (2026-05-07). Fires once per day at
        # _carryover_lock_time (default 15:10), BEFORE the intraday flush.
        # Closes profitable carryover positions to avoid the CROMPTON-style
        # overnight gap-loss after a profitable session. Needs current LTPs;
        # fetch only if we have positions to consider.
        if (self.portfolio.open_position_count > 0
                and not self._carryover_lock_done
                and now.time() >= self._carryover_lock_time):
            try:
                ie_h, ie_m = map(int, self.risk_manager.intraday_exit_time.split(":"))
                if now.time() < dtime(ie_h, ie_m):
                    _carryover_prices = self.data_handler.get_multiple_ltp(self.instruments)
                    self._maybe_carryover_profit_lock(_carryover_prices)
            except Exception as e:
                logger.error(f"Carryover profit-lock error: {e}")

        # Intraday time exit — CRITICAL for MIS products. Before the fix, this
        # check existed in RiskManager.should_time_exit() but was never called,
        # so positions rode all the way to 15:30 market close instead of being
        # flushed at the configured intraday_exit_time (e.g. 15:15). That let
        # losing positions like PPLPHARMA (-Rs 48, 11:13→15:30) bleed for hours.
        if self.risk_manager.should_time_exit():
            if self.portfolio.open_position_count > 0:
                label = f"intraday_exit_{self.risk_manager.intraday_exit_time}"
                logger.warning(
                    f"Intraday exit time reached ({self.risk_manager.intraday_exit_time}) "
                    f"— flushing {self.portfolio.open_position_count} positions"
                )
                self._square_off_all(label)
            # Continue to EOD path (no new trades, risk gate will also block)
            # but don't hard-return — we still want to send EOD summary etc.

        # Risk gate
        can_trade, reason = self.risk_manager.can_trade(self._market_context)
        if not can_trade:
            # 2026-05-04: Dedup repeated identical block reasons. Without
            # this, post-15:15 cycles spam ~9 identical "Past intraday exit
            # time" warnings every cycle (verified in today's EOD audit).
            # Re-log only when the reason transitions (and demote repeats
            # to debug). The reason is reset to None whenever can_trade
            # returns True so we re-warn on the next transition into a
            # blocked state.
            last_reason = getattr(self, "_last_trade_block_reason", None)
            if reason != last_reason:
                logger.warning(f"Trading blocked: {reason}")
            else:
                logger.debug(f"Trading blocked (suppressed repeat): {reason}")
            self._last_trade_block_reason = reason
            # Still check SL/TP on open positions even if can't open new
            current_prices = self.data_handler.get_multiple_ltp(self.instruments)
            self._check_position_exits(current_prices)
            self.risk_manager.update_open_positions(self.portfolio.open_position_count)
            return
        # Transitioned back to a tradeable state — clear the dedup memory
        # so the next blocked reason re-emits a fresh warning.
        self._last_trade_block_reason = None

        current_prices = self.data_handler.get_multiple_ltp(self.instruments)
        ltp_count = sum(1 for p in current_prices.values() if p is not None)
        logger.info(f"LTP fetched: {ltp_count}/{len(self.instruments)} instruments")

        # Stale data guard: if we got prices for less than 30% of instruments, skip signal generation
        if len(self.instruments) > 0 and ltp_count / len(self.instruments) < 0.3:
            logger.warning(f"[STALE DATA] Only {ltp_count}/{len(self.instruments)} prices available — skipping signal generation")
            self._check_position_exits(current_prices)
            self.risk_manager.update_open_positions(self.portfolio.open_position_count)
            return

        # Check SL/TP (including trailing stops)
        self._check_position_exits(current_prices)

        # Classify current market regime once per cycle
        current_regime = classify_regime(self._market_context)

        # Generate signals → ensemble → act. Tally per-cycle stats so the
        # operator can SEE whether silence is "no data yet" vs. "data but no
        # votes" vs. "votes but low confidence". Before this, a silent cycle
        # was indistinguishable from a stuck agent.
        symbols_evaluated = 0
        symbols_with_data = 0
        total_votes = 0
        ensemble_holds = 0
        ensemble_acts = 0

        for instrument in self.instruments:
            symbol = instrument["symbol"]
            token = instrument.get("token", "")
            price = current_prices.get(symbol)
            if price is None:
                continue
            symbols_evaluated += 1

            # Collect signals from all strategies (skip strategies with
            # regime-prefs that zero them out in this regime).
            signals: List[TradeSignal] = []
            any_had_data = False
            for strategy in self.strategies:
                mult = regime_multiplier(strategy.name, current_regime)
                if mult <= 0.01:
                    logger.debug(f"[REGIME-SKIP] {strategy.name} muted in {current_regime}")
                    continue
                sig = self._evaluate_strategy(strategy, symbol, token)
                if sig is not None:
                    signals.append(sig)
                    any_had_data = True

            if any_had_data:
                symbols_with_data += 1

            if not signals:
                continue

            total_votes += sum(1 for s in signals if s.signal != Signal.HOLD)

            sig_summary = ", ".join(f"{s.strategy_name}={s.signal.name}" for s in signals)
            logger.info(f"{symbol} @ ₹{price:.2f} | Signals: [{sig_summary}] | regime={current_regime}")

            # ── EXIT FAST-PATH (2026-05-04) ────────────────────────────
            # When we already hold a position, an opposite-side signal
            # from ANY strategy at reasonable confidence should close it.
            # The full ensemble gates (confidence_threshold,
            # min_strategies_agree) are designed to keep entries
            # conservative — but applying them to exits leaves
            # mean_reversion EXIT signals (conf=0.45 by design) below
            # the 0.55 entry threshold, so profitable shorts stay open
            # until SL/TP. Today's bug: 3 RAILTEL/IDEA/NIVABUPA shorts
            # had EXIT signals fire 4+ times each but never closed.
            held_pos = self.portfolio.positions.get(symbol)
            fast_path_fired = False
            if (held_pos is not None
                    and self._signal_exit_min_conf > 0):
                closing_dir = (
                    Signal.SELL if held_pos.side == "BUY" else Signal.BUY
                )
                closing_signals = [
                    s for s in signals if s.signal == closing_dir
                ]
                if closing_signals:
                    best = max(closing_signals, key=lambda s: s.confidence)
                    if best.confidence >= self._signal_exit_min_conf:
                        # Min unrealized-PnL gate (2026-05-05). Don't churn
                        # out near break-even on signal exits — round-trip
                        # MIS charges turn nominal wins into net losses.
                        # SL/TP exits remain unconditional (this only gates
                        # signal-driven fast-path closes).
                        # 2026-05-14: floor is now notional-aware. The flat
                        # Rs 15 right-sized for Rs 5k notional under-protects
                        # a Rs 30k position whose round-trip charges are ~Rs 31.
                        floor_rs = self._effective_signal_exit_floor(held_pos, price)
                        if floor_rs > 0:
                            unreal = held_pos.unrealized_pnl(price)
                            if unreal < floor_rs:
                                logger.info(
                                    f"[EXIT-FAST-PATH-SKIP] {symbol} "
                                    f"{held_pos.side} qty={held_pos.quantity} "
                                    f"signal={best.strategy_name} "
                                    f"conf={best.confidence:.2f} but "
                                    f"unrealized=Rs {unreal:+.2f} < floor "
                                    f"Rs {floor_rs:.2f} — "
                                    f"keeping position open (SL/TP still active)"
                                )
                                continue
                        logger.info(
                            f"[EXIT-FAST-PATH] {symbol} {held_pos.side} "
                            f"qty={held_pos.quantity} closing on "
                            f"{best.strategy_name} {closing_dir.name} "
                            f"conf={best.confidence:.2f} "
                            f"(floor={self._signal_exit_min_conf:.2f}, "
                            f"bypassing ensemble consensus)"
                        )
                        close_signal = TradeSignal(
                            signal=closing_dir,
                            symbol=symbol,
                            price=price,
                            timestamp=best.timestamp,
                            strategy_name=f"exit_fast_path:{best.strategy_name}",
                            confidence=best.confidence,
                            stop_loss=best.stop_loss,
                            take_profit=best.take_profit,
                            metadata={
                                **(best.metadata or {}),
                                "exit_fast_path": True,
                                "underlying_strategy": best.strategy_name,
                            },
                            contributing_strategies={best.strategy_name: 1.0},
                        )
                        self._process_signal(close_signal, token, price)
                        ensemble_acts += 1
                        fast_path_fired = True

            if fast_path_fired:
                continue  # already actioned, skip ensemble path

            # Ensemble decision — regime-aware
            ensemble_signal = self.ensemble.aggregate(signals, symbol, price, regime=current_regime)
            if ensemble_signal and ensemble_signal.signal != Signal.HOLD:
                ensemble_acts += 1
                self._process_signal(ensemble_signal, token, price)
            else:
                ensemble_holds += 1

        # One-line cycle digest — compact visibility into decision funnel.
        # Single INFO line per cycle, collapses all silent branches into a
        # readable tally so you can SEE whether we're data-starved, vote-less,
        # or just below the confidence threshold.
        logger.info(
            f"[CYCLE-DIGEST] symbols={symbols_evaluated} "
            f"with_data={symbols_with_data} "
            f"directional_votes={total_votes} "
            f"ensemble_acts={ensemble_acts} "
            f"ensemble_holds={ensemble_holds} "
            f"threshold={self.ensemble.confidence_threshold:.2f} "
            f"regime={current_regime}"
        )

        self.risk_manager.update_open_positions(self.portfolio.open_position_count)

        # Dynamic confidence threshold (nudges the ensemble to be stricter
        # when we're losing, looser when we're winning). Evaluated each cycle
        # from recent risk-manager history so it reacts inside a single day.
        self._tune_confidence_threshold()

    def _tune_confidence_threshold(self):
        """
        Adjust the ensemble confidence threshold based on recent performance.

        Continuous mapping (replaces the per-cycle ratchet that used to
        saturate to the bound within ~4 cycles, 2026-05-13 audit):

            target = base + (0.5 - rolling_wr) * dynamic_span

        where `dynamic_span` = `max_dynamic_threshold - min_dynamic_threshold`.
        Examples (base=0.55, span=0.30):
            wr=1.0  -> target=0.40 (clamped at min=0.45)
            wr=0.65 -> target=0.505
            wr=0.5  -> target=0.55  (neutral, no change)
            wr=0.4  -> target=0.58
            wr=0.0  -> target=0.70

        Properties this gives us that the old ratchet didn't:
            * Smooth: a 5%-point WR change moves threshold by 1.5%-points.
            * Idempotent: running it every cycle with the same recent_results
              keeps the threshold pinned, instead of bumping further.
            * Mean-reverting: as soon as WR climbs back toward 0.5, the
              threshold drifts back to `base`. The ratchet had no path home.

        Window: last 10 trades from `risk_manager.state.recent_trade_results`.
        Floor: requires >= 5 closed trades to act (avoid tuning on noise).
        """
        recent = list(self.risk_manager.state.recent_trade_results)[-10:]
        if len(recent) < 5:
            return
        wins = sum(1 for p in recent if p > 0)
        wr = wins / len(recent)
        base = self.ensemble._base_confidence_threshold
        span = (self.ensemble._max_dynamic_threshold
                - self.ensemble._min_dynamic_threshold)
        # (0.5 - wr) * span gives:
        #   wr=0  ->  +span/2  (max strictness above base)
        #   wr=1  ->  -span/2  (max looseness below base)
        target = base + (0.5 - wr) * span
        self.ensemble.set_runtime_threshold(target)

    def _evaluate_strategy(self, strategy: BaseStrategy, symbol: str, token: str) -> Optional[TradeSignal]:
        try:
            timeframe = strategy.params.get("timeframe", "5min")

            # Try tick-aggregated data first (fresher)
            data = self.tick_aggregator.get_candle_history(symbol, timeframe, limit=200)

            # Fall back to REST API if no tick data. For intraday bars we ALWAYS
            # ask for at least 7 calendar days of history so early-in-session
            # cycles still have 20+ warmed-up bars available. Before this fix,
            # at 10:00 AM the window was only 5 hours = ~11 bars, which made
            # every strategy silently return None for the first 2 hours of
            # each session (masked as "agent is quiet" in the logs).
            if data.empty:
                end = datetime.now(IST)
                bars_needed = strategy.required_history_bars
                if "min" in timeframe:
                    minutes = int(timeframe.replace("min", ""))
                    needed_minutes = minutes * bars_needed * 2
                    # 7 calendar days covers weekends + holidays comfortably
                    start = end - timedelta(minutes=max(needed_minutes, 7 * 24 * 60))
                else:
                    start = end - timedelta(days=bars_needed * 2)
                data = self.data_handler.get_historical_data(symbol, timeframe, start, end)

            if data.empty or not strategy.is_data_sufficient(data):
                return None

            # Data-quality guard — reject NaN-infested / stale / suspiciously
            # spiked OHLCV before any strategy computes indicators on it.
            # A split or bad tick at bar -1 would otherwise produce garbage
            # signals (e.g. a 50% "breakout" that's really a data glitch).
            #
            # Logging policy: routine skips (transient staleness, brief gaps)
            # are noisy — ~50k warnings on a single bad session. Emit DEBUG
            # per-strategy and escalate to a single WARNING per symbol once
            # it crosses _DQ_WARN_AFTER consecutive failures (real outage).
            is_clean, why = check_data_quality(data)
            if not is_clean:
                streak = self._dq_failure_streak.get(symbol, 0) + 1
                self._dq_failure_streak[symbol] = streak
                logger.debug(f"[DATA-QUALITY] Skipping {symbol}/{strategy.name}: {why}")
                if streak >= self._DQ_WARN_AFTER and symbol not in self._dq_warned_symbols:
                    logger.warning(
                        f"[DATA-QUALITY] {symbol}: {streak} consecutive failures "
                        f"({why}) — suspected feed outage, further skips silenced"
                    )
                    self._dq_warned_symbols.add(symbol)
                return None

            # Healthy read: reset streak and clear any previous WARN flag so a
            # future outage is logged anew.
            if self._dq_failure_streak.get(symbol):
                self._dq_failure_streak[symbol] = 0
                self._dq_warned_symbols.discard(symbol)

            return strategy.generate_signal(data, symbol)
        except Exception as e:
            logger.error(f"Error evaluating {strategy.name}/{symbol}: {e}")
            return None

    @staticmethod
    def _leading_contributor(signal: TradeSignal) -> Optional[str]:
        """Return the strategy with the highest vote share in a signal."""
        contrib = getattr(signal, "contributing_strategies", None) or {}
        if not contrib:
            return None
        return max(contrib.items(), key=lambda kv: kv[1])[0]

    def _atr_gate_threshold(self, regime: Optional[str]) -> float:
        """Return the ATR% floor to apply for the given regime.

        Precedence (most- → least-specific):
          1. robustness.min_entry_atr_pct_by_regime[<regime>]  — regime-specific
          2. robustness.min_entry_atr_pct                       — flat fallback
        If neither is set, returns 0.0 (gate disabled).

        Rationale: a single flat ATR floor can't serve both low-vol (VIX<15)
        and high-vol (VIX>20) markets. In bull_low_vol conditions (2026-04-29)
        a 0.8% floor rejected 74 % of BUY signals; in bear_high_vol a 0.4%
        floor would allow too many noise trades.
        """
        if regime and regime in self._min_entry_atr_pct_by_regime:
            return self._min_entry_atr_pct_by_regime[regime]
        return self._min_entry_atr_pct

    def _get_indicator_snapshot(self, symbol: str) -> Dict:
        """Grab RSI, ATR%, volume_ratio from the latest feature-enriched data."""
        try:
            data = self.data_handler.get_historical_data(
                symbol, "5min",
                start_date=datetime.now(IST) - timedelta(hours=6),
                end_date=datetime.now(IST))
            if data.empty or len(data) < 14:
                return {}
            enriched = self.feature_engine.compute_all(data, self._market_context)
            last = enriched.iloc[-1]
            atr_val = last.get("atr", 0)
            price = last.get("close", 1)
            return {
                "rsi": round(float(last.get("rsi", 0)), 2) if not pd.isna(last.get("rsi")) else None,
                "atr_pct": round(atr_val / price * 100, 2) if price > 0 and not pd.isna(atr_val) else None,
                "volume_ratio": round(float(last.get("volume_ratio", 0)), 2) if not pd.isna(last.get("volume_ratio")) else None,
            }
        except Exception:
            return {}

    def _get_previous_close(self, symbol: str) -> Optional[float]:
        """
        Fetch previous day's close for circuit-limit comparison.

        Cached per-symbol for the day since yesterday's close doesn't change
        intraday. We derive it from the most recent daily candle we can find.
        """
        cached = self._prev_close_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            df = self.data_handler.get_historical_data(symbol, token=None, interval="1d", bars=3)
            if df is not None and not df.empty and len(df) >= 2:
                prev = float(df.iloc[-2]["close"])
                self._prev_close_cache[symbol] = prev
                return prev
        except Exception as e:
            logger.debug(f"Could not fetch prev close for {symbol}: {e}")
        return None

    def _pre_trade_safety_checks(
        self, symbol: str, current_price: float, cost: float,
        strategy: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Pre-trade guardrails before placing a new BUY:
          1. Circuit-limit proximity (avoid stocks near upper/lower daily band).
          1b. Event blackout (earnings/AGM in horizon).
          2. Sector concentration limit (with supersector roll-up).
          3. Single-symbol exposure limit.
          4. Window cap (max N opens per rolling X minutes).
          5. Strategy concurrency cap (2026-05-14): max N concurrent
             positions from a single strategy, prevents single-strategy
             pile-on like the 2026-05-14 supertrend_follow disaster.

        Returns (is_safe, reason). When unsafe, caller should skip the trade.
        """
        # 1. Circuit-band check
        prev_close = self._get_previous_close(symbol)
        if prev_close:
            # Pull day high/low if we have it (optional — still meaningful without)
            day_high = day_low = None
            try:
                df = self.data_handler.get_historical_data(
                    symbol, token=None, interval="1d", bars=2
                )
                if df is not None and not df.empty:
                    today = df.iloc[-1]
                    day_high = float(today["high"])
                    day_low = float(today["low"])
            except Exception:
                pass
            safe, reason = check_circuit_risk(
                current_price=current_price,
                previous_close=prev_close,
                day_high=day_high,
                day_low=day_low,
            )
            if not safe:
                return False, f"circuit_guard: {reason}"

        # 1b. Event blackout (2026-05-14). Skip stocks with a known catalyst
        # in the configured horizon -- earnings/dividend/AGM/board meeting
        # /buyback. Empty calendar = no-op so this never tightens behaviour
        # unless the operator populates data/event_calendar.csv.
        # getattr() guard keeps `__new__`-built test stubs from crashing.
        if getattr(self, "_event_blackout_enabled", False) and getattr(self, "event_calendar", None):
            blackout, ev = self.event_calendar.is_blackout(symbol)
            if blackout and ev is not None:
                return False, f"event_blackout: {ev.to_display()}"

        # 2. Sector concentration
        total_equity = self.portfolio.get_total_value(
            {s: p.entry_price for s, p in self.portfolio.positions.items()}
        )
        positions_by_symbol = {
            s: p.entry_price * p.quantity for s, p in self.portfolio.positions.items()
        }
        safe, reason = check_sector_exposure(
            symbol=symbol,
            current_positions_by_symbol=positions_by_symbol,
            additional_cost=cost,
            total_equity=total_equity,
            max_sector_exposure_pct=self._max_sector_exposure_pct,
            unknown_per_symbol=self._unknown_sector_per_symbol,
            # getattr() guard so test stubs built via __new__ (without
            # full __init__) don't trip the new 2026-05-14 supersector flag.
            use_supersectors=getattr(self, "_use_supersectors", True),
        )
        if not safe:
            return False, reason

        # 3. Single-symbol exposure (even across re-entries on same day)
        if total_equity > 0:
            symbol_exposure_pct = cost / total_equity * 100
            if symbol_exposure_pct > self._max_symbol_exposure_pct:
                return False, (
                    f"symbol_concentration: {symbol_exposure_pct:.1f}% > "
                    f"{self._max_symbol_exposure_pct}%"
                )

        # 4. Window cap — max N opens per rolling X minutes
        if self._max_opens_per_window > 0:
            now = datetime.now(IST)
            window_start = now - timedelta(minutes=self._opens_window_minutes)
            # Prune stale entries
            while self._recent_opens and self._recent_opens[0][0] < window_start:
                self._recent_opens.popleft()
            if len(self._recent_opens) >= self._max_opens_per_window:
                return False, (
                    f"window_cap: {len(self._recent_opens)} opens in last "
                    f"{self._opens_window_minutes}m >= {self._max_opens_per_window}"
                )

        # 5. Strategy-concurrency cap (2026-05-14). When the leading strategy
        # is known and the cap is enabled, count current open positions
        # tagged with the same strategy. Blocks the all-in-one-strategy
        # pile-on (today's live evidence: 8 supertrend_follow shorts in
        # 37 min, 4 stopped for -Rs 730).
        cap = getattr(self, "_max_positions_per_strategy", 0)
        if cap > 0 and strategy:
            try:
                count = sum(
                    1 for p in self.portfolio.positions.values()
                    if (p.strategy or "") == strategy
                )
            except Exception:
                count = 0
            if count >= cap:
                return False, (
                    f"strategy_concurrency: {count} '{strategy}' positions open "
                    f">= cap {cap}"
                )

        return True, "ok"

    def _record_position_open(self, symbol: str) -> None:
        """Append to the recent-opens deque for window-cap tracking. Called
        right after a successful order placement."""
        if self._max_opens_per_window > 0:
            self._recent_opens.append((datetime.now(IST), symbol))
            # P2 restart-cluster (2026-05-17): persist so a mid-window
            # restart doesn't reset the open-rate counter and let us
            # burst through the cap.
            self._persist_runtime_state()

    def _audit_reject(
        self,
        signal: TradeSignal,
        price: float,
        reason: str,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> None:
        """Uniform helper for logging a signal rejection to the audit CSV.

        Also seeds the rejection-cooldown map so we don't re-evaluate the
        same (symbol, direction) for `rejection_cooldown_minutes` minutes —
        unless the rejection reason hinges on portfolio state (which clears
        on its own).
        """
        try:
            regime = classify_regime(self._market_context)
        except Exception:
            regime = None
        direction = "BUY" if signal.signal == Signal.BUY else "SELL"
        try:
            self.signal_audit.log(
                symbol=signal.symbol,
                direction=direction,
                confidence=signal.confidence,
                regime=regime,
                price=price,
                strategy=self._leading_contributor(signal) or signal.strategy_name,
                contributing=signal.contributing_strategies,
                outcome="REJECTED",
                reason=reason,
                stop_loss=stop_loss,
                take_profit=take_profit,
                quantity=quantity,
            )
        except Exception:
            pass

        # Seed the rejection cooldown unless this is a state-dependent reason.
        # Use getattr defensively — some test fixtures construct stub agents
        # that bypass __init__ (see tests/test_short_selling.py) and won't
        # have the cooldown attributes wired up.
        cooldown_secs = getattr(self, "_rejection_cooldown_seconds", 0)
        if (cooldown_secs > 0
                and signal.signal != Signal.HOLD
                and not self._reason_skips_cooldown(reason)):
            cooldown_map = getattr(self, "_rejection_cooldown_map", None)
            if cooldown_map is not None:
                cooldown_map[(signal.symbol, direction)] = datetime.now(IST)
                # 2026-05-15 Persist on rejection-cooldown mutation. See
                # _persist_cooldown_state docstring for rationale.
                persist = getattr(self, "_persist_cooldown_state", None)
                if callable(persist):
                    persist()

    def _reason_skips_cooldown(self, reason: str) -> bool:
        """Whether this rejection reason should NOT seed the cooldown map.

        Reasons that hinge on portfolio state (already_open, blacklist,
        etc.) clear naturally when state changes — we don't want a stale
        cooldown to keep blocking after the underlying condition resolves.
        """
        prefix = reason.split(":", 1)[0]
        skip_reasons = getattr(
            self, "_rejection_cooldown_skip_reasons",
            ("already_open", "blacklist", "cooldown", "shorts_disabled"),
        )
        return prefix in skip_reasons

    def _is_rejection_cooldown_active(self, symbol: str, direction: str) -> bool:
        """True iff (symbol, direction) was rejected within the cooldown window."""
        cooldown_secs = getattr(self, "_rejection_cooldown_seconds", 0)
        if cooldown_secs <= 0:
            return False
        cooldown_map = getattr(self, "_rejection_cooldown_map", None)
        if cooldown_map is None:
            return False
        last = cooldown_map.get((symbol, direction))
        if last is None:
            return False
        elapsed = (datetime.now(IST) - last).total_seconds()
        return elapsed < cooldown_secs

    def _is_in_opening_lockout(self, now: datetime) -> bool:
        """Return True during the opening-bar lockout window.

        Window is (_market_open_time, _market_open_time + opening_lockout_minutes).
        Set `risk.opening_lockout_minutes` to 0 to disable.

        Why this exists: 30-day post-mortem (2026-05-07) showed a heavy
        loss cluster in the first 5-10 min of the session — wide spreads,
        no price discovery, gap-driven false signals. Blocking NEW opens
        in this window historically would have prevented several MR losses
        without sacrificing meaningful upside (the same setups usually
        re-trigger 5-15 min later at fairer prices).
        """
        # `getattr` defaults preserve compatibility with test harnesses
        # that bypass __init__ via TradingAgent.__new__ (e.g.
        # test_short_selling.py builds a partial agent for routing tests).
        lockout_minutes = getattr(self, "_opening_lockout_minutes", 0)
        if lockout_minutes <= 0:
            return False
        mo = getattr(self, "_market_open_time", dtime(9, 15))
        market_open = now.replace(
            hour=mo.hour, minute=mo.minute, second=0, microsecond=0,
        )
        end = market_open + timedelta(minutes=lockout_minutes)
        return market_open <= now < end

    def _process_signal(self, signal: TradeSignal, token: str, current_price: float):
        """Act on an ensemble-validated signal.

        Routing:
          * BUY  + no position → open LONG
          * BUY  + held LONG   → duplicate, reject
          * BUY  + held SHORT  → signal-based cover (exit short)
          * SELL + held LONG   → signal-based exit (close long)
          * SELL + held SHORT  → duplicate, reject
          * SELL + no position → open SHORT (if enabled + regime allows)
        """
        # Fix 6: record the strategy mix for every actionable ensemble
        # signal so the EOD summary can flag strategy-monoculture days
        # (e.g. 2026-04-30: 83/87 contributions from mean_reversion alone).
        try:
            contrib = getattr(signal, "contributing_strategies", None) or {}
            for strat, weight in contrib.items():
                self._strategy_contrib_today[strat] = (
                    self._strategy_contrib_today.get(strat, 0.0) + float(weight)
                )
        except Exception:
            pass

        symbol = signal.symbol
        pos = self.portfolio.positions.get(symbol)

        # Opening-bar lockout (2026-05-07). Block new opens during the
        # first N minutes of the session. Exits (closing existing positions)
        # are still allowed — we don't want SL/TP starved during the
        # lockout. Only `signal_in (BUY, SELL) and pos is None` is a
        # *new open*; the other branches all require a held position.
        if pos is None and signal.signal in (Signal.BUY, Signal.SELL):
            now_ist = datetime.now(IST)
            if self._is_in_opening_lockout(now_ist):
                logger.info(
                    f"[OPENING-LOCKOUT] Skipping {signal.signal.value} {symbol}: "
                    f"in {self._opening_lockout_minutes}-min opening window"
                )
                self._audit_reject(signal, current_price, "opening_lockout")
                return

        if signal.signal == Signal.BUY:
            if pos is None:
                # Long-entry regime guard (2026-05-05). Symmetric mirror of
                # the short-selling regime guard. When configured, BUY
                # entries only fire if the current regime is in the allow
                # list. Empty allow list = permissive (legacy default).
                if self._long_entry_regimes:
                    regime = classify_regime(self._market_context)
                    if regime not in self._long_entry_regimes:
                        logger.info(
                            f"[LONG-REGIME] Skipping BUY {symbol}: "
                            f"regime={regime} not in allowed "
                            f"{sorted(self._long_entry_regimes)}"
                        )
                        self._audit_reject(
                            signal, current_price, f"long_regime:{regime}"
                        )
                        return
                # 2026-05-14 Intraday-regime overlay: even in a bull daily
                # regime, block new longs during a clear risk-off intraday
                # event (Nifty -0.5% in last 60min OR VIX spike +1.5).
                # Catches flash-crash / mid-session reversal cases the
                # daily regime can't see.
                if self._intraday_regime_block_longs:
                    intra = self._market_context.get("intraday_regime")
                    if intra == "risk_off":
                        logger.info(
                            f"[INTRADAY-REGIME] Skipping BUY {symbol}: "
                            f"intraday=risk_off "
                            f"(nifty_60m={self._market_context.get('nifty_intraday_pct')}%, "
                            f"vix_delta={self._market_context.get('vix_intraday_delta')})"
                        )
                        self._audit_reject(
                            signal, current_price, "intraday_risk_off"
                        )
                        return
                self._open_new_position(signal, token, current_price, side="BUY")
                return
            if pos.side == "SELL":
                # BUY signal while short → treat as cover-on-signal.
                self._exit_on_signal(signal, pos, token, current_price)
                return
            # Duplicate long
            self._audit_reject(signal, current_price, "already_open:duplicate")
            return

        if signal.signal == Signal.SELL:
            if pos is None:
                # Consider opening a SHORT. Guarded by feature flag + regime.
                if not self._enable_short_selling:
                    logger.debug(f"SELL signal for {symbol} ignored (shorts disabled)")
                    self._audit_reject(signal, current_price, "shorts_disabled")
                    return
                regime = classify_regime(self._market_context)
                if regime not in self._short_selling_regimes:
                    logger.info(
                        f"[SHORT-REGIME] Skipping SELL {symbol}: regime={regime} "
                        f"not in allowed {sorted(self._short_selling_regimes)}"
                    )
                    self._audit_reject(signal, current_price, f"short_regime:{regime}")
                    return
                self._open_new_position(signal, token, current_price, side="SELL")
                return
            if pos.side == "BUY":
                # Signal-based exit of a long position
                self._exit_on_signal(signal, pos, token, current_price)
                return
            # Duplicate short
            self._audit_reject(signal, current_price, "already_open:duplicate_short")

    # P0 #1 (2026-05-15) — LIVE-MODE SAFETY: coordinated exit helper.
    #
    # Previous pattern on every close path was:
    #   (a) place flatten order
    #   (b) close portfolio in-memory
    #   (c) cancel broker SL-M
    # The window between (a) and (c) left BOTH orders live at the broker.
    # An adverse tick could trigger the SL-M and the flatten in the same
    # round-trip → accidentally short (or long) after we thought we'd
    # flattened. The orphaned-SL hazard the SL registry was designed to
    # prevent was effectively still present for the duration of every exit.
    #
    # New ordering:
    #   1. Cancel broker SL-M.
    #   2. Place flatten.
    #   3. Update internal portfolio + risk state on flatten success.
    #
    # New edge cases the helper handles:
    #   • Cancel fails but SL is tracked → log CRITICAL, still flatten (a
    #     stuck broker SL is preferable to a stuck open position).
    #   • Flatten fails AFTER a successful cancel → CRITICAL alert + return
    #     None. Position is naked at broker until ops intervenes or P0 #4's
    #     boot-time broker reconciliation re-arms it.
    def _close_position_safely(
        self,
        symbol: str,
        token: Optional[str],
        exit_side: str,
        quantity: int,
        price: float,
        tag: str,
        exit_reason: str,
    ) -> tuple[Optional[dict], Optional[Any]]:
        """Cancel broker SL then place flatten. See class-level note above.

        Returns (order_dict, trade_record). Either element may be None on
        failure; callers should branch on order being None vs record being
        None (e.g. portfolio.close_position returned no record).

        P0 #2 residual (2026-05-18) — LIVE-MODE SAFETY: the entire
        cancel-SL → place-flatten → close-portfolio sequence MUST run
        under `self._exit_check_lock` so the WS-tick exit path and the
        signal / square-off / carryover-lock paths cannot both submit
        flatten orders for the same symbol. The lock is an RLock so this
        re-acquire is free when the caller is already inside
        `_check_position_exits_locked`.
        """
        with self._exit_check_lock:
            # Idempotency: another thread may have already closed this
            # position while we were queued behind the lock. Without this
            # re-check the second caller would `cancel_sl_order_for_symbol`
            # (no-op now), submit a second flatten order, and ride the
            # accidental reverse position until the next safety gate
            # caught it (potentially never, since `portfolio.positions`
            # is now empty for that symbol).
            if symbol not in self.portfolio.positions:
                logger.info(
                    f"[SAFE-EXIT] {symbol} already closed by another thread "
                    f"(tag={tag}, reason={exit_reason}). Skipping double-flatten."
                )
                return None, None

            sl_existed = self.execution.get_sl_order_for_symbol(symbol) is not None
            cancel_ok = self.execution.cancel_sl_order_for_symbol(symbol)
            if sl_existed and not cancel_ok:
                logger.critical(
                    f"[SAFE-EXIT] {symbol}: broker SL cancel FAILED before flatten "
                    f"(tag={tag}, reason={exit_reason}). Double-fill possible; "
                    f"proceeding with flatten anyway (stuck SL is the lesser evil)."
                )

            order = self.execution.place_order(
                symbol=symbol, token=token, transaction_type=exit_side,
                quantity=quantity, price=price, tag=tag,
            )
            flatten_ok = bool(order and order.get("status") in ("FILLED", "PLACED"))
            if not flatten_ok:
                if cancel_ok and sl_existed:
                    logger.critical(
                        f"[SAFE-EXIT] {symbol}: cancel OK but flatten FAILED. "
                        f"POSITION IS NAKED AT BROKER (no SL, no exit). "
                        f"Manual reconciliation required. tag={tag}."
                    )
                    try:
                        self.alert_manager.send_alert(
                            f"CRITICAL: Naked position {symbol}",
                            f"{symbol}: broker SL cancelled but flatten order failed "
                            f"(tag={tag}, reason={exit_reason}). Position is open "
                            f"without protection. Square off manually NOW.",
                            level="critical",
                        )
                    except Exception:
                        pass
                return None, None

            filled_price = order.get("filled_price") or price
            record = self.portfolio.close_position(symbol, filled_price, exit_reason=exit_reason)
            if record is None:
                return order, None

            self.risk_manager.record_trade(record.pnl)
            self.risk_manager.remove_trailing_stop(symbol)
            # P1 #15 (2026-05-17): persist after the removal so the next-boot
            # rehydrate doesn't try to restore state for a closed position.
            self._persist_trailing_states()
            # P2 logic-edges (2026-05-17): plumb the side through so the
            # cooldown can be keyed by (symbol, side).
            self._record_exit(
                symbol, record.pnl, exit_reason=exit_reason,
                side=getattr(record, "side", None),
            )
            self._on_trade_closed(record)
            return order, record

    def _exit_on_signal(
        self,
        signal: TradeSignal,
        pos: "Position",
        token: str,
        current_price: float,
    ):
        """Close an existing position on a reversing ensemble signal.

        Used for both long-exits (SELL signal while long) and short-covers
        (BUY signal while short). SL/TP checks remain the primary exit
        mechanism via `_check_position_exits`; this is the signal-driven path.
        """
        symbol = pos.symbol
        # Minimum holding guard: don't let a noisy signal flip us out
        # within the first N minutes of entry. SL and TP still fire
        # unconditionally through _check_position_exits; this only
        # protects against immediate whipsaw signal reversals.
        if self._min_holding_minutes > 0:
            try:
                entry = pos.entry_time
                if entry.tzinfo is None:
                    entry = IST.localize(entry)
                held_min = (datetime.now(IST) - entry).total_seconds() / 60.0
                if held_min < self._min_holding_minutes:
                    logger.info(
                        f"[MIN-HOLD] Ignoring signal-exit for {symbol}: "
                        f"held {held_min:.1f}m < min {self._min_holding_minutes:.1f}m "
                        f"(signal {signal.confidence:.2f})"
                    )
                    return
            except Exception:
                pass

        # P0 #1 (2026-05-15): cancel broker SL BEFORE flatten via the safe helper.
        exit_tx = "SELL" if pos.side == "BUY" else "BUY"
        order, record = self._close_position_safely(
            symbol=symbol, token=token, exit_side=exit_tx,
            quantity=pos.quantity, price=current_price,
            tag=signal.strategy_name, exit_reason="signal",
        )
        if order and record:
            filled_price = order.get("filled_price") or current_price
            self.alert_manager.send_trade_alert(
                exit_tx, symbol, pos.quantity, filled_price,
                signal.strategy_name, pnl=record.pnl,
            )

    def _open_new_position(
        self,
        signal: TradeSignal,
        token: str,
        current_price: float,
        *,
        side: str,
    ):
        """Run the full entry pipeline (gates → sizing → order) for either
        a LONG (side="BUY") or SHORT (side="SELL") position.

        All side-specific math flows through the `side` parameter; all gates
        are applied symmetrically. This is the ONLY path that creates new
        positions, so adding/removing a gate only has to happen once.
        """
        symbol = signal.symbol
        direction_label = "BUY" if side == "BUY" else "SELL"

        # Single-shot enforcement (Stage 3 e2e safety, --single-shot flag).
        # Once a symbol has had a full round-trip today, refuse re-entry. This
        # caps the maximum number of fills per symbol per day at 2 (one entry,
        # one exit) and prevents pyramiding / re-entering on the same name
        # after a stop-out -- critical for Stage 3 live basket runs.
        self._reset_single_shot_state_if_new_day()
        if self._single_shot_enabled and symbol in self._symbols_done_today:
            logger.info(
                f"[SINGLE-SHOT] Refusing new {direction_label} on {symbol}: "
                f"already round-tripped today."
            )
            self._audit_reject(signal, current_price, "single_shot:already_round_tripped")
            return

        # Rejection-cooldown short-circuit (2026-05-04 part 4). If this same
        # (symbol, direction) tuple was rejected by a persistent gate within
        # the cooldown window, skip evaluation entirely. Saves CPU + audit-row
        # noise — see __init__ for the rationale.
        if self._is_rejection_cooldown_active(symbol, direction_label):
            logger.debug(
                f"[REJECT-COOLDOWN] Skipping {direction_label} {symbol}: "
                f"recently rejected (cooldown active)"
            )
            return

        # P2 logic-edges (2026-05-17): cooldown now keyed by (symbol, side).
        # A losing long stop-out at 11:00 still blocks new longs on the
        # same name for ``reentry_cooldown_minutes``, but no longer blocks
        # an independent short edge that came in later. See _is_in_cooldown
        # docstring for the rationale.
        if self._is_in_cooldown(symbol, side=direction_label):
            remaining = self._reentry_cooldown - (datetime.now(IST) - self._cooldown_map[symbol])
            logger.info(
                f"[COOLDOWN] Skipping {symbol} {direction_label}: re-entry "
                f"cooldown ({remaining.seconds // 60}m remaining)"
            )
            self._audit_reject(signal, current_price, f"cooldown:{remaining.seconds // 60}m")
            return
        if self._is_stock_blacklisted(symbol):
            logger.info(f"[BLACKLIST] Skipping {symbol}: hit {self._max_losses_per_stock} losses today")
            self._audit_reject(signal, current_price, "blacklist:loss_cap")
            return
        if self._is_past_late_cutoff():
            logger.info(f"[LATE CUTOFF] Skipping new {direction_label} for {symbol}: past {self._late_entry_cutoff}")
            self._audit_reject(signal, current_price, f"late_cutoff:{self._late_entry_cutoff}")
            return

        # Dead-hour filter (2026-04-28 audit finding: noon lull loses money)
        in_dead, dead_label = self._is_in_dead_hour()
        if in_dead:
            logger.info(
                f"[DEAD-HOUR] Skipping {symbol}: inside {dead_label} block "
                f"(historically low win rate window)"
            )
            self._audit_reject(signal, current_price, f"dead_hour:{dead_label}")
            return

        can_trade, reason = self.risk_manager.can_trade(self._market_context)
        if not can_trade:
            self._audit_reject(signal, current_price, f"risk_gate:{reason}")
            return

        # Indicator snapshot (also feeds pattern memory)
        snap = self._get_indicator_snapshot(symbol)
        now = datetime.now(IST)

        # ATR% gate — regime-aware floor; quiet stocks can't cover charges.
        # 2026-05-04: Conviction-aware relaxation. The base regime threshold
        # is calibrated for typical setups (conf 0.55-0.70). This afternoon's
        # run surfaced 15/16 ensemble passes blocked here as the mid-day
        # market went exceptionally calm — including ACMESOLAR conf=0.935
        # and a multi-strategy BELRISE setup. We relax the threshold for
        # the highest-conviction signals — those that earned an exceptional
        # ensemble decision via either >=0.85 confidence or multi-strategy
        # convergence — so the system can still take its best swings even
        # when intraday volatility temporarily compresses. A hard 0.20%
        # floor remains: below that, no TP can be hit within session
        # regardless of edge.
        atr_pct = snap.get("atr_pct")
        current_regime = classify_regime(self._market_context)
        atr_threshold = self._atr_gate_threshold(current_regime)
        contrib = getattr(signal, "contributing_strategies", None) or {}
        multi_strategy = len(contrib) >= 2
        if multi_strategy or signal.confidence >= 0.85:
            effective_atr_threshold = max(atr_threshold * 0.40, 0.20)
            atr_relax_tag = "multi_strat" if multi_strategy else "high_conf"
        elif signal.confidence >= 0.75:
            effective_atr_threshold = max(atr_threshold * 0.60, 0.20)
            atr_relax_tag = "high_conf"
        else:
            effective_atr_threshold = atr_threshold
            atr_relax_tag = ""
        if atr_pct is not None and effective_atr_threshold > 0:
            if atr_pct < effective_atr_threshold:
                relax_note = (
                    f" [relaxed:{atr_relax_tag}, base={atr_threshold:.2f}]"
                    if atr_relax_tag and effective_atr_threshold < atr_threshold
                    else ""
                )
                logger.info(
                    f"[ATR-GATE] Skipping {symbol}: ATR%={atr_pct:.2f} < "
                    f"min {effective_atr_threshold:.2f} (regime={current_regime}, too quiet){relax_note}"
                )
                self._audit_reject(
                    signal, current_price,
                    f"atr_gate:{atr_pct:.2f}<{effective_atr_threshold:.2f}@{current_regime}",
                )
                return

        # Pattern memory gate
        if self.trade_analyzer.enabled:
            adj, pat_reason = self.trade_analyzer.evaluate_setup(
                strategy=signal.strategy_name,
                hour_of_day=now.hour,
                day_of_week=now.weekday(),
                rsi=snap.get("rsi"),
                atr_pct=snap.get("atr_pct"),
                volume_ratio=snap.get("volume_ratio"),
                market_trend=self._market_context.get("nifty_trend"),
            )
            if adj < -0.1:
                logger.info(f"[PATTERN] Skipping {direction_label} {symbol}: {pat_reason} (adj={adj:+.3f})")
                self._audit_reject(signal, current_price, f"pattern:{pat_reason}")
                return

        # Stop-loss / take-profit (side-aware)
        atr = self._get_latest_atr(symbol)
        # Strategy-supplied SLs MUST be routed through `enforce_sl_floor` --
        # otherwise a sub-noise SL (e.g. supertrend's 3*ATR on a quiet stock)
        # bypasses the `min_stop_loss_pct` floor and the position gets
        # whipsawed out within minutes. 2026-05-13 HCLTECH bug: shorted at
        # 1142.35 with a 0.75% SL, stopped at 1150.40 in 30 min for -Rs 148.
        if signal.stop_loss:
            stop_loss = self.risk_manager.enforce_sl_floor(
                current_price, signal.stop_loss, side
            )
        else:
            stop_loss = self.risk_manager.get_stop_loss(current_price, side, atr)

        # Trend-continuation only applies to LONG path: _consec_tp_today is
        # direction-agnostic so we'd otherwise inflate short TPs for a
        # stock trending up with long TPs. Keep shorts conservative.
        trend_continuation = (side == "BUY") and (self._consec_tp_today.get(symbol, 0) >= 1)

        take_profit = signal.take_profit or self.risk_manager.get_take_profit(
            current_price, side, atr,
            regime=current_regime,
            trend_continuation=trend_continuation,
        )
        quantity = self.risk_manager.calculate_position_size(
            current_price, stop_loss, atr, side=side, regime=current_regime,
        )

        if trend_continuation:
            logger.info(
                f"[TREND-CONTINUATION] {symbol} had {self._consec_tp_today[symbol]} TPs today. "
                f"Widening TP to {take_profit:.2f} (4x ATR) and relying on trailing stop."
            )

        # Kelly-lite sizing multiplier
        leading_strategy = self._leading_contributor(signal)
        kelly_mult = 1.0
        if self.trade_analyzer.enabled and leading_strategy:
            kelly_mult = self.trade_analyzer.kelly_multiplier(leading_strategy)
            quantity = max(1, int(round(quantity * kelly_mult)))

        # Minimum-notional floor (Fix 1, 2026-04-30): round-trip commissions
        # eat ~Rs 40 per trade regardless of size. On sub-Rs 6k trades this is
        # a ~0.65 %+ break-even hurdle before any slippage.
        #
        # Cap-aware variant (2026-05-04): the floor is now clipped by the
        # symbol-exposure cap so we never scale a trade *into* a safety-gate
        # rejection. Yesterday's pattern was: scale 8 -> 11 to hit Rs 2.8k floor,
        # then sector/symbol concentration check rejects the bigger trade,
        # net effect = no trade taken at all. The fix:
        #   target_notional = min(min_trade_notional, symbol_cap_notional)
        # (with a small headroom so rounding doesn't push us past the cap).
        cap_constrained_floor = False
        effective_floor = self._min_trade_notional
        if self._min_trade_notional > 0 and quantity > 0:
            current_notional = current_price * quantity
            if current_notional < self._min_trade_notional:
                try:
                    total_equity_for_cap = self.portfolio.get_total_value(
                        {s: p.entry_price for s, p in self.portfolio.positions.items()}
                    )
                except Exception:
                    total_equity_for_cap = self.portfolio.cash + sum(
                        p.entry_price * p.quantity
                        for p in self.portfolio.positions.values()
                    )

                symbol_cap_notional = float("inf")
                if total_equity_for_cap > 0 and self._max_symbol_exposure_pct > 0:
                    headroom_pct = max(self._max_symbol_exposure_pct - 0.5, 0.5)
                    symbol_cap_notional = total_equity_for_cap * headroom_pct / 100

                target_notional = min(self._min_trade_notional, symbol_cap_notional)
                if target_notional < self._min_trade_notional:
                    cap_constrained_floor = True
                    effective_floor = target_notional

                if target_notional > current_notional:
                    target_qty = max(1, int(target_notional // current_price))
                    if target_qty > quantity:
                        logger.info(
                            f"[NOTIONAL-FLOOR] {symbol} notional Rs {current_notional:.0f} "
                            f"-> Rs {target_qty * current_price:.0f} "
                            f"(min Rs {self._min_trade_notional:.0f}, "
                            f"sym-cap Rs {symbol_cap_notional:.0f}, "
                            f"qty {quantity} -> {target_qty})"
                        )
                        quantity = target_qty

        # Cash-aware guard — applies to both sides because we lock notional
        # as collateral for shorts (see Portfolio.open_position for rationale).
        effective_price = current_price * 1.01
        max_affordable = int(self.portfolio.cash // effective_price) if effective_price > 0 else 0
        cash_reduced_qty = False
        if quantity > max_affordable:
            logger.info(
                f"[CASH-SIZE] Reducing {symbol} qty {quantity} -> {max_affordable} "
                f"(cash=₹{self.portfolio.cash:.0f}, px=₹{current_price:.2f})"
            )
            quantity = max_affordable
            cash_reduced_qty = True

        if quantity <= 0:
            self._audit_reject(signal, current_price, "sizing:zero_qty",
                               stop_loss=stop_loss, take_profit=take_profit)
            return

        # Final floor check. Skip only when we're meaningfully below the
        # *effective* floor (which is already cap-clamped). Two distinct
        # geometries produce sub-floor quantities and need different
        # tolerances:
        #
        #   1. Cap-constrained (cash plentiful, cap < raw min): the cap is
        #      the real ceiling and the floor is just commission-efficiency
        #      nice-to-have. Allow trades down to 70% of effective floor —
        #      otherwise on a Rs 10k book with a 30% per-symbol cap, every
        #      stock priced Rs 500-Rs 1500 gets blocked because we can't
        #      fit qty=N between cap (~Rs 2.8k) and 95% of floor (~Rs 2.7k).
        #      Today: 17 of 28 valid signals were rejected for exactly this.
        #
        #   2. Cash-constrained (cash ran out before we hit either floor or
        #      cap): commission drag is a real concern — keep tight 95%.
        if self._min_trade_notional > 0:
            final_notional = current_price * quantity
            # 70% tolerance ONLY when truly cap-constrained AND not subsequently
            # cash-trimmed; otherwise 95%.
            true_cap_constrained = cap_constrained_floor and not cash_reduced_qty
            tolerance = 0.70 if true_cap_constrained else 0.95
            skip_threshold = effective_floor * tolerance
            if final_notional < skip_threshold:
                reason_tag = (
                    "cap_constrained" if true_cap_constrained
                    else "cash_constrained"
                )
                logger.info(
                    f"[NOTIONAL-FLOOR] Skipping {direction_label} {symbol}: "
                    f"Rs {final_notional:.0f} < Rs {skip_threshold:.0f} "
                    f"(eff floor Rs {effective_floor:.0f}, raw min "
                    f"Rs {self._min_trade_notional:.0f}, {reason_tag}, "
                    f"tol={tolerance:.0%}, qty={quantity})"
                )
                self._audit_reject(
                    signal, current_price,
                    f"notional_floor:{final_notional:.0f}<{skip_threshold:.0f}:{reason_tag}",
                    stop_loss=stop_loss, take_profit=take_profit, quantity=quantity,
                )
                return

        # Expected-profit gate (side-aware, strategy-aware RR floor)
        product = self.portfolio.product_type
        worth, why = self.risk_manager.is_trade_worth_taking(
            entry_price=current_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            quantity=quantity,
            side=side,
            product=product,
            strategy=leading_strategy or signal.strategy_name,
        )
        if not worth:
            logger.info(
                f"[EXPECTED-PROFIT-GATE] Skipping {direction_label} {symbol}: {why} "
                f"(entry={current_price:.2f} TP={take_profit:.2f} SL={stop_loss:.2f} qty={quantity})"
            )
            self._audit_reject(signal, current_price, f"expected_profit:{why}",
                               stop_loss=stop_loss, take_profit=take_profit, quantity=quantity)
            return

        # Pre-trade safety: circuit + sector + single-symbol + window +
        # strategy-concurrency. Notional = price * qty regardless of side
        # (used for concentration). `strategy` enables the per-strategy
        # cap added 2026-05-14.
        cost_notional = current_price * quantity
        lead_strat_for_safety = leading_strategy or signal.strategy_name
        safe, reason = self._pre_trade_safety_checks(
            symbol, current_price, cost_notional, strategy=lead_strat_for_safety,
        )
        if not safe:
            logger.warning(
                f"[SAFETY-GATE] Skipping {direction_label} {symbol}: {reason} "
                f"(sector={get_sector(symbol)}, cost=Rs {cost_notional:,.0f})"
            )
            self._audit_reject(signal, current_price, f"safety_gate:{reason}",
                               stop_loss=stop_loss, take_profit=take_profit, quantity=quantity)
            return

        # Per-strategy circuit breaker — suspend a single strategy for the
        # day after consecutive-losses or daily-loss thresholds. Other
        # strategies remain free to trade.
        lead_strat = leading_strategy or signal.strategy_name
        suspended, sus_reason = self._strategy_is_suspended(lead_strat)
        if suspended:
            logger.warning(
                f"[STRATEGY-BREAKER] Skipping {direction_label} {symbol}: "
                f"{lead_strat} suspended ({sus_reason})"
            )
            self._audit_reject(signal, current_price, f"strategy_suspended:{sus_reason}",
                               stop_loss=stop_loss, take_profit=take_profit, quantity=quantity)
            return

        order = self.execution.place_order(
            symbol=symbol, token=token, transaction_type=direction_label,
            quantity=quantity, price=current_price,
            stop_loss=stop_loss, take_profit=take_profit,
            tag=leading_strategy or signal.strategy_name,
        )

        # Shadow mode — log intent only, no portfolio impact.
        if order and order.get("status") == "SHADOW":
            try:
                self.signal_audit.log(
                    symbol=symbol,
                    direction=direction_label,
                    confidence=signal.confidence,
                    regime=current_regime,
                    price=current_price,
                    strategy=leading_strategy or signal.strategy_name,
                    contributing=signal.contributing_strategies,
                    outcome="SHADOW",
                    reason=f"would_{direction_label.lower()} qty={quantity}",
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    quantity=quantity,
                )
            except Exception:
                pass
            return

        if order and order.get("status") in ("FILLED", "PARTIALLY_FILLED", "PLACED"):
            filled_price = order.get("filled_price") or current_price
            actual_qty = int(order.get("filled_quantity", quantity)) or quantity
            if actual_qty < quantity:
                logger.warning(
                    f"[PARTIAL-FILL] {symbol}: requested {quantity}, "
                    f"filled {actual_qty} — sizing position accordingly"
                )
            opened = self.portfolio.open_position(
                symbol=symbol, side=side, price=filled_price,
                quantity=actual_qty,
                strategy=leading_strategy or signal.strategy_name,
                stop_loss=stop_loss, take_profit=take_profit,
                order_id=order["order_id"],
                rsi=snap.get("rsi"),
                atr_pct=snap.get("atr_pct"),
                volume_ratio=snap.get("volume_ratio"),
                market_trend=self._market_context.get("nifty_trend"),
                regime=current_regime,
                contributing_strategies=signal.contributing_strategies or {},
            )

            # 2026-05-06: previously we ignored open_position's return value.
            # When the DB save failed (e.g. JSON-serialization bug, UNIQUE
            # constraint, or a future schema mismatch) the agent would still
            # create a trailing stop, send a "trade executed" alert, and log
            # [TRADE-OPEN] — leaving phantom risk-manager state and lying to
            # the user. Guard the post-trade actions behind the actual result.
            if not opened:
                logger.error(
                    f"[TRADE-OPEN-FAILED] {direction_label} {symbol} qty={actual_qty} "
                    f"@ {filled_price:.2f} — open_position returned False; "
                    f"skipping trailing-stop creation and alert. "
                    f"Order {order.get('order_id')} simulated as filled but "
                    f"position was NOT persisted. Cash unchanged."
                )
                try:
                    self.signal_audit.log(
                        symbol=symbol,
                        direction=direction_label,
                        confidence=signal.confidence,
                        regime=current_regime,
                        price=current_price,
                        strategy=leading_strategy or signal.strategy_name,
                        contributing=signal.contributing_strategies,
                        outcome="REJECTED",
                        reason="open_position_returned_false",
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        quantity=actual_qty,
                    )
                except Exception:
                    pass
                return

            ts = self.risk_manager.create_trailing_stop(symbol, filled_price, stop_loss, side)
            # P1 #15 (2026-05-17): snapshot immediately so a crash before
            # the first trail update still leaves the position recoverable.
            self._persist_trailing_states()
            if trend_continuation:
                ts.trail_activation_rr = 0.5
                ts.trail_step_pct = 0.6
                # 2026-05-18 regression fix: re-snapshot AFTER the override
                # so the tighter activation/step survives a restart. The
                # schema now carries these fields explicitly; without the
                # second write the on-disk snapshot kept the default
                # activation=1.0 / step=0.3 values for the lifetime of the
                # position.
                self._persist_trailing_states()
            # 2026-05-14: keep WS subscription tight to held symbols.
            self._resubscribe_ws_to_held()

            # Track for window-cap (max N opens per X-min rolling window).
            self._record_position_open(symbol)

            self.alert_manager.send_trade_alert(
                direction_label, symbol, quantity, filled_price,
                leading_strategy or signal.strategy_name,
            )
            logger.info(
                f"[TRADE-OPEN] {direction_label} {symbol} qty={quantity} @ {filled_price:.2f} "
                f"SL={stop_loss:.2f} TP={take_profit:.2f} regime={current_regime} "
                f"kelly={kelly_mult:.2f} trend_cont={trend_continuation} "
                f"contrib={signal.contributing_strategies}"
            )
            try:
                self.signal_audit.log(
                    symbol=symbol,
                    direction=direction_label,
                    confidence=signal.confidence,
                    regime=current_regime,
                    price=filled_price,
                    strategy=leading_strategy or signal.strategy_name,
                    contributing=signal.contributing_strategies,
                    outcome="ACCEPTED",
                    reason=f"filled_{order.get('status','?').lower()}",
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    quantity=actual_qty,
                )
            except Exception:
                pass

    def _maybe_recompute_carryover_sl(self, now: datetime) -> None:
        """Tighten the SL on overnight positions to break-even at most once
        per day (idempotent via `_carryover_sl_recomputed_date`).

        Logic:
          - Only fires when market is open and feature is enabled.
          - For each position whose entry_time was on a prior session,
            set SL to MAX(current_sl, entry_price) for LONGs or
            MIN(current_sl, entry_price) for SHORTs.
          - Refreshes the trailing stop's `current_sl` to match if a
            TrailingStop is registered.
          - If the position is already underwater, leave the SL alone
            (tightening to break-even on a losing trade just makes us
            stop out faster — defeats the point).
        """
        today = now.date()
        if not self.data_handler.is_market_open():
            return  # only at/after market open

        recomputed = 0
        for symbol, pos in self.portfolio.positions.items():
            try:
                entry_dt = pos.entry_time
                if entry_dt is None:
                    continue
                if getattr(entry_dt, "tzinfo", None) is None:
                    entry_dt = IST.localize(entry_dt)
                if entry_dt.date() >= today:
                    continue  # fresh intraday position, nothing to do
            except Exception:
                continue

            old_sl = float(pos.stop_loss)
            entry_px = float(pos.entry_price)
            if pos.side == "BUY":
                new_sl = max(old_sl, entry_px)
            else:  # SELL
                new_sl = min(old_sl, entry_px)

            if abs(new_sl - old_sl) < 0.01:
                continue

            pos.stop_loss = new_sl
            ts = self.risk_manager.get_trailing_stop(symbol)
            if ts is not None:
                if pos.side == "BUY":
                    ts.current_sl = max(ts.current_sl, new_sl)
                else:
                    ts.current_sl = min(ts.current_sl, new_sl)

            logger.info(
                f"[CARRYOVER-SL] {symbol} {pos.side}: SL {old_sl:.2f} -> "
                f"{new_sl:.2f} (break-even, entry={entry_px:.2f})"
            )
            recomputed += 1

        if recomputed > 0:
            logger.warning(
                f"[CARRYOVER-SL] tightened {recomputed} overnight positions "
                f"to break-even at market open"
            )
        self._carryover_sl_recomputed_date = today

    def _maybe_carryover_profit_lock(
        self, current_prices: Dict[str, Optional[float]]
    ) -> None:
        """Auto-close carryover positions at session end if they're profitable.

        Triggers once per day at `self._carryover_lock_time` (default 15:10
        IST). For each position whose entry_time is on a previous trading
        day AND whose unrealized PnL exceeds `_carryover_lock_min_profit`,
        close it via the standard exit path with reason
        `carryover_profit_lock`.

        Why: 2026-05-07 CROMPTON had +Rs 108 at 15:25 yesterday; we held it
        overnight expecting the SHORT thesis to play out and lost Rs 166
        when it gapped up. A pure session-end take-profit on profitable
        carryovers would have saved Rs 274 on this single trade. Tightly
        scoped to *carryovers in profit* — fresh intraday positions are
        managed normally (SL/TP/intraday flush at 15:15).
        """
        now = datetime.now(IST)
        # Reset the once-per-day flag if we've crossed a date boundary.
        today_date = now.date()
        if getattr(self, "_carryover_lock_done_date", None) != today_date:
            self._carryover_lock_done = False
            self._carryover_lock_done_date = today_date

        if getattr(self, "_carryover_lock_done", False):
            return
        if now.time() < getattr(self, "_carryover_lock_time", dtime(15, 10)):
            return

        candidates: list[tuple[str, float, float]] = []
        for symbol, pos in list(self.portfolio.positions.items()):
            try:
                entry_dt = pos.entry_time
                entry_date = entry_dt.date() if hasattr(entry_dt, "date") else None
            except Exception:
                entry_date = None
            if entry_date is None or entry_date >= today_date:
                continue
            price = current_prices.get(symbol)
            if price is None:
                continue
            unrealized = pos.unrealized_pnl(price)
            if unrealized <= getattr(self, "_carryover_lock_min_profit", 0.0):
                continue
            candidates.append((symbol, price, unrealized))

        if not candidates:
            self._carryover_lock_done = True
            return

        logger.info(
            f"[CARRYOVER-LOCK] Closing {len(candidates)} profitable carryover "
            f"position(s) at {now.strftime('%H:%M:%S')}"
        )
        for symbol, price, unrealized in candidates:
            pos = self.portfolio.positions.get(symbol)
            if pos is None:
                continue
            token = self._get_token(symbol)
            exit_side = "SELL" if pos.side == "BUY" else "BUY"
            # P0 #1 (2026-05-15): cancel broker SL BEFORE flatten via the safe helper.
            order, rec = self._close_position_safely(
                symbol=symbol, token=token, exit_side=exit_side,
                quantity=pos.quantity, price=price,
                tag="carryover_profit_lock", exit_reason="carryover_profit_lock",
            )
            if order and rec:
                filled = order.get("filled_price") or price
                self.alert_manager.send_alert(
                    "Exit: CARRYOVER_PROFIT_LOCK",
                    f"{pos.side} {pos.quantity}x{symbol} @ \u20B9{filled:.2f} | "
                    f"Locked PnL: \u20B9{rec.pnl:+.2f} (was carryover from "
                    f"{pos.entry_time.date()})",
                    level="info",
                )
        self._carryover_lock_done = True

    def _check_position_exits(self, current_prices: Dict[str, Optional[float]]):
        """Check SL/TP/trailing for all open positions.

        P0 #2 (2026-05-15) — LIVE-MODE SAFETY: this body is serialized
        across the main scan loop and the WebSocket background thread by
        `self._exit_check_lock`. A coarse single lock is fine for our
        position-book size (≤10 positions); fine-grained per-symbol
        locking would be overkill. The lock guards the *combined*
        check + flatten transaction, not just the check — so the second
        thread enters after the first has cleared `portfolio.positions`
        and finds nothing to do.
        """
        with self._exit_check_lock:
            self._check_position_exits_locked(current_prices)

    def _check_position_exits_locked(self, current_prices: Dict[str, Optional[float]]):
        """Locked body of `_check_position_exits`. Do not call directly —
        always enter through `_check_position_exits` so concurrent callers
        (WS thread + main loop) cannot race on the same symbol's exit."""
        to_close = []
        any_trail_mutation = False
        for symbol, pos in self.portfolio.positions.items():
            price = current_prices.get(symbol)
            if price is None:
                continue

            # Update trailing stop (also tracks peak-giveback state)
            trailing_sl = self.risk_manager.update_trailing_stop(symbol, price)
            if trailing_sl is not None:
                any_trail_mutation = True
            effective_sl = trailing_sl if trailing_sl else pos.stop_loss

            # 2026-05-14 LIVE-MODE SAFETY: propagate the trail SL to the
            # broker so the standing SL-M follows our in-memory trail. If
            # the daemon dies, the broker still enforces a tight SL instead
            # of the (much wider) original stop. Idempotent + cheap.
            if trailing_sl is not None and trailing_sl != pos.stop_loss:
                try:
                    self.execution.update_sl_trigger_for_symbol(symbol, float(trailing_sl))
                except Exception as e:
                    logger.debug(f"[SL-PROPAGATE] {symbol} update failed (non-fatal): {e}")

            trigger = self.risk_manager.check_stop_loss_take_profit(
                pos.entry_price, price, pos.side, effective_sl, pos.take_profit,
            )
            if trigger:
                to_close.append((symbol, price, trigger))
                continue

            # Peak-giveback: independent of price-trail. Catches the case
            # where a strong MFE reverts before the price-trail can lock
            # anything (e.g. today's MEESHO: peak +Rs 276, exited +Rs 71
            # via signal — peak-giveback would have exited around +Rs 180).
            ts = self.risk_manager.get_trailing_stop(symbol)
            if ts is not None and ts.should_peak_giveback_exit():
                logger.info(
                    f"[PEAK-GIVEBACK] {symbol} {pos.side} | "
                    f"peak_R={ts.peak_unrealized_r:.2f}  "
                    f"current_R={ts.last_unrealized_r:.2f}  "
                    f"giveback={(ts.peak_unrealized_r - ts.last_unrealized_r) / max(ts.peak_unrealized_r, 1e-9) * 100:.0f}%"
                )
                to_close.append((symbol, price, "peak_giveback"))

        for symbol, price, reason in to_close:
            pos = self.portfolio.positions.get(symbol)
            if pos is None:
                continue
            token = self._get_token(symbol)
            exit_side = "SELL" if pos.side == "BUY" else "BUY"

            # Reclassify the trigger if it was actually a trailing-stop hit.
            # `risk_manager.check_stop_loss_take_profit` returns "stop_loss"
            # for any SL breach — including trailing-stop hits that locked
            # in profit. The IDEA trade today closed with PnL=+Rs 20.80 but
            # the email subject said "Exit: STOP_LOSS", which is misleading.
            # `TrailingStop.trailing_active` flips True only after the
            # position moves 1R favorable, so it cleanly distinguishes a
            # real initial-SL stop-out from a profit-locking trailing exit.
            actual_reason = reason
            if reason == "stop_loss":
                ts = self.risk_manager.get_trailing_stop(symbol)
                if ts is not None and getattr(ts, "trailing_active", False):
                    actual_reason = "trailing_stop"

            # P0 #1 (2026-05-15): cancel broker SL BEFORE flatten via the safe helper.
            order, record = self._close_position_safely(
                symbol=symbol, token=token, exit_side=exit_side,
                quantity=pos.quantity, price=price,
                tag=f"auto_{actual_reason}", exit_reason=actual_reason,
            )
            if order and record:
                filled_price = order.get("filled_price") or price
                # Alert level driven by realised PnL, not the trigger
                # name — a trailing-stop locking in profit is GOOD news
                # and shouldn't be flagged as a warning.
                level = "warning" if record.pnl < 0 else "info"
                self.alert_manager.send_alert(
                    f"Exit: {actual_reason.upper()}",
                    f"{pos.side} {pos.quantity}x{symbol} @ \u20B9{filled_price:.2f} | "
                    f"PnL: \u20B9{record.pnl:+.2f}",
                    level=level,
                )

        # P1 #15 (2026-05-17): snapshot trailing-stop state if anything
        # mutated this cycle. Cheap (a few-symbol JSON write); the next
        # restart can rehydrate breakeven_armed / peak_giveback_armed /
        # highest_since_entry etc. instead of starting from defaults.
        if any_trail_mutation or to_close:
            self._persist_trailing_states()

    def _square_off_all(self, reason: str = "eod_square_off"):
        logger.info(f"Squaring off all positions: {reason}")
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions.get(symbol)
            if not pos:
                continue
            token = self._get_token(symbol)
            price = self.data_handler.get_ltp(symbol, token) or pos.entry_price
            exit_side = "SELL" if pos.side == "BUY" else "BUY"
            # P0 #1 (2026-05-15): cancel broker SL BEFORE flatten via the safe helper.
            self._close_position_safely(
                symbol=symbol, token=token, exit_side=exit_side,
                quantity=pos.quantity, price=price,
                tag=reason, exit_reason=reason,
            )

        self.alert_manager.send_alert(
            "Square Off Complete",
            f"All positions closed ({reason}). Day P&L: \u20B9{self.risk_manager.state.daily_pnl:+.2f}",
        )

    def _get_token(self, symbol: str) -> str:
        for inst in self.instruments:
            if inst["symbol"] == symbol:
                return inst.get("token", "")
        return ""

    def _get_latest_atr(self, symbol: str) -> Optional[float]:
        """Get ATR(14) for a symbol from recent data."""
        try:
            data = self.data_handler.get_historical_data(symbol, "5min",
                start_date=datetime.now(IST) - timedelta(hours=6),
                end_date=datetime.now(IST))
            if len(data) >= 14:
                import pandas as pd
                tr = pd.concat([
                    data["high"] - data["low"],
                    (data["high"] - data["close"].shift()).abs(),
                    (data["low"] - data["close"].shift()).abs(),
                ], axis=1).max(axis=1)
                return float(tr.rolling(14).mean().iloc[-1])
        except Exception:
            pass
        return None

    def _reset_single_shot_state_if_new_day(self) -> None:
        """Reset the per-symbol round-trip set at day boundary.

        Called from the entry path so the check is lazy and cheap; no
        scheduled callback needed. Safe to call repeatedly.
        """
        today = datetime.now(IST).date()
        if self._single_shot_day != today:
            if self._symbols_done_today:
                logger.info(
                    f"[SINGLE-SHOT] New trading day -- clearing "
                    f"{len(self._symbols_done_today)} symbols from done-list."
                )
            self._symbols_done_today.clear()
            self._single_shot_day = today

    def _on_trade_closed(self, record):
        """Called after every trade close — persists, learns, and updates weights."""
        self._store_trade_to_db(record)
        # 2026-05-14: tighten WS subscription to remaining held symbols.
        # Cheap no-op when WS is off or `ws_held_only` is false.
        try:
            self._resubscribe_ws_to_held()
        except Exception:
            pass

        # Single-shot mode: a trade close == full round-trip; record this
        # symbol as "done for today". Cheap no-op when the flag is off, so
        # we always update the set (keeps behaviour observable in tests).
        try:
            symbol = getattr(record, "symbol", None)
            if symbol:
                self._reset_single_shot_state_if_new_day()
                self._symbols_done_today.add(symbol)
                if self._single_shot_enabled:
                    logger.info(
                        f"[SINGLE-SHOT] {symbol} marked done for today "
                        f"(set size: {len(self._symbols_done_today)})"
                    )
        except Exception as e:
            logger.warning(f"single-shot tracking update failed: {e}")

        try:
            self.trade_analyzer.record_trade(record, market_context=self._market_context)
            if self.trade_analyzer.has_enough_data():
                learned = self.trade_analyzer.get_learned_weights()
                if learned:
                    self.ensemble.update_weights(learned)
                # Push regime-specific weights too so the ensemble can use them
                for regime_key in (
                    "bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol",
                    "sideways", "unknown",
                ):
                    rw = self.trade_analyzer.get_regime_weights(regime_key)
                    if rw:
                        self.ensemble.update_regime_weights(regime_key, rw)
        except Exception as e:
            logger.error(f"Trade analyzer error: {e}")

        # Update per-strategy circuit breaker state. Always best-effort —
        # any failure here must not poison the trade close path.
        try:
            self._update_strategy_breaker_state(record)
        except Exception as e:
            logger.warning(f"strategy breaker update failed: {e}")

        # 2026-05-14: feed cumulative realised P&L into the self-sufficiency
        # ledger. NET pnl already includes commissions (see Portfolio.close).
        try:
            pnl = float(getattr(record, "pnl", 0.0) or 0.0)
            self.self_sufficiency.record_realised_pnl(pnl)
        except Exception as e:
            logger.warning(f"self-sufficiency ledger update failed: {e}")

    def _update_strategy_breaker_state(self, record) -> None:
        """Maintain per-strategy consec-loss + daily-PnL counters and flip
        the `suspended` flag when thresholds are crossed.
        """
        strat = getattr(record, "strategy", None)
        if not strat:
            return
        st = self._strategy_state.setdefault(strat, {
            "consec_losses": 0, "daily_pnl": 0.0,
            "suspended": False, "suspended_reason": "", "trades": 0,
        })
        pnl = float(getattr(record, "pnl", 0.0) or 0.0)
        st["daily_pnl"] += pnl
        st["trades"] += 1
        if pnl < 0:
            st["consec_losses"] += 1
        else:
            st["consec_losses"] = 0

        # P2 restart-cluster (2026-05-17): persist BEFORE the suspended
        # early-return so even a strategy that just hit its 3rd consec
        # loss (about to suspend below) is captured on disk.
        self._persist_runtime_state()

        if st["suspended"]:
            return  # already suspended for the day

        # Threshold 1: too many consecutive losses
        if (self._strategy_max_consec_losses > 0
                and st["consec_losses"] >= self._strategy_max_consec_losses):
            st["suspended"] = True
            st["suspended_reason"] = f"consec_losses={st['consec_losses']}"
            logger.warning(
                f"[STRATEGY-BREAKER] {strat} suspended for the day "
                f"({st['consec_losses']} consecutive losses, "
                f"day_pnl=Rs {st['daily_pnl']:+.2f})"
            )
            # P2 restart-cluster: persist immediately after the suspended
            # transition so a restart in the next minute can't unsuspend.
            self._persist_runtime_state()
            return

        # Threshold 2: per-strategy daily PnL floor (% of initial capital)
        if self._strategy_daily_loss_pct > 0:
            base = float(self.config.get("capital", {}).get("initial_balance", 0.0))
            if base > 0:
                floor = -base * self._strategy_daily_loss_pct / 100.0
                if st["daily_pnl"] <= floor:
                    st["suspended"] = True
                    st["suspended_reason"] = (
                        f"daily_pnl=Rs {st['daily_pnl']:+.2f} <= floor Rs {floor:+.2f}"
                    )
                    self._persist_runtime_state()
                    logger.warning(
                        f"[STRATEGY-BREAKER] {strat} suspended for the day "
                        f"({st['suspended_reason']})"
                    )

    def _strategy_is_suspended(self, strategy_name: str) -> Tuple[bool, str]:
        """True iff the named strategy is breakered out for today."""
        st = self._strategy_state.get(strategy_name)
        if st and st.get("suspended"):
            return True, st.get("suspended_reason", "suspended")
        return False, ""

    def _store_trade_to_db(self, record):
        """Persist a completed trade to the database."""
        try:
            self.database.store_trade(record.to_dict())
        except Exception as e:
            logger.error(f"DB trade store failed: {e}")

    def _periodic_cleanup(self):
        """Purge old ticks and cap tick aggregator history to prevent memory leaks."""
        try:
            self.database.purge_old_ticks(days=7)
        except Exception as e:
            logger.error(f"Tick purge failed: {e}")

        # Cap in-memory candle history at 500 candles per symbol/interval
        max_history = 500
        for interval_hist in self.tick_aggregator._history.values():
            for symbol, candles in interval_hist.items():
                if len(candles) > max_history:
                    interval_hist[symbol] = candles[-max_history:]

    def _snapshot_equity(self):
        """Record current equity to database for curve tracking."""
        try:
            prices = {inst["symbol"]: self.data_handler.get_ltp(inst["symbol"], inst.get("token", ""))
                       for inst in self.instruments}
            equity = self.portfolio.get_total_value(prices)
            self.database.store_equity_point(equity, self.portfolio.cash, self.portfolio.open_position_count)
        except Exception as e:
            logger.error(f"Equity snapshot failed: {e}")

    def get_status(self) -> dict:
        prices = {}
        try:
            prices = self.data_handler.get_multiple_ltp(self.instruments)
        except Exception:
            pass
        return {
            "mode": self.execution.mode,
            "is_running": self._running,
            "cycle_count": self._cycle_count,
            "market_open": self.data_handler.is_market_open(),
            "portfolio": self.portfolio.get_summary(prices),
            "risk": self.risk_manager.get_risk_summary(),
            "strategies": [s.name for s in self.strategies],
            "instruments": [i["symbol"] for i in self.instruments],
            "auto_scan": self._auto_scan,
            "websocket": self._use_websocket,
            "ensemble_threshold": self.ensemble.confidence_threshold,
            "learning": {
                "enabled": self.trade_analyzer.enabled,
                "has_enough_data": self.trade_analyzer.has_enough_data(),
                "scorecard": self.trade_analyzer.get_scorecard(),
                "learned_weights": self.trade_analyzer.get_learned_weights(),
            },
            "timestamp": datetime.now(IST).isoformat(),
        }

    def _shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        self.ws_client.stop()
        self.tick_aggregator.flush_all()

        if self.portfolio.open_position_count > 0:
            self._square_off_all("shutdown")

        # Final report
        summary = self.portfolio.get_summary()
        risk = self.risk_manager.get_risk_summary()
        metrics = summary.get("metrics", {})

        logger.info("=" * 60)
        logger.info("SESSION SUMMARY")
        logger.info(f"  Portfolio:    \u20B9{summary['total_value']:,.2f}")
        logger.info(f"  Cash:         \u20B9{summary['cash']:,.2f}")
        logger.info(f"  Realized PnL: \u20B9{summary['realized_pnl']:+,.2f}")
        logger.info(f"  Day PnL:      \u20B9{risk['daily_pnl']:+,.2f}")
        logger.info(f"  Week PnL:     \u20B9{risk['weekly_pnl']:+,.2f}")
        logger.info(f"  Trades:       {risk['daily_trades']}")
        logger.info(f"  Drawdown:     {risk['drawdown_pct']:.2f}%")
        if metrics.get("total_trades", 0) > 0:
            logger.info(f"  Win Rate:     {metrics['win_rate']:.1f}%")
            logger.info(f"  Sharpe:       {metrics['sharpe_ratio']:.2f}")
            logger.info(f"  Profit Factor:{metrics['profit_factor']:.2f}")
        if self.trade_analyzer.enabled:
            scorecard = self.trade_analyzer.get_scorecard()
            if scorecard:
                logger.info("  --- Strategy Scorecard ---")
                for strat, sc in scorecard.items():
                    logger.info(
                        f"  {strat}: wr={sc.get('win_rate', 0):.1%} "
                        f"sharpe={sc.get('sharpe', 0):.2f} "
                        f"weight={sc.get('learned_weight', 1.0):.2f}"
                    )
        logger.info("=" * 60)

        # 2026-05-04: Skip the daily report email if EOD summary was
        # already sent via `_maybe_send_eod_summary` earlier this session.
        # Otherwise, daemon shutdown around the intraday-close window
        # produces a third near-identical email (verified in today's run:
        # 15:30:29 Daily Report after the 15:20 EOD Summary).
        if not self._eod_summary_sent:
            self.alert_manager.send_daily_report(summary, risk)
        else:
            logger.info("Skipping shutdown Daily Report — EOD summary already sent today.")
        logger.info("Agent stopped.")
