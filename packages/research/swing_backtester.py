"""Strategy-agnostic swing backtest engine (Path B, charter v4 §3).

Extracts the V27 standalone backtest loop (``tools/v27_backtest_2026_06_01.py``)
into a reusable engine driven by a :class:`StrategySpec`. Charter v4 §3
mandates vol-target sizing + risk-parity allocation + portfolio-level
concurrency cap; this engine bakes those in so every Mode A swing strategy
produced through it is charter-compliant by construction.

Why this exists
----------------
Until now V27–V34 lived inside ``tools/v27_backtest_2026_06_01.py`` as a
single-strategy script. Scaling to multiple swing strategies (V35–V39 =
mean-reversion, SMA50-pullback, weekly-breakout, MACD-swing, dual-momentum
relative-strength) without copy-pasting the 600-line loop required pulling
the strategy-specific bits (entry signal, exit signal, trailing-stop state)
out behind a small interface and keeping everything else — universe, sizing,
allocation, charges, equity curve, metrics, NIFTYBEES benchmark, artifact
writers — in one place.

Engine A vs Engine B (see ``docs/changes/changes_done_2026-06-01.md`` Phase 13):
    Engine A = legacy ``packages/research/backtest_ensemble.py`` + ``battery.py``
               (drives V1–V26: ensemble voting, v2.1 fixed-fraction sizing).
    Engine B = this file + ``tools/multi_swing_backtest_2026_06_01.py``
               (drives V27 standalone equivalent + V35+ multi-strategy:
               vol-target sizing + risk-parity allocator + 75-instrument
               cross-asset universe).

Charter compliance is the dividing line. Engine A cannot host V27+ variants
without being rewritten to satisfy charter v4 §3.3/§3.5/§3.6 — Engine B
already does.

Sanity guarantee
----------------
This engine MUST reproduce V32 (Donchian-55/20, max_concurrent=6) within
±0.1% CAGR / ±0.01 PF when fed the equivalent ``StrategySpec``. That check
runs as part of ``tools/multi_swing_backtest_2026_06_01.py --sanity-check``.
If the sanity check fails the engine is wrong, not V32.

Interface contract (StrategySpec)
---------------------------------
A strategy module exports a single ``SPEC: StrategySpec`` constant. The
engine never imports anything from the strategy module other than that
constant. The strategy is responsible for:

    * Computing entry signals from a (slice of) OHLCV history.
    * Computing exit signals from a position + (slice of) OHLCV history.
    * Optionally maintaining per-position state across bars (e.g. trailing
      stops' high-water marks). The engine handles ALL portfolio-level
      bookkeeping — cash, charges, allocation, sizing.

The engine intentionally does NOT expose the portfolio book to the strategy.
Strategies are stateless across symbols; their only state is per-position
and confined to ``OpenPosition.state``.
"""
from __future__ import annotations

import io
import json
import math
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

# Engine B is part of the research pod. It is allowed to import from
# core.* (signals, charges, instruments) per pod-boundary contract, but
# NOT from strategies.*. Strategy modules are passed in via StrategySpec
# (dependency injection), so this file never grows a hard dep on any
# particular strategy.
from core import charges as charges_mod
from core.instruments.sector_classifier import sector_for
from core.signals import risk_parity, volatility_sizer


CHARTER_PATH = "docs/reviews/strategy_charter_v4_2026-06-01.md"


# ============================================================
# Core data structures
# ============================================================

@dataclass
class OpenPosition:
    """Live position in the backtest's portfolio book.

    ``state`` holds strategy-specific per-position bookkeeping (e.g. a
    chandelier-stop high-water mark for Donchian, a max-favourable-excursion
    for MACD-swing). Strategies mutate this dict via ``on_bar_fn``; the
    engine never inspects its contents.

    ``initial_stop`` is the strategy's first-bar protective stop, used both
    for the risk-budget unit in ``vol_target_size`` (when the spec provides
    one) and for charter §3.10 verdict-tree reporting. If the spec does NOT
    provide an initial_stop_fn, the engine falls back to ATR(14) as the
    risk unit.
    """
    symbol: str
    entry_date: pd.Timestamp
    entry_bar_index: int          # position in the SYMBOL's df
    entry_price: float
    shares: int
    initial_stop: float
    entry_charges_inr: float
    state: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClosedTrade:
    """A round-tripped trade — what gets written to ``trades.csv``."""
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    bars_held: int
    pnl_gross_inr: float
    charges_inr: float           # entry + exit charges combined
    pnl_net_inr: float
    exit_reason: str


# ============================================================
# Strategy interface
# ============================================================

# entry_fn(df_today, params, last_entry_bar_index, context) -> (fires, diag)
# ``context`` is an engine-supplied per-bar dict; carries ``today`` and
# ``universe_signal`` (the cached output of ``universe_signals_fn``, if any).
# Strategies that don't need cross-sectional info just ignore context.
EntryFn = Callable[
    [pd.DataFrame, Dict[str, Any], Optional[int], Dict[str, Any]],
    Tuple[bool, Dict[str, Any]],
]

# exit_fn(df_today, position, params) -> exit_reason | None
ExitFn = Callable[[pd.DataFrame, "OpenPosition", Dict[str, Any]], Optional[str]]

# initial_state_fn(df_at_entry, params) -> initial state dict
InitialStateFn = Callable[[pd.DataFrame, Dict[str, Any]], Dict[str, Any]]

# initial_stop_fn(df_at_entry, params) -> float (price-level stop)
InitialStopFn = Callable[[pd.DataFrame, Dict[str, Any]], float]

# on_bar_fn(position, df_today, params) -> None (mutates position.state)
OnBarFn = Callable[["OpenPosition", pd.DataFrame, Dict[str, Any]], None]

# universe_signals_fn(history, today, params) -> any (cached per-bar).
# Called ONCE per bar BEFORE entry candidate gathering. The return value is
# stashed into the engine's per-bar context and passed to every entry_fn
# invocation that bar via ``context["universe_signal"]``. Used by
# cross-sectional strategies (dual-momentum relative-strength, sector
# rotation, low-vol decile, etc.) where the entry rule depends on a
# RANK across the whole universe rather than a per-symbol indicator.
UniverseSignalsFn = Callable[
    [Dict[str, pd.DataFrame], pd.Timestamp, Dict[str, Any]],
    Any,
]


@dataclass
class StrategySpec:
    """The contract every Mode A swing strategy fulfils for this engine.

    The 5-strategy multi-swing scale-up (V35–V39, 2026-06-01) was the
    forcing function for this interface — see
    ``docs/findings/multi_swing_v35_v39_results_2026-06-01.md``.

    Attributes:
        name: Short snake_case identifier, e.g. ``"mean_reversion_swing_v1"``.
            Used as the manifest variant name and the output subdirectory.
        description: One-line human description for the comparison report.
        required_warmup_bars: Minimum history bars before entry/exit can be
            evaluated. The engine skips ``date_idx < required_warmup_bars``.
            Set to ``max(longest indicator lookback) + a small guard``.
        entry_fn: Callable; takes the symbol's OHLCV history sliced
            ``[..., today_inclusive]``, the strategy params, and the bar
            index of the LAST entry in THIS symbol (or None). Returns
            ``(fires: bool, diag: dict)``. Diag is logged but not currently
            persisted (Phase 13 deferred to a follow-up).
        exit_fn: Callable; takes the symbol's history slice, the open
            position (read-only), and params. Returns the exit-reason
            string (e.g. ``"donchian_exit"``, ``"rsi_overbought"``) or
            ``None`` if the position should stay open.
        initial_state_fn: Optional. Computes the per-position state at
            entry. If None, the engine seeds ``position.state = {}``.
        initial_stop_fn: Optional. Computes the initial protective stop
            price. If None, the engine uses ATR(14) as the risk unit for
            ``vol_target_size`` (matching V27 Donchian behaviour).
        on_bar_fn: Optional. Called each bar with the open position and
            the latest history slice. Mutates ``position.state`` in place
            (e.g. updates a chandelier high-water mark). The engine never
            reads what on_bar_fn writes — it's strategy-private.
        default_params: Dict of strategy-tunable parameters. CLI overrides
            and engine-level params (max_concurrent, sector_cap, etc.) are
            applied separately and NEVER mutate this dict.
        cost_product: Always "DELIVERY" for Mode A (charter §3.1: CNC swing
            cash). Engine passes this through to ``core.charges.compute_one_leg``.
            Future Mode B (F&O paper) will use "INTRADAY" — keeping the
            knob means the engine doesn't need a fork for that path.
    """
    name: str
    description: str
    required_warmup_bars: int
    entry_fn: EntryFn
    exit_fn: ExitFn
    initial_state_fn: Optional[InitialStateFn] = None
    initial_stop_fn: Optional[InitialStopFn] = None
    on_bar_fn: Optional[OnBarFn] = None
    universe_signals_fn: Optional[UniverseSignalsFn] = None
    default_params: Dict[str, Any] = field(default_factory=dict)
    cost_product: str = "DELIVERY"


# ============================================================
# Engine parameters (portfolio-level, charter §3 + §3.6)
# ============================================================

@dataclass
class EngineParams:
    """Engine-level (NOT strategy-level) parameters.

    Mirrors V27Params for the portfolio-shared fields. Strategy-specific
    knobs live in ``StrategySpec.default_params`` and are merged with
    operator overrides BEFORE being passed to entry_fn/exit_fn.
    """
    max_concurrent_positions: int = 6
    sector_cap: Optional[int] = None
    risk_per_trade_pct: float = volatility_sizer.DEFAULT_RISK_PCT       # 0.5%
    max_position_pct: float = volatility_sizer.DEFAULT_MAX_POSITION_PCT  # 8%
    # Allocator volatility window. Charter §3.5 prescribes 20-day daily
    # return std for risk-parity weights; matches V27.
    sigma_window_bars: int = 20


# ============================================================
# Engine entrypoint
# ============================================================

def run_swing_backtest(
    spec: StrategySpec,
    *,
    history: Dict[str, pd.DataFrame],
    capital_inr: float,
    start: str,
    end: str,
    engine_params: Optional[EngineParams] = None,
    strategy_params_override: Optional[Dict[str, Any]] = None,
    output_dir: Optional[Path] = None,
    excluded_from_signals: Optional[set[str]] = None,
    write_artifacts: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run ONE strategy against the supplied pre-fetched history.

    The multi-strategy runner fetches the universe ONCE and passes the
    same history dict to N invocations of this function. That's the
    primary reason ``history`` is an argument — refetching per strategy
    would 5× the yfinance traffic and break apples-to-apples.

    Args:
        spec: StrategySpec from a strategy module's ``SPEC`` constant.
        history: Pre-fetched ``{symbol: DataFrame[open,high,low,close,volume]}``
            keyed by clean symbol (no ``.NS`` suffix). MUST include
            NIFTYBEES if a benchmark comparison is wanted.
        capital_inr: Initial capital. Charter §3.9 default is 100,000.
        start, end: Display strings YYYY-MM-DD. The actual date range is
            derived from the intersection of ``history``'s indices, so
            these are reported in the manifest but don't gate the loop.
        engine_params: Portfolio-level config. Defaults to EngineParams().
        strategy_params_override: Operator/CLI overrides merged ON TOP of
            ``spec.default_params``. Spec defaults are NOT mutated.
        output_dir: Where to write artifacts. None = compute equity_curve +
            trades + metrics but don't write to disk.
        excluded_from_signals: Symbols kept in ``history`` (for benchmark
            + sigma references) but excluded from the candidate set —
            e.g. NIFTYBEES for self-cannibalisation sensitivity tests.
        write_artifacts: If False, returns the in-memory result only.
            Used by the multi-strategy runner's sanity check.
        verbose: If False, suppresses per-symbol fetch + per-bar progress
            prints. The runner enables it for the active strategy and
            disables it for the sanity-check run.

    Returns:
        Dict with keys: ``metrics``, ``benchmark``, ``equity_curve``,
        ``trades``, ``n_trades``, ``elapsed_sec``, ``output_dir`` (or
        None), ``manifest``.
    """
    eng = engine_params or EngineParams()
    strat_params: Dict[str, Any] = {**spec.default_params, **(strategy_params_override or {})}
    excluded_from_signals = excluded_from_signals or set()

    if output_dir is not None and write_artifacts:
        output_dir.mkdir(parents=True, exist_ok=True)

    if not history:
        raise ValueError("history is empty; supply at least one symbol's OHLCV")

    # ── Build master date index from intersection of fetched symbols.
    all_dates = sorted({d for df in history.values() for d in df.index})
    if not all_dates:
        raise ValueError("history contains no dates")

    warmup_bars = spec.required_warmup_bars
    if verbose:
        print(f"[{spec.name}] dates: {all_dates[0]} → {all_dates[-1]}  "
              f"({len(all_dates)} bars, warmup {warmup_bars})")

    # ── State.
    cash = float(capital_inr)
    open_positions: Dict[str, OpenPosition] = {}
    closed_trades: List[ClosedTrade] = []
    last_entry_idx: Dict[str, int] = {}
    equity_curve: List[dict] = []

    t0 = time.time()
    if verbose:
        loop_bars = max(0, len(all_dates) - warmup_bars)
        print(f"[{spec.name}] running loop over {loop_bars} bars ...")

    for date_idx, today in enumerate(all_dates):
        if date_idx < warmup_bars:
            continue

        # ── (1) Check exits + on_bar updates for all open positions.
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df = history[sym]
            if today not in df.index:
                # Symbol skipped today's session (e.g. unscheduled holiday);
                # carry the position into the next bar unchanged.
                continue
            today_pos = df.index.get_loc(today)
            df_today = df.iloc[: today_pos + 1]

            # Strategy-private per-bar state update FIRST (e.g. trailing
            # stop high-water mark) so exit_fn sees the latest state.
            if spec.on_bar_fn is not None:
                spec.on_bar_fn(pos, df_today, strat_params)

            exit_reason = spec.exit_fn(df_today, pos, strat_params)

            if exit_reason is not None:
                exit_price = float(df_today["close"].iloc[-1])
                exit_charges = float(charges_mod.compute_one_leg(
                    price=exit_price, quantity=pos.shares,
                    side="SELL", product=spec.cost_product,
                ))
                gross_pnl = (exit_price - pos.entry_price) * pos.shares
                net_pnl = gross_pnl - pos.entry_charges_inr - exit_charges
                cash += pos.shares * exit_price - exit_charges
                closed_trades.append(ClosedTrade(
                    symbol=sym,
                    entry_date=pos.entry_date,
                    exit_date=today,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    shares=pos.shares,
                    bars_held=today_pos - pos.entry_bar_index,
                    pnl_gross_inr=gross_pnl,
                    charges_inr=pos.entry_charges_inr + exit_charges,
                    pnl_net_inr=net_pnl,
                    exit_reason=exit_reason,
                ))
                del open_positions[sym]

        # ── (2) Gather entry candidates.
        if len(open_positions) < eng.max_concurrent_positions:
            # Cross-sectional pre-pass (charter §3.5 risk-parity does its
            # own variance-driven selection at allocation time; this hook
            # is for strategy-level cross-sectional rules like dual-momentum
            # relative-strength's top-decile rank).
            if spec.universe_signals_fn is not None:
                try:
                    universe_signal = spec.universe_signals_fn(history, today, strat_params)
                except Exception as exc:  # noqa: BLE001
                    if verbose:
                        print(f"[{spec.name}] universe_signals_fn raised "
                              f"{type(exc).__name__}: {exc}; treating as empty signal")
                    universe_signal = None
            else:
                universe_signal = None

            context: Dict[str, Any] = {
                "today": today,
                "universe_signal": universe_signal,
            }

            candidates: List[Tuple[str, pd.DataFrame, dict]] = []
            for sym, df in history.items():
                if sym in open_positions:
                    continue
                if sym in excluded_from_signals:
                    continue
                if today not in df.index:
                    continue
                today_pos = df.index.get_loc(today)
                df_today = df.iloc[: today_pos + 1]
                if len(df_today) < warmup_bars:
                    continue
                last_entry = last_entry_idx.get(sym)
                # Per-symbol context augmentation — strategies that want
                # to know the candidate symbol can read context["symbol"].
                context["symbol"] = sym
                fires, diag = spec.entry_fn(df_today, strat_params, last_entry, context)
                if fires:
                    candidates.append((sym, df_today, diag))

            # ── (3) Risk-parity allocation across surviving candidates.
            if candidates:
                slots_available = eng.max_concurrent_positions - len(open_positions)

                # If more candidates than slots, take the LOWEST-vol ones
                # (charter §3.5: equal-risk preference for low-vol names).
                if len(candidates) > slots_available:
                    candidates_with_sigma = [
                        (sym, df, diag, risk_parity.daily_return_std(df, window=eng.sigma_window_bars))
                        for sym, df, diag in candidates
                    ]
                    candidates_with_sigma = [
                        c for c in candidates_with_sigma
                        if math.isfinite(c[3]) and c[3] > 0
                    ]
                    candidates_with_sigma.sort(key=lambda x: x[3])
                    candidates = [(s, d, dg) for s, d, dg, _ in candidates_with_sigma[:slots_available]]

                sigmas = {
                    sym: risk_parity.daily_return_std(df, window=eng.sigma_window_bars)
                    for sym, df, _ in candidates
                }
                alloc = risk_parity.allocate(
                    cash, sigmas, max_per_name_pct=eng.max_position_pct,
                )

                # Sector cap (charter §3.6). None = OFF.
                sector_counts: Counter = Counter()
                if eng.sector_cap is not None:
                    sector_counts = Counter(sector_for(s) for s in open_positions.keys())

                for sym, df_today, _diag in candidates:
                    if sym not in alloc or alloc[sym] <= 0:
                        continue
                    sec = sector_for(sym) if eng.sector_cap is not None else None
                    if sec is not None and sector_counts[sec] >= eng.sector_cap:
                        continue

                    price_today = float(df_today["close"].iloc[-1])

                    # ATR(14) as the risk unit when the spec doesn't provide
                    # an explicit initial stop. Matches V27's behaviour.
                    atr_val = _atr_14_for_sizing(df_today)

                    # vol-target sizing: equity-aware risk budget, then
                    # capped by the allocator's per-name INR ceiling.
                    portfolio_equity = cash + sum(
                        p.shares * float(history[s]["close"].asof(today))
                        for s, p in open_positions.items()
                        if today in history[s].index
                    )
                    sizing = volatility_sizer.vol_target_size(
                        equity_inr=portfolio_equity,
                        price_inr=price_today,
                        atr_14_inr_per_share=atr_val,
                        risk_pct=eng.risk_per_trade_pct,
                        max_position_pct=eng.max_position_pct,
                        lot_size=1,
                    )
                    if sizing.shares == 0:
                        continue

                    shares_alloc_cap = int(alloc[sym] / price_today)
                    final_shares = min(sizing.shares, shares_alloc_cap)
                    if final_shares < 1:
                        continue

                    notional = final_shares * price_today
                    if notional > cash:
                        # Another candidate consumed cash this bar; skip
                        # rather than partial-fill (matches V27).
                        continue

                    entry_charges = float(charges_mod.compute_one_leg(
                        price=price_today, quantity=final_shares,
                        side="BUY", product=spec.cost_product,
                    ))

                    # Strategy-provided initial stop (or ATR-anchored fallback).
                    if spec.initial_stop_fn is not None:
                        try:
                            initial_stop = float(spec.initial_stop_fn(df_today, strat_params))
                        except Exception:  # noqa: BLE001
                            initial_stop = (
                                price_today - 3.0 * atr_val
                                if math.isfinite(atr_val) and atr_val > 0
                                else price_today * 0.92
                            )
                    else:
                        initial_stop = (
                            price_today - 3.0 * atr_val
                            if math.isfinite(atr_val) and atr_val > 0
                            else price_today * 0.92
                        )

                    # Strategy-private initial state.
                    if spec.initial_state_fn is not None:
                        try:
                            state = spec.initial_state_fn(df_today, strat_params)
                        except Exception:  # noqa: BLE001
                            state = {}
                    else:
                        state = {}

                    cash -= notional + entry_charges

                    open_positions[sym] = OpenPosition(
                        symbol=sym,
                        entry_date=today,
                        entry_bar_index=df_today.index.get_loc(today),
                        entry_price=price_today,
                        shares=final_shares,
                        initial_stop=initial_stop,
                        entry_charges_inr=entry_charges,
                        state=state,
                    )
                    last_entry_idx[sym] = df_today.index.get_loc(today)
                    if sec is not None:
                        sector_counts[sec] += 1

                    if len(open_positions) >= eng.max_concurrent_positions:
                        break

        # ── (4) Mark-to-market equity at EOD.
        mtm_value = 0.0
        for sym, pos in open_positions.items():
            if today in history[sym].index:
                mtm_value += pos.shares * float(history[sym]["close"].asof(today))
            else:
                mtm_value += pos.shares * pos.entry_price
        equity = cash + mtm_value
        equity_curve.append({
            "date": today,
            "cash": round(cash, 2),
            "mtm_value": round(mtm_value, 2),
            "equity": round(equity, 2),
            "open_positions": len(open_positions),
        })

    elapsed = time.time() - t0
    if verbose:
        print(f"[{spec.name}] loop done in {elapsed:.1f}s. "
              f"closed_trades={len(closed_trades)}  still_open={len(open_positions)}")

    # ── Force-close remaining positions at the final bar's close.
    final_date = all_dates[-1]
    for sym, pos in list(open_positions.items()):
        if final_date not in history[sym].index:
            continue
        final_close = float(history[sym]["close"].asof(final_date))
        exit_charges = float(charges_mod.compute_one_leg(
            price=final_close, quantity=pos.shares,
            side="SELL", product=spec.cost_product,
        ))
        gross_pnl = (final_close - pos.entry_price) * pos.shares
        net_pnl = gross_pnl - pos.entry_charges_inr - exit_charges
        cash += pos.shares * final_close - exit_charges
        closed_trades.append(ClosedTrade(
            symbol=sym, entry_date=pos.entry_date, exit_date=final_date,
            entry_price=pos.entry_price, exit_price=final_close,
            shares=pos.shares,
            bars_held=(all_dates.index(final_date) - pos.entry_bar_index),
            pnl_gross_inr=gross_pnl,
            charges_inr=pos.entry_charges_inr + exit_charges,
            pnl_net_inr=net_pnl,
            exit_reason="end_of_window_close_out",
        ))
        del open_positions[sym]

    metrics = _compute_metrics(
        equity_curve, closed_trades,
        initial_capital=capital_inr,
        start=all_dates[warmup_bars],
        end=all_dates[-1],
    )
    benchmark = _compute_niftybees_benchmark(
        history, initial_capital=capital_inr,
        start_date=all_dates[warmup_bars], end_date=all_dates[-1],
    )

    manifest = _build_manifest(
        spec=spec, eng=eng, strat_params=strat_params,
        capital_inr=capital_inr,
        start=all_dates[warmup_bars], end=all_dates[-1],
        excluded_from_signals=excluded_from_signals,
    )

    if write_artifacts and output_dir is not None:
        _write_artifacts(
            output_dir=output_dir,
            spec=spec, manifest=manifest,
            equity_curve=equity_curve, trades=closed_trades,
            metrics=metrics, benchmark=benchmark,
        )

    return {
        "metrics": metrics,
        "benchmark": benchmark,
        "equity_curve": equity_curve,
        "trades": closed_trades,
        "n_trades": len(closed_trades),
        "elapsed_sec": elapsed,
        "output_dir": str(output_dir) if output_dir else None,
        "manifest": manifest,
    }


# ============================================================
# Helpers — kept module-private so the strategy modules can't
# accidentally depend on them.
# ============================================================

def _atr_14_for_sizing(df: pd.DataFrame, period: int = 14) -> float:
    """ATR(14) as a price-units scalar, EWM-smoothed (matches V27).

    Decoupled from ``core.signals.donchian._atr_ewm`` so the engine doesn't
    pull a strategy-pod helper for portfolio-level sizing. Computed identically.
    """
    if len(df) < period + 1:
        return float("nan")
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else float("nan")


def _compute_metrics(
    equity_curve: List[dict],
    trades: List[ClosedTrade],
    *,
    initial_capital: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict:
    if not equity_curve:
        return {"error": "no_equity_curve"}

    eq = pd.DataFrame(equity_curve).set_index("date")["equity"]
    final_equity = float(eq.iloc[-1])
    total_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0
    years = (end - start).days / 365.25
    cagr_pct = (
        (((final_equity / initial_capital) ** (1.0 / years)) - 1.0) * 100.0
        if years > 0 else 0.0
    )
    running_max = eq.cummax()
    drawdown = (eq / running_max - 1.0) * 100.0
    max_dd_pct = float(drawdown.min())

    if trades:
        wins = [t for t in trades if t.pnl_net_inr > 0]
        losses = [t for t in trades if t.pnl_net_inr < 0]
        gross_profit = sum(t.pnl_net_inr for t in wins)
        gross_loss = abs(sum(t.pnl_net_inr for t in losses))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        win_rate = (len(wins) / len(trades)) * 100.0
        avg_charges = sum(t.charges_inr for t in trades) / len(trades)
        total_charges = sum(t.charges_inr for t in trades)
    else:
        pf = 0.0
        win_rate = 0.0
        gross_profit = gross_loss = 0.0
        avg_charges = total_charges = 0.0

    return {
        "start": str(start.date()),
        "end": str(end.date()),
        "years": round(years, 2),
        "initial_capital_inr": initial_capital,
        "final_equity_inr": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "n_trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(pf, 2) if math.isfinite(pf) else None,
        "gross_profit_inr": round(gross_profit, 2),
        "gross_loss_inr": round(gross_loss, 2),
        "avg_charges_per_trade_inr": round(avg_charges, 2),
        "total_charges_inr": round(total_charges, 2),
    }


def _compute_niftybees_benchmark(
    history: Dict[str, pd.DataFrame],
    *,
    initial_capital: float,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> dict:
    nb = history.get("NIFTYBEES")
    if nb is None or nb.empty:
        return {"error": "no_niftybees_data"}
    nb_slice = nb[(nb.index >= start_date) & (nb.index <= end_date)]
    if nb_slice.empty:
        return {"error": "empty_slice"}
    entry_price = float(nb_slice["close"].iloc[0])
    exit_price = float(nb_slice["close"].iloc[-1])
    shares = int(initial_capital / entry_price)
    final_equity = shares * exit_price + (initial_capital - shares * entry_price)
    total_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0
    years = (end_date - start_date).days / 365.25
    cagr_pct = (
        ((final_equity / initial_capital) ** (1.0 / years) - 1.0) * 100.0
        if years > 0 else 0.0
    )
    eq = nb_slice["close"] * shares + (initial_capital - shares * entry_price)
    running_max = eq.cummax()
    dd = (eq / running_max - 1.0) * 100.0
    max_dd_pct = float(dd.min())

    return {
        "instrument": "NIFTYBEES",
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "final_equity_inr": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "max_dd_pct": round(max_dd_pct, 2),
    }


def _build_manifest(
    *,
    spec: StrategySpec,
    eng: EngineParams,
    strat_params: Dict[str, Any],
    capital_inr: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    excluded_from_signals: set[str],
) -> dict:
    return {
        "variant": spec.name,
        "description": spec.description,
        "engine": "swing_backtester (Engine B)",
        "engine_file": "packages/research/swing_backtester.py",
        "charter_version": "v4.0",
        "charter_path": CHARTER_PATH,
        "universe_file": "data/v4_universe_swing_cash.txt",
        "excluded_from_signals": sorted(excluded_from_signals),
        "engine_params": {
            "max_concurrent_positions": eng.max_concurrent_positions,
            "sector_cap": eng.sector_cap,
            "risk_per_trade_pct": eng.risk_per_trade_pct,
            "max_position_pct": eng.max_position_pct,
            "sigma_window_bars": eng.sigma_window_bars,
        },
        "strategy_params": strat_params,
        "cost_model": f"CashCNCCharges:angelone:{datetime.now().date()}",
        "cost_product": spec.cost_product,
        "window_start": str(start.date()),
        "window_end": str(end.date()),
        "initial_capital_inr": capital_inr,
        "required_warmup_bars": spec.required_warmup_bars,
    }


def _write_artifacts(
    *,
    output_dir: Path,
    spec: StrategySpec,
    manifest: dict,
    equity_curve: List[dict],
    trades: List[ClosedTrade],
    metrics: dict,
    benchmark: dict,
) -> None:
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8",
    )
    pd.DataFrame(equity_curve).to_csv(output_dir / "equity_curve.csv", index=False)
    if trades:
        pd.DataFrame([{
            "symbol": t.symbol,
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "shares": t.shares,
            "bars_held": t.bars_held,
            "pnl_gross_inr": round(t.pnl_gross_inr, 2),
            "charges_inr": round(t.charges_inr, 2),
            "pnl_net_inr": round(t.pnl_net_inr, 2),
            "exit_reason": t.exit_reason,
        } for t in trades]).to_csv(output_dir / "trades.csv", index=False)
    else:
        (output_dir / "trades.csv").write_text(
            "symbol,entry_date,exit_date,entry_price,exit_price,shares,"
            "bars_held,pnl_gross_inr,charges_inr,pnl_net_inr,exit_reason\n",
            encoding="utf-8",
        )
    (output_dir / "results.json").write_text(
        json.dumps({"metrics": metrics, "benchmark": benchmark}, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "comparison.md").write_text(
        _render_comparison_md(spec, manifest, metrics, benchmark, trades),
        encoding="utf-8",
    )


def _render_comparison_md(
    spec: StrategySpec,
    manifest: dict,
    metrics: dict,
    benchmark: dict,
    trades: List[ClosedTrade],
) -> str:
    out: List[str] = []
    out.append(f"# {spec.name} — backtest comparison")
    out.append("")
    out.append(f"> **Variant:** `{spec.name}`  ")
    out.append(f"> **Strategy:** {spec.description}  ")
    out.append(f"> **Engine:** `{manifest['engine']}`  ")
    out.append(f"> **Charter:** [{manifest['charter_path']}]({'../../../' + manifest['charter_path']})  ")
    out.append(f"> **Window:** {manifest['window_start']} → {manifest['window_end']} "
               f"({metrics.get('years', '?')} years)  ")
    out.append(f"> **Initial capital:** ₹{manifest['initial_capital_inr']:,.0f}  ")
    out.append(f"> **Cost model:** `{manifest['cost_model']}` ({manifest['cost_product']})  ")
    out.append("")

    out.append("## Headline")
    out.append("")
    out.append("| Metric | This variant | NIFTYBEES (buy-and-hold) | Δ |")
    out.append("|---|---:|---:|---:|")
    v_cagr = metrics.get("cagr_pct", 0)
    b_cagr = benchmark.get("cagr_pct", 0) if "cagr_pct" in benchmark else 0
    v_dd = metrics.get("max_dd_pct", 0)
    b_dd = benchmark.get("max_dd_pct", 0) if "max_dd_pct" in benchmark else 0
    v_tr = metrics.get("total_return_pct", 0)
    b_tr = benchmark.get("total_return_pct", 0) if "total_return_pct" in benchmark else 0
    v_fe = metrics.get("final_equity_inr", 0)
    b_fe = benchmark.get("final_equity_inr", 0) if "final_equity_inr" in benchmark else 0
    out.append(f"| CAGR % | {v_cagr:+.2f} | {b_cagr:+.2f} | {v_cagr - b_cagr:+.2f} |")
    out.append(f"| Total Return % | {v_tr:+.2f} | {b_tr:+.2f} | {v_tr - b_tr:+.2f} |")
    out.append(f"| Max DD % | {v_dd:+.2f} | {b_dd:+.2f} | {v_dd - b_dd:+.2f} |")
    out.append(f"| Final equity ₹ | {v_fe:,.0f} | {b_fe:,.0f} | {v_fe - b_fe:+,.0f} |")
    out.append("")

    out.append("## Trade statistics")
    out.append("")
    out.append(f"- **Trades:** {metrics.get('n_trades', 0)}")
    out.append(f"- **Win rate:** {metrics.get('win_rate_pct', 0):.1f}%")
    out.append(f"- **Profit factor:** {metrics.get('profit_factor', 0)}")
    out.append(f"- **Gross profit (₹):** {metrics.get('gross_profit_inr', 0):,.0f}")
    out.append(f"- **Gross loss (₹):** {metrics.get('gross_loss_inr', 0):,.0f}")
    out.append(f"- **Avg charges/trade (₹):** {metrics.get('avg_charges_per_trade_inr', 0):,.2f}")
    out.append(f"- **Total charges (₹):** {metrics.get('total_charges_inr', 0):,.0f}")
    out.append("")

    out.append("## Charter §3.10 verdict")
    out.append("")
    pf = metrics.get("profit_factor")
    pf_val = pf if pf is not None and isinstance(pf, (int, float)) else 0.0
    cagr = metrics.get("cagr_pct", 0)
    dd = metrics.get("max_dd_pct", 0)
    if pf_val < 1.10:
        verdict = "A1 — PF < 1.10 → **no edge at any size; abandon.**"
    elif pf_val < 1.20:
        verdict = "A2 — PF ∈ [1.10, 1.20) → **Borderline; defer to retune.**"
    elif pf_val >= 1.20 and cagr < b_cagr + 2.0:
        verdict = "A3 — PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% → **Edge exists but doesn't justify cost burn; informational only.**"
    elif dd < -25.0:
        verdict = "A5 — MaxDD > 25% → **Stop; DD incompatible with capital base.**"
    elif pf_val >= 1.20 and cagr >= b_cagr + 2.0 and abs(dd) <= 25.0:
        verdict = "A4 — **PASS** → advance to Phase 2 paper-mode."
    else:
        verdict = "UNDETERMINED — check charter §3.10 manually."
    out.append(f"**{verdict}**")
    out.append("")

    if trades:
        out.append("## Exit-reason breakdown")
        out.append("")
        cnt = Counter(t.exit_reason for t in trades)
        for r, n in cnt.most_common():
            out.append(f"- {r}: {n}")
        out.append("")

        out.append("## Top 5 winners + bottom 5 losers")
        out.append("")
        sorted_trades = sorted(trades, key=lambda t: t.pnl_net_inr, reverse=True)
        out.append("### Top 5 winners")
        out.append("")
        out.append("| Symbol | Entry → Exit | Bars | PnL net ₹ |")
        out.append("|---|---|---:|---:|")
        for t in sorted_trades[:5]:
            out.append(f"| {t.symbol} | {t.entry_date.date()} → {t.exit_date.date()} | "
                       f"{t.bars_held} | {t.pnl_net_inr:+,.0f} |")
        out.append("")
        out.append("### Bottom 5 losers")
        out.append("")
        out.append("| Symbol | Entry → Exit | Bars | PnL net ₹ |")
        out.append("|---|---|---:|---:|")
        for t in sorted_trades[-5:]:
            out.append(f"| {t.symbol} | {t.entry_date.date()} → {t.exit_date.date()} | "
                       f"{t.bars_held} | {t.pnl_net_inr:+,.0f} |")
        out.append("")

    out.append("---")
    out.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST "
               f"by `packages/research/swing_backtester.py` (Engine B, Path B).*")
    return "\n".join(out)
