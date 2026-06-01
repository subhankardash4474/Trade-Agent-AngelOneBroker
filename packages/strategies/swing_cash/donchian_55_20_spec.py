"""StrategySpec wrapper for the V27 Donchian-55/20 trend-follow strategy.

This module exists ONLY so the new ``swing_backtester`` engine
(``packages/research/swing_backtester.py``) can run the V27/V32 strategy
through the same loop as the 5 new V35–V39 swing variants. The signal
logic is unchanged — it delegates 100% to ``core.signals.donchian`` (the
same module ``tools/v27_backtest_2026_06_01.py`` uses).

Why a wrapper:
    Engine B's ``StrategySpec`` interface needs entry_fn/exit_fn/on_bar_fn
    callables with a uniform signature. Donchian's own ``entry_signal``
    takes keyword args (entry_n, sma_regime, ...) that we have to map
    from a params dict; its exit logic (Donchian-exit + chandelier_stop +
    time-in-trade) is composed across three rules that we have to call
    in sequence; and its trailing stop requires per-position
    high_since_entry bookkeeping.

Sanity guarantee:
    Running this spec with ``max_concurrent=6`` and V27 defaults through
    ``swing_backtester.run_swing_backtest`` MUST reproduce V32's number
    (CAGR +2.84%, PF 1.36, MaxDD -7.80%) within ±0.1% / ±0.01. That's
    verified by ``tools/multi_swing_backtest_2026_06_01.py --sanity-check``.
    If the numbers diverge, the engine extraction broke something — the
    wrapper here is a thin pass-through and can be ruled out fast.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from core.signals import donchian
from research.swing_backtester import OpenPosition, StrategySpec


def _entry(
    df_today: pd.DataFrame,
    params: Dict[str, Any],
    last_entry_bar_index: Optional[int],
    context: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    """Wraps ``donchian.entry_signal`` with the V27 default param surface."""
    return donchian.entry_signal(
        df_today,
        side="long",
        entry_n=params.get("entry_n", donchian.DEFAULT_ENTRY_N),
        sma_regime=params.get("sma_regime", donchian.DEFAULT_SMA_REGIME),
        volume_window=params.get("volume_window", donchian.DEFAULT_VOLUME_WINDOW),
        volume_multiplier=params.get("volume_multiplier", donchian.DEFAULT_VOLUME_MULTIPLIER),
        atr_period=params.get("atr_period", donchian.DEFAULT_ATR_PERIOD),
        atr_cap_pct=params.get("atr_cap_pct", donchian.DEFAULT_ATR_CAP_PCT),
        whipsaw_days=params.get("whipsaw_days", donchian.DEFAULT_WHIPSAW_DAYS),
        adx_period=params.get("adx_period", donchian.DEFAULT_ADX_PERIOD),
        adx_min=params.get("adx_min", donchian.DEFAULT_ADX_MIN),
        sma50_period=params.get("sma50_period", donchian.DEFAULT_SMA50_PERIOD),
        last_entry_bar_index=last_entry_bar_index,
    )


def _on_bar(
    position: OpenPosition,
    df_today: pd.DataFrame,
    params: Dict[str, Any],
) -> None:
    """Track high_since_entry for the chandelier trailing stop."""
    today_high = float(df_today["high"].iloc[-1])
    cur = position.state.get("high_since_entry", position.entry_price)
    if today_high > cur:
        position.state["high_since_entry"] = today_high


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
    context: Dict[str, Any],  # v4.1: engine passes per-bar context; unused here
) -> Optional[str]:
    """V27 long-exit gate stack:
        (a) Donchian exit: today_close < rolling_min(low, M)
        (b) Chandelier stop: today_close < high_since_entry - mult*ATR(period)
        (c) Time-in-trade: bars_held > max_time_in_trade_bars
    """
    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    exit_m = int(params.get("exit_m", donchian.DEFAULT_EXIT_M))
    chandelier_mult = float(params.get("chandelier_mult", donchian.DEFAULT_CHANDELIER_MULT))
    atr_period = int(params.get("atr_period", donchian.DEFAULT_ATR_PERIOD))
    max_time = int(params.get("max_time_in_trade_bars", 60))

    # (a) Donchian exit
    ch_low = donchian.rolling_low(df_today, m=exit_m)
    if math.isfinite(ch_low) and today_close < ch_low:
        return "donchian_exit"

    # (b) Chandelier stop
    atr_val = donchian._atr_ewm(df_today, period=atr_period)
    high_since = position.state.get("high_since_entry", position.entry_price)
    if math.isfinite(atr_val) and atr_val > 0:
        chand = high_since - chandelier_mult * atr_val
        if today_close < chand:
            return "chandelier_stop"

    # (c) Time-in-trade
    if (today_pos - position.entry_bar_index) > max_time:
        return "time_in_trade"

    return None


def _initial_state(df_at_entry: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
    return {"high_since_entry": float(df_at_entry["high"].iloc[-1])}


def _initial_stop(df_at_entry: pd.DataFrame, params: Dict[str, Any]) -> float:
    price = float(df_at_entry["close"].iloc[-1])
    atr_val = donchian._atr_ewm(df_at_entry, period=int(params.get("atr_period", 14)))
    mult = float(params.get("chandelier_mult", donchian.DEFAULT_CHANDELIER_MULT))
    if math.isfinite(atr_val) and atr_val > 0:
        return price - mult * atr_val
    return price * 0.92  # -8% fallback


SPEC = StrategySpec(
    name="V35_donchian55_20",
    description="V27/V32 Donchian-55/20 cross-asset trend-follow (engine sanity baseline).",
    required_warmup_bars=220,  # max(200-SMA + guard, 55-day entry + 1, ATR*2)
    entry_fn=_entry,
    exit_fn=_exit,
    initial_state_fn=_initial_state,
    initial_stop_fn=_initial_stop,
    on_bar_fn=_on_bar,
    default_params={
        "entry_n": donchian.DEFAULT_ENTRY_N,                   # 55
        "exit_m": donchian.DEFAULT_EXIT_M,                     # 20
        "sma_regime": donchian.DEFAULT_SMA_REGIME,             # 200
        "volume_window": donchian.DEFAULT_VOLUME_WINDOW,       # 20
        "volume_multiplier": donchian.DEFAULT_VOLUME_MULTIPLIER, # 1.2
        "atr_period": donchian.DEFAULT_ATR_PERIOD,             # 14
        "atr_cap_pct": donchian.DEFAULT_ATR_CAP_PCT,           # 5.0
        "whipsaw_days": donchian.DEFAULT_WHIPSAW_DAYS,         # 10
        "adx_period": donchian.DEFAULT_ADX_PERIOD,             # 14
        "adx_min": donchian.DEFAULT_ADX_MIN,                   # 20
        "sma50_period": donchian.DEFAULT_SMA50_PERIOD,         # 50
        "chandelier_mult": donchian.DEFAULT_CHANDELIER_MULT,   # 3.0
        "max_time_in_trade_bars": 60,
    },
    cost_product="DELIVERY",
)
