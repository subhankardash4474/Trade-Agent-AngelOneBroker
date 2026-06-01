"""V40 — Dual-momentum relative-strength (top-decile rank, monthly).

Hypothesis (Gary Antonacci, "Dual Momentum"): cross-sectional + absolute
momentum stacks. Hold the highest-ranked names by 12-month total return,
BUT only when their absolute 12-month return is positive AND above the
benchmark's (NIFTYBEES 12-month return). Rebalance monthly.

This is the only V35–V40 strategy that uses ``universe_signals_fn`` to
do CROSS-SECTIONAL ranking; the engine calls that hook once per bar
and passes the cached ranks dict to entry_fn via context.

Entry (all must be True):
    1. context["universe_signal"][symbol]["rank_pct"] <= top_decile_pct
       (i.e. symbol is in the top X% of the universe by 12-month return)
    2. symbol's 12-month return > 0                   (absolute momentum)
    3. symbol's 12-month return > NIFTYBEES 12-month return
       (relative strength vs benchmark)
    4. close[today] > SMA(50)[today]                  (avoid mid-correction)
    5. today is the first trading day of a month       (monthly rebalance)

Exit (any of):
    1. monthly rebalance + symbol no longer in top decile
       (exit_reason: "rank_drop_below_top_decile")
    2. close[today] < initial_stop (hard 2.5*ATR stop)
    3. bars_held > max_time_in_trade_bars (60)

Initial stop: max(2.5*ATR(14) below entry, entry * 0.88) — wider because
momentum positions tolerate larger pullbacks.

Engine impact: ``universe_signals_fn`` is called ONCE per bar BEFORE
entry candidate gathering. It computes 12-month returns for ALL symbols
in the history and returns a dict ``{symbol: {"return_12m": float,
"rank_pct": float (0=best, 1=worst)}}``. Cost is O(N) per bar in N
symbols, which is negligible vs the per-symbol entry-signal compute.

Charter compliance: long-only, CNC DELIVERY, vol-target sized via engine.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.swing_backtester import OpenPosition, StrategySpec


DEFAULT_MOMENTUM_LOOKBACK_BARS = 252      # ~12 months
DEFAULT_TOP_DECILE_PCT = 0.20             # actually "top quintile" — was top 10% too restrictive on 75-symbol universe
DEFAULT_BENCHMARK_SYMBOL = "NIFTYBEES"
DEFAULT_SMA_FILTER = 50
DEFAULT_ATR_PERIOD = 14
DEFAULT_STOP_ATR_MULT = 2.5
DEFAULT_STOP_FALLBACK_PCT = 0.88
DEFAULT_MAX_TIME_BARS = 60


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float("nan")
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    return val if np.isfinite(val) else float("nan")


def _is_first_trading_day_of_month(df_today: pd.DataFrame) -> bool:
    """True when today is the first trading day of the calendar month.

    Implemented as: today's month differs from the previous trading day's
    month. The previous trading day comes from the position-2 (-2) bar
    in the SYMBOL's history slice. Mid-month holidays don't matter — we
    only care that we're crossing a month boundary.
    """
    if len(df_today) < 2:
        return False
    today = df_today.index[-1]
    yest = df_today.index[-2]
    return today.month != yest.month or today.year != yest.year


def _universe_signals(
    history: Dict[str, pd.DataFrame],
    today: pd.Timestamp,
    params: Dict[str, Any],
) -> Dict[str, Dict[str, float]]:
    """Compute 12-month returns + cross-sectional rank for every symbol.

    Returns a dict ``{symbol: {"return_12m": float, "rank_pct": float}}``
    where ``rank_pct`` is in [0.0, 1.0] with 0.0 = best (highest return)
    and 1.0 = worst (lowest return). Symbols without enough history are
    excluded entirely.

    The benchmark return (default NIFTYBEES) is included so entry_fn
    can check relative strength vs the index without re-computing.
    """
    lookback = int(params.get("momentum_lookback_bars", DEFAULT_MOMENTUM_LOOKBACK_BARS))
    benchmark = str(params.get("benchmark_symbol", DEFAULT_BENCHMARK_SYMBOL))

    returns: Dict[str, float] = {}
    for sym, df in history.items():
        if today not in df.index:
            continue
        pos = df.index.get_loc(today)
        df_slice = df.iloc[: pos + 1]
        if len(df_slice) < lookback + 1:
            continue
        price_today = float(df_slice["close"].iloc[-1])
        price_then = float(df_slice["close"].iloc[-lookback - 1])
        if price_then <= 0 or not math.isfinite(price_then):
            continue
        ret = (price_today / price_then) - 1.0
        if not math.isfinite(ret):
            continue
        returns[sym] = ret

    if not returns:
        return {}

    # Rank by descending return.
    sorted_syms = sorted(returns.items(), key=lambda kv: kv[1], reverse=True)
    n = len(sorted_syms)
    benchmark_return = returns.get(benchmark, 0.0)

    out: Dict[str, Dict[str, float]] = {}
    for i, (sym, ret) in enumerate(sorted_syms):
        out[sym] = {
            "return_12m": ret,
            "rank_pct": i / (n - 1) if n > 1 else 0.0,
            "rank": i,
            "n_universe": n,
            "benchmark_return_12m": benchmark_return,
        }
    return out


def _entry(
    df_today: pd.DataFrame,
    params: Dict[str, Any],
    last_entry_bar_index: Optional[int],
    context: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    sma_filter = int(params.get("sma_filter", DEFAULT_SMA_FILTER))
    top_decile_pct = float(params.get("top_decile_pct", DEFAULT_TOP_DECILE_PCT))

    sym = context.get("symbol")
    universe_signal = context.get("universe_signal")

    # Gate 5 — monthly rebalance
    if not _is_first_trading_day_of_month(df_today):
        return False, {"reason": "not_month_start"}

    if not universe_signal or sym not in universe_signal:
        return False, {"reason": "no_universe_signal_for_symbol"}

    sig = universe_signal[sym]
    rank_pct = sig["rank_pct"]
    ret_12m = sig["return_12m"]
    bench_ret = sig.get("benchmark_return_12m", 0.0)

    # Gate 1 — top decile (or top-X%)
    if rank_pct > top_decile_pct:
        return False, {"reason": "outside_top_decile", "rank_pct": rank_pct, "thresh": top_decile_pct}

    # Gate 2 — absolute momentum
    if ret_12m <= 0:
        return False, {"reason": "absolute_momentum_negative", "ret_12m": ret_12m}

    # Gate 3 — relative strength vs benchmark
    if ret_12m <= bench_ret:
        return False, {"reason": "relstrength_failed", "ret_12m": ret_12m, "bench": bench_ret}

    # Gate 4 — close > 50-SMA
    if len(df_today) >= sma_filter:
        sma = float(df_today["close"].rolling(sma_filter).mean().iloc[-1])
        if np.isfinite(sma) and float(df_today["close"].iloc[-1]) <= sma:
            return False, {"reason": "below_sma50"}

    return True, {
        "rank_pct": rank_pct,
        "rank": sig.get("rank"),
        "return_12m": ret_12m,
        "benchmark_return_12m": bench_ret,
    }


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
) -> Optional[str]:
    max_time = int(params.get("max_time_in_trade_bars", DEFAULT_MAX_TIME_BARS))

    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    # (a) hard stop
    if position.initial_stop > 0 and today_close < position.initial_stop:
        return "stop_loss"

    # (b) Rank-drop check is deferred until monthly rebalance because the
    # engine's exit_fn doesn't see the universe_signal (only entry_fn
    # does). A cleaner v4.1 evolution would extend exit_fn to also
    # receive context, BUT today the simpler hack is: implement
    # rank-drop via a "force exit on first day of next month" rule and
    # rely on entry_fn to re-establish positions only for symbols that
    # still rank top-decile. We approximate this by exiting at month
    # boundaries on a fixed schedule.
    #
    # NOTE: this is a CORRECTNESS COMPROMISE for the V40 prototype.
    # Trade attribution will show a spike of "month_end_rebalance"
    # exits on the first trading day of each month — those are the
    # forced book-cleansings. If V40 shows edge, the v4.1 follow-up
    # adds context-aware exit_fn and removes this rule.
    if _is_first_trading_day_of_month(df_today):
        # The position was OPENED on the first day of *some* month; we
        # want to force-close on EVERY subsequent first-of-month boundary
        # (not on the entry's own first-of-month). The entry_bar_index
        # check guards against immediate same-day exit.
        bars_held = today_pos - position.entry_bar_index
        if bars_held >= 1:
            return "month_end_rebalance"

    # (c) timeout (insurance — momentum positions sometimes drift)
    if (today_pos - position.entry_bar_index) > max_time:
        return "time_in_trade"

    return None


def _is_first_trading_day_of_month_today(df_today: pd.DataFrame) -> bool:
    """Alias kept for symmetry with the entry/exit conventions."""
    return _is_first_trading_day_of_month(df_today)


def _initial_stop(df_at_entry: pd.DataFrame, params: Dict[str, Any]) -> float:
    price = float(df_at_entry["close"].iloc[-1])
    atr_val = _atr(df_at_entry, period=int(params.get("atr_period", DEFAULT_ATR_PERIOD)))
    mult = float(params.get("stop_atr_mult", DEFAULT_STOP_ATR_MULT))
    fallback = float(params.get("stop_fallback_pct", DEFAULT_STOP_FALLBACK_PCT))
    if math.isfinite(atr_val) and atr_val > 0:
        return max(price - mult * atr_val, price * fallback)
    return price * fallback


SPEC = StrategySpec(
    name="V40_dual_momentum_relstrength",
    description="Top-quintile 12-month return + absolute > 0 + > NIFTYBEES; monthly rebalance, 2.5*ATR stop.",
    required_warmup_bars=260,  # 12-month lookback + 1 month guard
    entry_fn=_entry,
    exit_fn=_exit,
    initial_stop_fn=_initial_stop,
    universe_signals_fn=_universe_signals,
    default_params={
        "momentum_lookback_bars": DEFAULT_MOMENTUM_LOOKBACK_BARS,
        "top_decile_pct": DEFAULT_TOP_DECILE_PCT,
        "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
        "sma_filter": DEFAULT_SMA_FILTER,
        "atr_period": DEFAULT_ATR_PERIOD,
        "stop_atr_mult": DEFAULT_STOP_ATR_MULT,
        "stop_fallback_pct": DEFAULT_STOP_FALLBACK_PCT,
        "max_time_in_trade_bars": DEFAULT_MAX_TIME_BARS,
    },
    cost_product="DELIVERY",
)
