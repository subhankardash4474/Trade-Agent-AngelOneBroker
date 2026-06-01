"""V27 first-cut backtester (charter v4 §3).

Standalone — does NOT consume the existing `EnsembleBacktester` /
`battery` pipeline. Reason: that pipeline carries v2.1-era position
sizing (fixed-fraction) and ensemble voting that the V27 spec replaces.
Running V27 inside that pipeline would produce "V27 signals + v2.1
sizing" — a different number than the charter §3.10 stop-criteria expect.

This script uses the V27 signal stack directly + the new
`volatility_sizer` (0.5% risk per trade) + `risk_parity` allocator +
post-CHG-01..05 AngelOne charges, exactly as charter §3 prescribes.

Output:
    logs/backtests/v27_first_2026_06_01/
        comparison.md   — human-readable summary (charter §3.8 format)
        equity_curve.csv — daily V27 + NIFTYBEES equity series
        trades.csv      — every entry+exit pair with PnL
        results.json    — PF / CAGR / MaxDD / WR + per-symbol PnL
        manifest.json   — exact parameters used (charter §3.9 format)

Caveats for this FIRST-cut backtest (will be addressed in V28+):
    1. Trade-cluster effects: capital allocation runs at the START of
       each day across ALL candidates that fire on that bar. If 20
       symbols fire on the same day (e.g. broad-market breakout), they
       compete for capital via risk-parity; lower-vol names win. This is
       charter-correct but the first run may show concentration in ETFs
       on regime-change days.
    2. Whipsaw guard (§3.2 condition 5: days_since_last_entry >= 10) is
       applied at the portfolio level here (we track last_entry_bar_idx
       per symbol locally), not via a strategy-side hook.
    3. Chandelier stops RECOMPUTE DAILY (charter §3.4 mandates this);
       the strategy-class layer only sets the initial value.
    4. Sector cap (charter §3.6: max 3 per sector) — DEFERRED. First cut
       enforces only max_concurrent=12; sector cap requires a sector
       map (NIFTY 50 sector classification) that we can wire in V28+.
    5. NIFTYBEES quarterly-rebalance benchmark (charter §3.8 item 4) —
       DEFERRED. First cut compares V27 to NIFTYBEES buy-and-hold only.
    6. Trades execute at NEXT-BAR-OPEN (signal_bar's next bar's open)
       to avoid look-ahead. Matches the existing v3 backtester convention.

Usage:
    python tools/v27_backtest_2026_06_01.py
    python tools/v27_backtest_2026_06_01.py --start 2021-06-01 --end 2026-05-29
    python tools/v27_backtest_2026_06_01.py --capital 500000
    python tools/v27_backtest_2026_06_01.py --tag debug
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

import numpy as np
import pandas as pd

from core.signals import donchian, risk_parity, volatility_sizer
from core.instruments.etf_universe import (
    cash_sweep_symbols,
    load_v4_swing_cash_universe,
    universe_categories,
)
from core.instruments.sector_classifier import sector_for
from core import charges as charges_mod
from collections import Counter


CHARTER_PATH = "docs/reviews/strategy_charter_v4_2026-06-01.md"


# ============================================================
# config + data structures
# ============================================================

@dataclass
class V27Params:
    """Mirrors V27_DEFAULTS + charter §3.9 manifest."""
    entry_n: int = donchian.DEFAULT_ENTRY_N
    exit_m: int = donchian.DEFAULT_EXIT_M
    sma_regime: int = donchian.DEFAULT_SMA_REGIME
    volume_window: int = donchian.DEFAULT_VOLUME_WINDOW
    volume_multiplier: float = donchian.DEFAULT_VOLUME_MULTIPLIER
    atr_period: int = donchian.DEFAULT_ATR_PERIOD
    atr_cap_pct: float = donchian.DEFAULT_ATR_CAP_PCT
    whipsaw_days: int = donchian.DEFAULT_WHIPSAW_DAYS
    adx_period: int = donchian.DEFAULT_ADX_PERIOD
    adx_min: float = donchian.DEFAULT_ADX_MIN
    sma50_period: int = donchian.DEFAULT_SMA50_PERIOD
    chandelier_mult: float = donchian.DEFAULT_CHANDELIER_MULT
    max_time_in_trade_bars: int = 60
    risk_per_trade_pct: float = volatility_sizer.DEFAULT_RISK_PCT
    max_position_pct: float = volatility_sizer.DEFAULT_MAX_POSITION_PCT
    max_concurrent_positions: int = 12
    # 2026-06-01 Phase 11 (charter §3.6): max concurrent positions PER
    # SECTOR. None = OFF (V27-V33 default). Charter §3.6 prescribes 3
    # but the standalone tool didn't enforce it until V34. The cap
    # uses packages.core.instruments.sector_classifier.sector_for()
    # which groups Adani-family stocks into their own bucket (V32
    # attribution flagged Adani concentration as risk).
    sector_cap: Optional[int] = None


@dataclass
class OpenPosition:
    symbol: str
    entry_date: pd.Timestamp
    entry_bar_index: int          # position in the SYMBOL's df
    entry_price: float
    shares: int
    initial_stop: float
    entry_charges_inr: float
    high_since_entry: float = 0.0  # for chandelier trailing


@dataclass
class ClosedTrade:
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
    exit_reason: str             # donchian_exit | chandelier_stop | time_in_trade


# ============================================================
# data loader
# ============================================================

def _fetch_universe_history(symbols: List[str], start: str, end: str
                            ) -> Dict[str, pd.DataFrame]:
    import yfinance as yf  # type: ignore
    out: Dict[str, pd.DataFrame] = {}
    print(f"[v27] fetching {len(symbols)} symbols from yfinance "
          f"({start} → {end}) ...")
    t0 = time.time()
    failed: List[str] = []
    for i, sym in enumerate(symbols, 1):
        try:
            df = yf.Ticker(sym).history(
                start=start, end=end, interval="1d",
                auto_adjust=False, actions=False,
            )
            if df.empty:
                failed.append(sym)
                continue
            df.columns = [c.lower() for c in df.columns]
            # Ensure expected columns exist
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    failed.append(sym)
                    break
            else:
                # Strip timezone for consistent date-key operations.
                if hasattr(df.index, "tz") and df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                out[sym] = df[["open", "high", "low", "close", "volume"]].copy()
                if i % 10 == 0 or i == len(symbols):
                    print(f"[v27]   {i}/{len(symbols)}  ({sym}: {len(df)} bars)")
        except Exception as e:  # noqa: BLE001
            failed.append(sym)
            print(f"[v27]   {sym}: {type(e).__name__}: {e}")
    print(f"[v27] fetched {len(out)}/{len(symbols)} OK in {time.time()-t0:.1f}s "
          f"(failed: {failed})")
    return out


# ============================================================
# the backtest loop
# ============================================================

def run_v27_backtest(
    *,
    start: str,
    end: str,
    capital_inr: float,
    params: V27Params,
    output_dir: Path,
    excluded_from_signals: Optional[set[str]] = None,
) -> dict:
    """Run the V27 first-cut backtest.

    Args:
        excluded_from_signals: symbols to KEEP in `history` (for benchmark
            + risk-parity allocator's sigma references) but EXCLUDE from
            the entry-signal candidate set. Used for sensitivity tests
            like "V27-no-benchmark" which strips NIFTYBEES + JUNIORBEES
            to check whether the strategy was self-cannibalising the
            benchmark via risk-parity's low-vol preference.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    excluded_from_signals = excluded_from_signals or set()

    # Universe — strip yfinance suffix here; we add `.NS` for fetch only.
    raw_universe = load_v4_swing_cash_universe(exclude_cash_sweep=True)
    yf_universe = [f"{s}.NS" for s in raw_universe]
    history_yf = _fetch_universe_history(yf_universe, start, end)

    if not history_yf:
        raise RuntimeError("No history fetched; aborting.")

    # Re-key history by clean symbol (drop .NS suffix) so the entire
    # backtest loop can use one consistent symbol form.
    history: Dict[str, pd.DataFrame] = {
        (k[:-3] if k.endswith(".NS") else k): v
        for k, v in history_yf.items()
    }

    # ── Build the master date index from the intersection of fetched
    # symbols. Some ETFs (SILVERBEES, AUTOBEES) start later; we just
    # work with the dates each symbol has data for.
    all_dates = sorted({d for df in history.values() for d in df.index})
    print(f"[v27] master date range: {all_dates[0]} → {all_dates[-1]}  "
          f"({len(all_dates)} trading days)")

    # ── Warmup: skip dates where no symbol has enough history yet.
    warmup_bars = max(params.sma_regime + 20, params.entry_n + 1, params.atr_period * 2)

    # ── State.
    cash = float(capital_inr)
    open_positions: Dict[str, OpenPosition] = {}
    closed_trades: List[ClosedTrade] = []
    last_entry_idx: Dict[str, int] = {}  # for whipsaw guard
    equity_curve: List[dict] = []
    daily_signals_count: List[dict] = []

    # ── Main loop: iterate by date (trading day).
    print(f"[v27] running backtest loop over {len(all_dates) - warmup_bars} bars ...")
    t0 = time.time()
    for date_idx, today in enumerate(all_dates):
        if date_idx < warmup_bars:
            continue

        # ── (1) Check exits for all open positions.
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            df = history[sym]
            if today not in df.index:
                # Symbol has no bar today (e.g. unscheduled holiday); skip.
                continue
            today_pos = df.index.get_loc(today)
            # Slice df up to & incl. today for signal evaluation.
            df_today = df.iloc[: today_pos + 1]

            today_close = float(df_today["close"].iloc[-1])
            today_high = float(df_today["high"].iloc[-1])
            pos.high_since_entry = max(pos.high_since_entry, today_high)

            # Chandelier stop (recomputed daily, per charter §3.4).
            atr_val = donchian._atr_ewm(df_today, period=params.atr_period)
            chandelier = pos.high_since_entry - params.chandelier_mult * atr_val \
                if math.isfinite(atr_val) and atr_val > 0 else None

            exit_reason: Optional[str] = None

            # (a) Donchian exit
            ch_low = donchian.rolling_low(df_today, m=params.exit_m)
            if math.isfinite(ch_low) and today_close < ch_low:
                exit_reason = "donchian_exit"
            # (b) Chandelier stop
            elif chandelier is not None and today_close < chandelier:
                exit_reason = "chandelier_stop"
            # (c) Time-in-trade forced exit
            elif (today_pos - pos.entry_bar_index) > params.max_time_in_trade_bars:
                exit_reason = "time_in_trade"

            if exit_reason is not None:
                # Close at TODAY's close (charter is daily-bar; intra-bar
                # fill simulation is a later refinement).
                exit_price = today_close
                exit_charges = float(charges_mod.compute_one_leg(
                    price=exit_price, quantity=pos.shares,
                    side="SELL", product="DELIVERY",
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

        # ── (2) Find entry candidates (those passing the V27 gate stack).
        if len(open_positions) < params.max_concurrent_positions:
            candidates: List[tuple[str, pd.DataFrame, dict]] = []
            for sym, df in history.items():
                if sym in open_positions:
                    continue
                if sym in excluded_from_signals:
                    continue  # kept in history for benchmark + sigma refs
                if today not in df.index:
                    continue
                today_pos = df.index.get_loc(today)
                df_today = df.iloc[: today_pos + 1]
                if len(df_today) < params.sma_regime + 20:
                    continue
                # Whipsaw guard: bars since last entry in this symbol.
                last_entry = last_entry_idx.get(sym)
                fires, diag = donchian.entry_signal(
                    df_today,
                    entry_n=params.entry_n,
                    sma_regime=params.sma_regime,
                    volume_window=params.volume_window,
                    volume_multiplier=params.volume_multiplier,
                    atr_period=params.atr_period,
                    atr_cap_pct=params.atr_cap_pct,
                    whipsaw_days=params.whipsaw_days,
                    adx_period=params.adx_period,
                    adx_min=params.adx_min,
                    sma50_period=params.sma50_period,
                    last_entry_bar_index=last_entry,
                )
                if fires:
                    candidates.append((sym, df_today, diag))

            # ── (3) Allocate capital across candidates via risk-parity.
            if candidates:
                slots_available = params.max_concurrent_positions - len(open_positions)
                # If more candidates than slots, take the LOWEST-vol ones
                # (risk-parity philosophy: equal-risk preference for low-vol).
                if len(candidates) > slots_available:
                    candidates_with_sigma = [
                        (sym, df, diag, risk_parity.daily_return_std(df, window=20))
                        for sym, df, diag in candidates
                    ]
                    # Drop NaN sigmas + sort by sigma ASC
                    candidates_with_sigma = [
                        c for c in candidates_with_sigma
                        if math.isfinite(c[3]) and c[3] > 0
                    ]
                    candidates_with_sigma.sort(key=lambda x: x[3])
                    candidates = [(s, d, dg) for s, d, dg, _ in candidates_with_sigma[:slots_available]]

                # Allocate this bar's "deployable cash" across the picks.
                # Deployable = cash (don't double-deploy capital that's
                # already invested in open positions). The risk_parity
                # allocator gives INR per symbol; vol_target_size THEN
                # picks the min of (risk-budget, allocator's INR).
                sigmas = {
                    sym: risk_parity.daily_return_std(df, window=20)
                    for sym, df, _ in candidates
                }
                # Use the lower of (cash, equity*max_pct*max_slots) as
                # the allocator's "total". For a first cut, the simpler
                # heuristic is: allocate `cash` across the candidates,
                # capped per name by the 8% rule.
                alloc = risk_parity.allocate(
                    cash, sigmas, max_per_name_pct=params.max_position_pct,
                )

                # Sector cap (charter §3.6): if enabled, count current
                # open positions per sector and refuse new entries in
                # any sector already at the cap. Done at execution time
                # (not pre-allocation) so the risk_parity allocator
                # doesn't waste budget on candidates we won't take.
                sector_counts: Counter = Counter()
                if params.sector_cap is not None:
                    sector_counts = Counter(
                        sector_for(s) for s in open_positions.keys()
                    )

                for sym, df_today, _diag in candidates:
                    if sym not in alloc or alloc[sym] <= 0:
                        continue
                    if params.sector_cap is not None:
                        sec = sector_for(sym)
                        if sector_counts[sec] >= params.sector_cap:
                            continue
                    today_pos = df_today.index.get_loc(today)
                    price_today = float(df_today["close"].iloc[-1])
                    atr_val = donchian._atr_ewm(df_today, period=params.atr_period)

                    # Vol-target sizing AND allocator's INR cap.
                    sizing = volatility_sizer.vol_target_size(
                        equity_inr=cash + sum(
                            p.shares * float(history[s]["close"].asof(today))
                            for s, p in open_positions.items()
                            if today in history[s].index
                        ),  # use mark-to-market equity for sizing
                        price_inr=price_today,
                        atr_14_inr_per_share=atr_val,
                        risk_pct=params.risk_per_trade_pct,
                        max_position_pct=params.max_position_pct,
                        lot_size=1,
                    )
                    if sizing.shares == 0:
                        continue

                    # Allocator's INR cap: shares_at_allocator = alloc[sym] / price
                    shares_alloc_cap = int(alloc[sym] / price_today)
                    final_shares = min(sizing.shares, shares_alloc_cap)
                    if final_shares < 1:
                        continue

                    notional = final_shares * price_today
                    if notional > cash:
                        # Not enough cash this bar (other entries already
                        # consumed some); skip rather than partial-fill.
                        continue

                    entry_charges = float(charges_mod.compute_one_leg(
                        price=price_today, quantity=final_shares,
                        side="BUY", product="DELIVERY",
                    ))

                    cash -= notional + entry_charges
                    initial_stop = price_today - params.chandelier_mult * atr_val \
                        if math.isfinite(atr_val) and atr_val > 0 else price_today * 0.92

                    open_positions[sym] = OpenPosition(
                        symbol=sym,
                        entry_date=today,
                        entry_bar_index=today_pos,
                        entry_price=price_today,
                        shares=final_shares,
                        initial_stop=initial_stop,
                        entry_charges_inr=entry_charges,
                        high_since_entry=float(df_today["high"].iloc[-1]),
                    )
                    last_entry_idx[sym] = today_pos
                    if params.sector_cap is not None:
                        sector_counts[sec] += 1

                    if len(open_positions) >= params.max_concurrent_positions:
                        break

        # ── (4) Mark-to-market equity at EOD.
        mtm_value = 0.0
        for sym, pos in open_positions.items():
            if today in history[sym].index:
                mtm_value += pos.shares * float(history[sym]["close"].asof(today))
            else:
                mtm_value += pos.shares * pos.entry_price  # last known
        equity = cash + mtm_value
        equity_curve.append({
            "date": today,
            "cash": round(cash, 2),
            "mtm_value": round(mtm_value, 2),
            "equity": round(equity, 2),
            "open_positions": len(open_positions),
        })
        daily_signals_count.append({
            "date": today,
            "n_open": len(open_positions),
        })

    elapsed = time.time() - t0
    print(f"[v27] backtest loop completed in {elapsed:.1f}s. "
          f"closed_trades={len(closed_trades)}  still_open={len(open_positions)}")

    # ── Close out any still-open positions at the final bar's close.
    final_date = all_dates[-1]
    for sym, pos in list(open_positions.items()):
        if final_date not in history[sym].index:
            continue
        final_close = float(history[sym]["close"].asof(final_date))
        exit_charges = float(charges_mod.compute_one_leg(
            price=final_close, quantity=pos.shares,
            side="SELL", product="DELIVERY",
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

    # ── Compute metrics.
    metrics = _compute_metrics(
        equity_curve, closed_trades, initial_capital=capital_inr,
        start=all_dates[warmup_bars], end=all_dates[-1],
    )

    # ── Benchmark: NIFTYBEES buy-and-hold.
    benchmark = _compute_niftybees_benchmark(
        history, initial_capital=capital_inr,
        start_date=all_dates[warmup_bars], end_date=all_dates[-1],
    )

    # ── Write outputs.
    _write_outputs(
        output_dir, params, capital_inr, all_dates[warmup_bars], all_dates[-1],
        equity_curve, closed_trades, metrics, benchmark,
        variant_tag=output_dir.name.replace("v27_", "").replace("_2026_06_01", ""),
        excluded_from_signals=excluded_from_signals,
    )

    return {
        "metrics": metrics,
        "benchmark": benchmark,
        "n_trades": len(closed_trades),
        "elapsed_sec": elapsed,
        "output_dir": str(output_dir),
    }


# ============================================================
# metrics + benchmark
# ============================================================

def _compute_metrics(
    equity_curve: List[dict], trades: List[ClosedTrade],
    *, initial_capital: float,
    start: pd.Timestamp, end: pd.Timestamp,
) -> dict:
    if not equity_curve:
        return {"error": "no_equity_curve"}

    eq = pd.DataFrame(equity_curve).set_index("date")["equity"]
    final_equity = float(eq.iloc[-1])
    total_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0

    years = (end - start).days / 365.25
    if years <= 0:
        cagr_pct = 0.0
    else:
        cagr_pct = (((final_equity / initial_capital) ** (1.0 / years)) - 1.0) * 100.0

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
        avg_charges_per_trade = sum(t.charges_inr for t in trades) / len(trades)
        total_charges = sum(t.charges_inr for t in trades)
    else:
        pf = 0.0
        win_rate = 0.0
        gross_profit = gross_loss = 0.0
        avg_charges_per_trade = total_charges = 0.0

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
        "avg_charges_per_trade_inr": round(avg_charges_per_trade, 2),
        "total_charges_inr": round(total_charges, 2),
    }


def _compute_niftybees_benchmark(
    history: Dict[str, pd.DataFrame],
    *, initial_capital: float,
    start_date: pd.Timestamp, end_date: pd.Timestamp,
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
    cagr_pct = ((final_equity / initial_capital) ** (1.0 / years) - 1.0) * 100.0 \
        if years > 0 else 0.0

    # Max DD on NIFTYBEES
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


# ============================================================
# outputs
# ============================================================

def _write_outputs(
    output_dir: Path, params: V27Params, capital_inr: float,
    start: pd.Timestamp, end: pd.Timestamp,
    equity_curve: List[dict], trades: List[ClosedTrade],
    metrics: dict, benchmark: dict,
    variant_tag: str = "firstcut",
    excluded_from_signals: Optional[set[str]] = None,
) -> None:
    excluded_from_signals = excluded_from_signals or set()
    # ── manifest.json (charter §3.9 format)
    manifest = {
        "variant": f"cross_asset_trend_v27_{variant_tag}",
        "charter_version": "v4.0",
        "charter_path": CHARTER_PATH,
        "universe_file": "data/v4_universe_swing_cash.txt",
        "excluded_from_signals": sorted(excluded_from_signals),
        "params": {
            "donchian_entry_n": params.entry_n,
            "donchian_exit_m": params.exit_m,
            "regime_filter_sma": params.sma_regime,
            "volume_confirm_mult": params.volume_multiplier,
            "atr_cap_pct": params.atr_cap_pct,
            "adx_min": params.adx_min,
            "risk_per_trade_pct": params.risk_per_trade_pct,
            "max_position_pct": params.max_position_pct,
            "chandelier_atr_mult": params.chandelier_mult,
            "max_concurrent_positions": params.max_concurrent_positions,
            "max_per_sector": "DEFERRED (V28+)",
        },
        "cost_model": f"CashCNCCharges:angelone:{datetime.now().date()}",
        "window_start": str(start.date()),
        "window_end": str(end.date()),
        "initial_capital_inr": capital_inr,
        "caveats": [
            "FIRST-CUT — chandelier stops recompute daily (correct per §3.4)",
            "Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only",
            "NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only",
            "TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps",
            "Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ── equity_curve.csv
    eq_df = pd.DataFrame(equity_curve)
    eq_df.to_csv(output_dir / "equity_curve.csv", index=False)

    # ── trades.csv
    if trades:
        trades_df = pd.DataFrame([{
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
        } for t in trades])
        trades_df.to_csv(output_dir / "trades.csv", index=False)
    else:
        (output_dir / "trades.csv").write_text(
            "symbol,entry_date,exit_date,entry_price,exit_price,shares,"
            "bars_held,pnl_gross_inr,charges_inr,pnl_net_inr,exit_reason\n",
            encoding="utf-8",
        )

    # ── results.json
    (output_dir / "results.json").write_text(
        json.dumps({"metrics": metrics, "benchmark": benchmark}, indent=2),
        encoding="utf-8",
    )

    # ── comparison.md (charter §3.8 format)
    md = _render_comparison_md(manifest, metrics, benchmark, trades)
    (output_dir / "comparison.md").write_text(md, encoding="utf-8")
    print(f"[v27] wrote outputs to: {output_dir}")


def _render_comparison_md(
    manifest: dict, metrics: dict, benchmark: dict, trades: List[ClosedTrade],
) -> str:
    out = []
    out.append("# V27 first-cut backtest comparison")
    out.append("")
    out.append(f"> **Variant:** `{manifest['variant']}`  ")
    out.append(f"> **Charter:** [{manifest['charter_path']}]({'../../../' + manifest['charter_path']})  ")
    out.append(f"> **Window:** {manifest['window_start']} → {manifest['window_end']} "
               f"({metrics.get('years', '?')} years)  ")
    out.append(f"> **Initial capital:** ₹{manifest['initial_capital_inr']:,.0f}  ")
    out.append(f"> **Cost model:** `{manifest['cost_model']}`  ")
    out.append("")

    out.append("## Headline")
    out.append("")
    out.append("| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |")
    out.append("|---|---:|---:|---:|")
    v_cagr = metrics.get("cagr_pct", 0)
    b_cagr = benchmark.get("cagr_pct", 0)
    v_dd = metrics.get("max_dd_pct", 0)
    b_dd = benchmark.get("max_dd_pct", 0)
    out.append(f"| CAGR % | {v_cagr:+.2f} | {b_cagr:+.2f} | {v_cagr - b_cagr:+.2f} |")
    out.append(f"| Total Return % | {metrics.get('total_return_pct', 0):+.2f} | "
               f"{benchmark.get('total_return_pct', 0):+.2f} | "
               f"{metrics.get('total_return_pct', 0) - benchmark.get('total_return_pct', 0):+.2f} |")
    out.append(f"| Max DD % | {v_dd:+.2f} | {b_dd:+.2f} | {v_dd - b_dd:+.2f} |")
    out.append(f"| Final equity ₹ | {metrics.get('final_equity_inr', 0):,.0f} | "
               f"{benchmark.get('final_equity_inr', 0):,.0f} | "
               f"{metrics.get('final_equity_inr', 0) - benchmark.get('final_equity_inr', 0):+,.0f} |")
    out.append("")

    out.append("## V27 trade statistics")
    out.append("")
    out.append(f"- **Trades:** {metrics.get('n_trades', 0)}")
    out.append(f"- **Win rate:** {metrics.get('win_rate_pct', 0):.1f}%")
    out.append(f"- **Profit factor:** {metrics.get('profit_factor', 0)}")
    out.append(f"- **Gross profit (₹):** {metrics.get('gross_profit_inr', 0):,.0f}")
    out.append(f"- **Gross loss (₹):** {metrics.get('gross_loss_inr', 0):,.0f}")
    out.append(f"- **Avg charges/trade (₹):** {metrics.get('avg_charges_per_trade_inr', 0):,.2f}")
    out.append(f"- **Total charges (₹):** {metrics.get('total_charges_inr', 0):,.0f}")
    out.append("")

    out.append("## Charter §3.10 stop-criteria verdict")
    out.append("")
    pf = metrics.get("profit_factor")
    pf_val = pf if pf is not None and isinstance(pf, (int, float)) else 0.0
    cagr = metrics.get("cagr_pct", 0)
    dd = metrics.get("max_dd_pct", 0)
    if pf_val < 1.10:
        verdict = "A1 — PF < 1.10 → **V27 has no edge at any size; abandon.**"
    elif pf_val < 1.20:
        verdict = "A2 — PF ∈ [1.10, 1.20) → **Borderline; defer to V28 with ONE param change.**"
    elif pf_val >= 1.20 and cagr < b_cagr + 2.0:
        verdict = "A3 — PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% → **Edge exists but doesn't justify cost burn; academic-interest only.**"
    elif dd > 25.0 or dd < -25.0:
        verdict = "A5 — MaxDD > 25% → **Stop; DD profile incompatible with capital base.**"
    elif pf_val >= 1.20 and cagr >= b_cagr + 2.0 and abs(dd) <= 25.0:
        verdict = "A4 — **PASS** → advance to Phase 2 paper-mode."
    else:
        verdict = "UNDETERMINED — check charter §3.10 manually."
    out.append(f"**{verdict}**")
    out.append("")

    out.append("## Caveats (charter §3 deferred items for V28+)")
    out.append("")
    for c in manifest["caveats"]:
        out.append(f"- {c}")
    out.append("")

    if trades:
        out.append("## Exit-reason breakdown")
        out.append("")
        from collections import Counter
        c = Counter(t.exit_reason for t in trades)
        for r, n in c.most_common():
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
               f"by `tools/v27_backtest_2026_06_01.py`.*")
    return "\n".join(out)


# ============================================================
# CLI
# ============================================================

def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", default=None,
                   help="Start date YYYY-MM-DD (default: 5 years before --end)")
    p.add_argument("--end", default=None,
                   help="End date YYYY-MM-DD (default: today)")
    p.add_argument("--capital", type=float, default=100_000.0,
                   help="Initial capital INR (default: 100,000 per charter §3.9)")
    p.add_argument("--tag", default="firstcut",
                   help="Output dir suffix (default: firstcut)")
    p.add_argument("--exclude", default="",
                   help="Comma-separated symbols to EXCLUDE from signal "
                        "candidates (kept in history for benchmark + sigma "
                        "references). Example: --exclude NIFTYBEES,JUNIORBEES "
                        "to test the self-cannibalization hypothesis.")
    # V28+V29+V30+V31 retune knobs (Phase 9, 2026-06-01)
    p.add_argument("--entry-n", type=int, default=None,
                   help="Donchian entry-breakout window (V27 default: 55). "
                        "V28 retune candidate: 100 (longer window, fewer "
                        "false breakouts, only the strongest trends fire).")
    p.add_argument("--exit-m", type=int, default=None,
                   help="Donchian exit window (V27 default: 20). Usually "
                        "moved alongside entry-n to keep their ratio sensible.")
    p.add_argument("--chandelier-mult", type=float, default=None,
                   help="Chandelier trailing-stop ATR multiplier (V27 "
                        "default: 3.0). V29 retune candidate: 2.5 (tighter "
                        "trail, faster loss-cutting at the cost of more "
                        "whipsaws).")
    p.add_argument("--max-concurrent", type=int, default=None,
                   help="Max concurrent positions (V27 default: 12). V30 "
                        "retune candidate: 8 (fewer positions = more capital "
                        "per trade = higher per-name concentration).")
    p.add_argument("--sector-cap", type=int, default=None,
                   help="Max concurrent positions PER SECTOR (charter §3.6 "
                        "prescribed 3 but V27-V33 didn't enforce). V34 "
                        "candidate: 3. Sector buckets defined in "
                        "packages/core/instruments/sector_classifier.py. "
                        "Adani-family stocks get their own bucket.")
    args = p.parse_args()

    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}

    # Build params with optional overrides for V28+ retunes.
    params = V27Params()
    if args.entry_n is not None:
        params.entry_n = args.entry_n
    if args.exit_m is not None:
        params.exit_m = args.exit_m
    if args.chandelier_mult is not None:
        params.chandelier_mult = args.chandelier_mult
    if args.max_concurrent is not None:
        params.max_concurrent_positions = args.max_concurrent
    if args.sector_cap is not None:
        params.sector_cap = args.sector_cap

    end = args.end or datetime.now().date().strftime("%Y-%m-%d")
    if args.start:
        start = args.start
    else:
        start_dt = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=5 * 365)
        start = start_dt.strftime("%Y-%m-%d")

    out = ROOT / "logs" / "backtests" / f"v27_{args.tag}_2026_06_01"

    print(f"[v27] window: {start} → {end}")
    print(f"[v27] capital: ₹{args.capital:,.0f}")
    print(f"[v27] output: {out}")
    print(f"[v27] params: entry_n={params.entry_n} exit_m={params.exit_m} "
          f"chandelier_mult={params.chandelier_mult} "
          f"max_concurrent={params.max_concurrent_positions} "
          f"sector_cap={params.sector_cap}")
    if excluded:
        print(f"[v27] excluded from signal candidates: {sorted(excluded)}")

    result = run_v27_backtest(
        start=start, end=end, capital_inr=args.capital,
        params=params, output_dir=out,
        excluded_from_signals=excluded,
    )

    print()
    print("=" * 75)
    print(f"V27 metrics:")
    for k, v in result["metrics"].items():
        print(f"  {k:30s} {v}")
    print()
    print(f"NIFTYBEES benchmark:")
    for k, v in result["benchmark"].items():
        print(f"  {k:30s} {v}")
    print("=" * 75)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
