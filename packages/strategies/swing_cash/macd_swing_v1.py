"""V39 — MACD swing (bullish cross in 200-SMA uptrend).

Hypothesis: a fresh MACD bullish cross-over in a confirmed uptrend
captures the start of a new short-to-medium-term up-leg. Combined with
the 200-SMA regime filter, the strategy ignores cross-overs in
downtrending stocks (which would be high-quality whipsaws).

Entry (all must be True):
    1. close[today] > SMA(200)[today]                (regime: long-term up)
    2. MACD(12,26,9) BULLISH cross in last 2 bars    (signal line was below
                                                      MACD, now above)
    3. MACD line > 0                                 (above zero-line — adds
                                                      strength filter; avoids
                                                      crosses deep in negative)
    4. MACD histogram[today] > 0                     (confirmation; histogram
                                                      is positive when cross
                                                      is genuine, can be
                                                      negative on a stale cross)
    5. volume[today] >= 0.9 * mean(volume[-20:])     (basic liquidity)
    6. ATR%(14)[today] <= 6.0%                       (vol cap)

Exit (any of):
    1. MACD BEARISH cross (signal line crosses back ABOVE MACD line)
    2. close[today] < initial_stop                   (hard 2*ATR stop)
    3. bars_held > max_time_in_trade_bars (30)       (~6 weeks timeout)

Initial stop: max(2*ATR(14) below entry, entry * 0.92).

This strategy is INTENTIONALLY similar to V36 (mean-reversion) in
risk/sizing but uses MOMENTUM (MACD cross) instead of MEAN-REVERSION
(RSI oversold) as the trigger. The two should fire on different
market regimes — V36 on dips, V39 on breakouts — so their P&L curves
should be lowly-correlated.

Charter compliance: long-only, CNC DELIVERY, vol-target sized via engine.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.swing_backtester import OpenPosition, StrategySpec


DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_SMA_REGIME = 200
DEFAULT_VOL_WINDOW = 20
DEFAULT_VOL_MIN_RATIO = 0.9
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_CAP_PCT = 6.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_STOP_FALLBACK_PCT = 0.92
DEFAULT_MAX_TIME_BARS = 30
DEFAULT_CROSS_LOOKBACK_BARS = 2  # cross must have happened in last N bars


def _macd_series(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram) — full series."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


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
    fast = int(params.get("macd_fast", DEFAULT_MACD_FAST))
    slow = int(params.get("macd_slow", DEFAULT_MACD_SLOW))
    sig_p = int(params.get("macd_signal", DEFAULT_MACD_SIGNAL))
    sma_regime = int(params.get("sma_regime", DEFAULT_SMA_REGIME))
    vol_window = int(params.get("vol_window", DEFAULT_VOL_WINDOW))
    vol_min_ratio = float(params.get("vol_min_ratio", DEFAULT_VOL_MIN_RATIO))
    atr_period = int(params.get("atr_period", DEFAULT_ATR_PERIOD))
    atr_cap_pct = float(params.get("atr_cap_pct", DEFAULT_ATR_CAP_PCT))
    cross_lookback = int(params.get("cross_lookback_bars", DEFAULT_CROSS_LOOKBACK_BARS))

    needed = max(sma_regime + 1, slow + sig_p + 5, vol_window + 1, atr_period * 2 + 1)
    if len(df_today) < needed:
        return False, {"reason": f"insufficient_history (have {len(df_today)} need {needed})"}

    close_today = float(df_today["close"].iloc[-1])
    volume_today = float(df_today["volume"].iloc[-1])

    # Gate 1 — regime
    sma200 = float(df_today["close"].rolling(sma_regime).mean().iloc[-1])
    if not np.isfinite(sma200) or close_today <= sma200:
        return False, {"reason": "regime_filter_failed"}

    # Compute MACD series (we need last `cross_lookback + 1` bars).
    macd, sig, hist = _macd_series(df_today["close"], fast=fast, slow=slow, signal=sig_p)
    if len(macd) < cross_lookback + 1:
        return False, {"reason": "macd_insufficient"}

    macd_today = float(macd.iloc[-1])
    hist_today = float(hist.iloc[-1])

    # Gate 3 — MACD line > 0
    if not np.isfinite(macd_today) or macd_today <= 0:
        return False, {"reason": "macd_not_positive", "macd": macd_today}

    # Gate 4 — histogram positive
    if not np.isfinite(hist_today) or hist_today <= 0:
        return False, {"reason": "macd_hist_not_positive", "hist": hist_today}

    # Gate 2 — bullish cross in last `cross_lookback` bars (MACD crossed
    # above signal). A cross at bar t means hist[t-1] <= 0 AND hist[t] > 0.
    crossed = False
    for back in range(1, cross_lookback + 1):
        if len(hist) < back + 1:
            break
        h_prev = float(hist.iloc[-back - 1])
        h_now = float(hist.iloc[-back])
        if np.isfinite(h_prev) and np.isfinite(h_now) and h_prev <= 0 and h_now > 0:
            crossed = True
            break
    if not crossed:
        return False, {"reason": "no_bullish_cross_in_lookback"}

    # Gate 5 — volume floor
    vol_mean = float(df_today["volume"].iloc[-vol_window - 1 : -1].mean())
    if not np.isfinite(vol_mean) or vol_mean <= 0 or volume_today < vol_min_ratio * vol_mean:
        return False, {"reason": "volume_too_low"}

    # Gate 6 — ATR cap
    atr_val = _atr(df_today, period=atr_period)
    atr_pct = (atr_val / close_today) * 100.0 if close_today > 0 else float("inf")
    if not np.isfinite(atr_pct) or atr_pct > atr_cap_pct:
        return False, {"reason": "atr_too_high"}

    return True, {
        "macd": macd_today,
        "hist": hist_today,
        "sma200": sma200,
        "atr_pct": atr_pct,
    }


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
) -> Optional[str]:
    fast = int(params.get("macd_fast", DEFAULT_MACD_FAST))
    slow = int(params.get("macd_slow", DEFAULT_MACD_SLOW))
    sig_p = int(params.get("macd_signal", DEFAULT_MACD_SIGNAL))
    max_time = int(params.get("max_time_in_trade_bars", DEFAULT_MAX_TIME_BARS))

    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    # (a) hard stop
    if position.initial_stop > 0 and today_close < position.initial_stop:
        return "stop_loss"

    # (b) MACD bearish cross — hist crossed from > 0 to <= 0 today (or yesterday)
    if len(df_today) >= slow + sig_p + 2:
        _, _, hist = _macd_series(df_today["close"], fast=fast, slow=slow, signal=sig_p)
        if len(hist) >= 2:
            h_prev = float(hist.iloc[-2])
            h_now = float(hist.iloc[-1])
            if np.isfinite(h_prev) and np.isfinite(h_now) and h_prev > 0 and h_now <= 0:
                return "macd_bearish_cross"

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
    name="V39_macd_swing",
    description="MACD(12,26,9) bullish cross in 200-SMA uptrend; 2*ATR stop, 30-day timeout.",
    required_warmup_bars=220,
    entry_fn=_entry,
    exit_fn=_exit,
    initial_stop_fn=_initial_stop,
    default_params={
        "macd_fast": DEFAULT_MACD_FAST,
        "macd_slow": DEFAULT_MACD_SLOW,
        "macd_signal": DEFAULT_MACD_SIGNAL,
        "sma_regime": DEFAULT_SMA_REGIME,
        "vol_window": DEFAULT_VOL_WINDOW,
        "vol_min_ratio": DEFAULT_VOL_MIN_RATIO,
        "atr_period": DEFAULT_ATR_PERIOD,
        "atr_cap_pct": DEFAULT_ATR_CAP_PCT,
        "stop_atr_mult": DEFAULT_STOP_ATR_MULT,
        "stop_fallback_pct": DEFAULT_STOP_FALLBACK_PCT,
        "max_time_in_trade_bars": DEFAULT_MAX_TIME_BARS,
        "cross_lookback_bars": DEFAULT_CROSS_LOOKBACK_BARS,
    },
    cost_product="DELIVERY",
)
