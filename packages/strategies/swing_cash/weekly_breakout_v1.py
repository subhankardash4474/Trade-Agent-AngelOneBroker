"""V38 — Weekly Donchian breakout (longer-timeframe trend capture).

Hypothesis: V27's Donchian-55/20 on DAILY bars captures medium-term
trends. A longer-timeframe version on WEEKLY bars captures the bigger,
slower trends with fewer trades and lower noise. The trade-off is
fewer fills + later entries + later exits — designed to complement,
not replace, the daily version.

Entry (all must be True; computed on WEEKLY-resampled bars):
    1. weekly_close[this_week] > rolling_max(weekly_high, 20 weeks)  (~5mo breakout)
    2. weekly_close[this_week] > SMA(40 weeks, weekly_close)         (regime: long-term up)
    3. weekly_volume[this_week] >= 1.1 * mean(weekly_volume[-13:])   (~3-month vol avg)

Daily-bar reality: the engine ticks on daily bars, so we only EVALUATE
the entry on the LAST trading day of each week (Friday or last trading
day of the week if a market holiday). On non-Friday days the entry_fn
returns (False, {"reason": "not_week_end"}). This keeps the engine's
day-loop unchanged while honoring the weekly-bar semantic.

Exit (any of):
    1. weekly_close < rolling_min(weekly_low, 10 weeks)              (Donchian exit)
    2. close[today] < initial_stop                                    (hard stop)
    3. bars_held > max_time_in_trade_bars (120 daily ≈ 6mo)           (timeout)

Initial stop: max(2.5*ATR(14) daily below entry, entry * 0.88).
Wider stop than V36/V37 since weekly-bar moves are larger.

Charter compliance: long-only, CNC DELIVERY, vol-target sized via engine.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.swing_backtester import OpenPosition, StrategySpec


DEFAULT_WEEKLY_ENTRY_N = 20      # 20-week Donchian entry
DEFAULT_WEEKLY_EXIT_M = 10       # 10-week Donchian exit
DEFAULT_WEEKLY_SMA_REGIME = 40   # 40-week (~200-trading-day) regime SMA on weekly
DEFAULT_WEEKLY_VOL_WINDOW = 13   # ~3 months of weeks
DEFAULT_WEEKLY_VOL_MIN_RATIO = 1.1
DEFAULT_ATR_PERIOD = 14          # daily ATR for sizing/stop
DEFAULT_STOP_ATR_MULT = 2.5
DEFAULT_STOP_FALLBACK_PCT = 0.88
DEFAULT_MAX_TIME_BARS = 120      # ~6 months of trading days


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


def _to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly (Friday-anchored, standard).

    Uses ``W-FRI`` so each weekly bar's timestamp is the Friday of the
    week. Pandas handles partial weeks at the boundary by dropping the
    incomplete leading/trailing week — which is exactly what we want
    (entry_fn just checks whether today IS the last day of the latest
    complete weekly bar).
    """
    return df.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(how="any")


def _is_week_end(df_today: pd.DataFrame) -> bool:
    """True when today is the LAST trading day of the calendar week.

    Looking ahead is not possible inside a backtest, so we use the
    proxy: today is week-end iff the next bar (if any) belongs to a
    different week. Since we don't have the next bar yet, we use a
    simpler safe approximation: today's weekday is Friday (weekday() == 4)
    OR today is the last available bar (end-of-data trailing edge).
    Mid-week holidays may push the actual week-end to Thursday — those
    weeks will simply not produce entries, which is a small bias toward
    missing signals (False negatives) but not a forward-looking bias.
    """
    today = df_today.index[-1]
    return today.weekday() == 4  # Friday


def _entry(
    df_today: pd.DataFrame,
    params: Dict[str, Any],
    last_entry_bar_index: Optional[int],
    context: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    entry_n_weeks = int(params.get("weekly_entry_n", DEFAULT_WEEKLY_ENTRY_N))
    sma_regime_weeks = int(params.get("weekly_sma_regime", DEFAULT_WEEKLY_SMA_REGIME))
    vol_window_weeks = int(params.get("weekly_vol_window", DEFAULT_WEEKLY_VOL_WINDOW))
    vol_min_ratio = float(params.get("weekly_vol_min_ratio", DEFAULT_WEEKLY_VOL_MIN_RATIO))

    # Only evaluate on week-end bars.
    if not _is_week_end(df_today):
        return False, {"reason": "not_week_end"}

    weekly = _to_weekly(df_today)
    needed_weeks = max(entry_n_weeks + 1, sma_regime_weeks + 1, vol_window_weeks + 1)
    if len(weekly) < needed_weeks:
        return False, {"reason": f"insufficient_weekly_history (have {len(weekly)} need {needed_weeks})"}

    wclose_today = float(weekly["close"].iloc[-1])
    wvol_today = float(weekly["volume"].iloc[-1])

    # Gate 1 — weekly Donchian breakout
    ch_high = float(weekly["high"].iloc[-entry_n_weeks - 1 : -1].max())
    if not np.isfinite(ch_high) or wclose_today <= ch_high:
        return False, {
            "reason": "weekly_no_breakout",
            "weekly_close": wclose_today,
            "channel_high": ch_high,
        }

    # Gate 2 — weekly regime filter
    sma_w = float(weekly["close"].rolling(sma_regime_weeks).mean().iloc[-1])
    if not np.isfinite(sma_w) or wclose_today <= sma_w:
        return False, {"reason": "weekly_regime_failed"}

    # Gate 3 — weekly volume confirm
    vol_mean = float(weekly["volume"].iloc[-vol_window_weeks - 1 : -1].mean())
    if not np.isfinite(vol_mean) or vol_mean <= 0 or wvol_today < vol_min_ratio * vol_mean:
        return False, {"reason": "weekly_volume_too_low"}

    return True, {
        "weekly_close": wclose_today,
        "channel_high": ch_high,
        "weekly_sma": sma_w,
    }


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
    context: Dict[str, Any],  # v4.1: engine passes per-bar context; unused here
) -> Optional[str]:
    exit_m_weeks = int(params.get("weekly_exit_m", DEFAULT_WEEKLY_EXIT_M))
    max_time = int(params.get("max_time_in_trade_bars", DEFAULT_MAX_TIME_BARS))

    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    # (a) hard stop — checked DAILY (not gated on week-end) so risk is
    # cut at the earliest opportunity rather than waiting for Friday.
    if position.initial_stop > 0 and today_close < position.initial_stop:
        return "stop_loss"

    # (b) weekly Donchian exit — only evaluated on week-end bars (mirror
    # of entry cadence)
    if _is_week_end(df_today):
        weekly = _to_weekly(df_today)
        if len(weekly) >= exit_m_weeks + 1:
            ch_low = float(weekly["low"].iloc[-exit_m_weeks - 1 : -1].min())
            if np.isfinite(ch_low) and float(weekly["close"].iloc[-1]) < ch_low:
                return "weekly_donchian_exit"

    # (c) timeout
    if (today_pos - position.entry_bar_index) > max_time:
        return "time_in_trade"

    return None


def _initial_stop(df_at_entry: pd.DataFrame, params: Dict[str, Any]) -> float:
    price = float(df_at_entry["close"].iloc[-1])
    atr_val = _atr(df_at_entry, period=int(params.get("atr_period", DEFAULT_ATR_PERIOD)))
    mult = float(params.get("stop_atr_mult", DEFAULT_STOP_ATR_MULT))
    fallback = float(params.get("stop_fallback_pct", DEFAULT_STOP_FALLBACK_PCT))
    if math.isfinite(atr_val) and atr_val > 0:
        return max(price - mult * atr_val, price * fallback)
    return price * fallback


SPEC = StrategySpec(
    name="V38_weekly_breakout",
    description="Weekly Donchian-20/10 breakout w/ 40-week regime; 2.5*ATR daily stop, 120-day timeout.",
    # 220 daily bars ~= 44 weeks; enough for 40-week SMA + 20-week entry + 1
    # bar guard. The 220 figure also matches the engine's typical warmup.
    required_warmup_bars=220,
    entry_fn=_entry,
    exit_fn=_exit,
    initial_stop_fn=_initial_stop,
    default_params={
        "weekly_entry_n": DEFAULT_WEEKLY_ENTRY_N,
        "weekly_exit_m": DEFAULT_WEEKLY_EXIT_M,
        "weekly_sma_regime": DEFAULT_WEEKLY_SMA_REGIME,
        "weekly_vol_window": DEFAULT_WEEKLY_VOL_WINDOW,
        "weekly_vol_min_ratio": DEFAULT_WEEKLY_VOL_MIN_RATIO,
        "atr_period": DEFAULT_ATR_PERIOD,
        "stop_atr_mult": DEFAULT_STOP_ATR_MULT,
        "stop_fallback_pct": DEFAULT_STOP_FALLBACK_PCT,
        "max_time_in_trade_bars": DEFAULT_MAX_TIME_BARS,
    },
    cost_product="DELIVERY",
)
