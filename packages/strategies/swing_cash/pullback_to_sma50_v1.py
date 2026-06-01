"""V37 — Pullback-to-SMA50 swing (classic O'Neil/Minervini bounce trade).

Hypothesis: in a confirmed uptrend (close > 200-SMA), pullbacks to the
50-SMA are buyable when the bounce is confirmed by an up-day. We're not
catching the bottom — we're entering on day-1 of the bounce after the
50-SMA has acted as support.

Entry (all must be True):
    1. close[today] > SMA(200)[today]                (regime: long-term up)
    2. low[t-k:t] touched SMA50 band (within touch_band_pct) at some point
       in the last `lookback_for_touch_bars` (default 5) bars      (pullback proof)
    3. close[today] > SMA50[today]                   (back above 50)
    4. close[today] > open[today]                    (bounce day, up bar)
    5. close[today] > close[yesterday]               (momentum confirm)
    6. volume[today] >= 0.9 * mean(volume[-20:])     (basic liquidity)
    7. ATR%(14)[today] <= 6.0%                       (vol cap)

Exit (any of):
    1. close[today] < SMA50[today]                   (the bounce thesis broke)
    2. close[today] < initial_stop                   (hard 2*ATR stop)
    3. close[today] >= entry_price * (1 + profit_take_pct)  (take +12% gift)
    4. bars_held > max_time_in_trade_bars (30)       (no edge after 6 weeks)

Initial stop: max(2*ATR(14) below entry, entry * 0.92).

Distinct from the v3.0 ``trend_pullback`` strategy: trend_pullback uses
an RSI band (40–55) as the pullback proxy. V37 uses an explicit
50-SMA-touch + bounce-day confirmation, which fires less often but
arguably with better conviction.

Charter compliance: long-only, CNC DELIVERY, vol-target sized via engine.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.swing_backtester import OpenPosition, StrategySpec


DEFAULT_SMA_REGIME = 200
DEFAULT_SMA_SUPPORT = 50
DEFAULT_TOUCH_BAND_PCT = 1.5         # low within ±1.5% of SMA50 counts as touch
DEFAULT_LOOKBACK_TOUCH_BARS = 5
DEFAULT_VOL_WINDOW = 20
DEFAULT_VOL_MIN_RATIO = 0.9
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_CAP_PCT = 6.0
DEFAULT_PROFIT_TAKE_PCT = 0.12       # +12% take-profit (asymmetric vs 2*ATR stop)
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_STOP_FALLBACK_PCT = 0.92
DEFAULT_MAX_TIME_BARS = 30


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


def _entry(
    df_today: pd.DataFrame,
    params: Dict[str, Any],
    last_entry_bar_index: Optional[int],
    context: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    sma_regime = int(params.get("sma_regime", DEFAULT_SMA_REGIME))
    sma_support = int(params.get("sma_support", DEFAULT_SMA_SUPPORT))
    touch_band = float(params.get("touch_band_pct", DEFAULT_TOUCH_BAND_PCT))
    lookback_touch = int(params.get("lookback_for_touch_bars", DEFAULT_LOOKBACK_TOUCH_BARS))
    vol_window = int(params.get("vol_window", DEFAULT_VOL_WINDOW))
    vol_min_ratio = float(params.get("vol_min_ratio", DEFAULT_VOL_MIN_RATIO))
    atr_period = int(params.get("atr_period", DEFAULT_ATR_PERIOD))
    atr_cap_pct = float(params.get("atr_cap_pct", DEFAULT_ATR_CAP_PCT))

    needed = max(sma_regime + 1, sma_support + 2, vol_window + 1, atr_period * 2 + 1)
    if len(df_today) < needed:
        return False, {"reason": f"insufficient_history (have {len(df_today)} need {needed})"}

    close_today = float(df_today["close"].iloc[-1])
    open_today = float(df_today["open"].iloc[-1])
    close_yest = float(df_today["close"].iloc[-2]) if len(df_today) >= 2 else close_today
    volume_today = float(df_today["volume"].iloc[-1])

    # Gate 1 — regime
    sma200_series = df_today["close"].rolling(sma_regime).mean()
    sma200 = float(sma200_series.iloc[-1])
    if not np.isfinite(sma200) or close_today <= sma200:
        return False, {"reason": "regime_filter_failed"}

    # Gate 3 — close above SMA50 today
    sma50_series = df_today["close"].rolling(sma_support).mean()
    sma50_today = float(sma50_series.iloc[-1])
    if not np.isfinite(sma50_today) or close_today <= sma50_today:
        return False, {"reason": "close_below_sma50"}

    # Gate 2 — pullback proof: low in last N bars touched SMA50 band
    touched = False
    for back in range(1, lookback_touch + 1):
        if len(sma50_series) < back + 1:
            break
        low_back = float(df_today["low"].iloc[-back])
        sma_back = float(sma50_series.iloc[-back])
        if not np.isfinite(sma_back):
            continue
        band = sma_back * (touch_band / 100.0)
        if low_back <= sma_back + band:
            touched = True
            break
    if not touched:
        return False, {"reason": "no_sma50_touch_in_lookback"}

    # Gate 4 — up day
    if close_today <= open_today:
        return False, {"reason": "not_up_day"}

    # Gate 5 — momentum confirm
    if close_today <= close_yest:
        return False, {"reason": "no_momentum_confirm"}

    # Gate 6 — volume floor
    vol_mean = float(df_today["volume"].iloc[-vol_window - 1 : -1].mean())
    if not np.isfinite(vol_mean) or vol_mean <= 0 or volume_today < vol_min_ratio * vol_mean:
        return False, {"reason": "volume_too_low"}

    # Gate 7 — ATR cap
    atr_val = _atr(df_today, period=atr_period)
    atr_pct = (atr_val / close_today) * 100.0 if close_today > 0 else float("inf")
    if not np.isfinite(atr_pct) or atr_pct > atr_cap_pct:
        return False, {"reason": "atr_too_high"}

    return True, {
        "sma200": sma200,
        "sma50": sma50_today,
        "atr_pct": atr_pct,
    }


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
) -> Optional[str]:
    sma_support = int(params.get("sma_support", DEFAULT_SMA_SUPPORT))
    profit_take = float(params.get("profit_take_pct", DEFAULT_PROFIT_TAKE_PCT))
    max_time = int(params.get("max_time_in_trade_bars", DEFAULT_MAX_TIME_BARS))

    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    # (a) hard stop
    if position.initial_stop > 0 and today_close < position.initial_stop:
        return "stop_loss"

    # (b) below SMA50 → thesis broke
    if len(df_today) >= sma_support:
        sma50 = float(df_today["close"].rolling(sma_support).mean().iloc[-1])
        if np.isfinite(sma50) and today_close < sma50:
            return "sma50_breach"

    # (c) profit-take
    if today_close >= position.entry_price * (1.0 + profit_take):
        return "profit_take"

    # (d) timeout
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
    name="V37_pullback_to_sma50",
    description="Pullback to 50-SMA in 200-SMA uptrend, bounce-day entry; +12% TP, 2*ATR stop, 30-day timeout.",
    required_warmup_bars=220,
    entry_fn=_entry,
    exit_fn=_exit,
    initial_stop_fn=_initial_stop,
    default_params={
        "sma_regime": DEFAULT_SMA_REGIME,
        "sma_support": DEFAULT_SMA_SUPPORT,
        "touch_band_pct": DEFAULT_TOUCH_BAND_PCT,
        "lookback_for_touch_bars": DEFAULT_LOOKBACK_TOUCH_BARS,
        "vol_window": DEFAULT_VOL_WINDOW,
        "vol_min_ratio": DEFAULT_VOL_MIN_RATIO,
        "atr_period": DEFAULT_ATR_PERIOD,
        "atr_cap_pct": DEFAULT_ATR_CAP_PCT,
        "profit_take_pct": DEFAULT_PROFIT_TAKE_PCT,
        "stop_atr_mult": DEFAULT_STOP_ATR_MULT,
        "stop_fallback_pct": DEFAULT_STOP_FALLBACK_PCT,
        "max_time_in_trade_bars": DEFAULT_MAX_TIME_BARS,
    },
    cost_product="DELIVERY",
)
