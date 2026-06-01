"""V36 — Mean-reversion swing (RSI-extreme reversal in 200-SMA uptrend).

Hypothesis: in a confirmed uptrend (close > 200-SMA), short-term oversold
conditions (RSI(14) < 25) tend to mean-revert within 5–15 trading days.
This is the classic "buy the dip" swing trade, but gated to ONLY trigger
in uptrending names so we're not catching falling knives.

Entry (all must be True):
    1. close[today] > SMA(200)[today]              (regime: long-term up)
    2. RSI(14)[today] < rsi_oversold (default 25)  (short-term oversold)
    3. volume[today] >= 0.8 * mean(volume[-20:])   (basic liquidity)
    4. ATR%(14)[today] <= 6.0%                     (vol cap)
    5. close[today] > 0.97 * SMA(200)              (dip but not freefall;
                                                    if we're >3% below 200-SMA
                                                    the trend is bending)

Exit (any of):
    1. RSI(14)[today] > rsi_overbought (default 55)  (oversold → fair value)
    2. close[today] < initial_stop                   (hard stop, -8% or 2*ATR)
    3. bars_held > max_time_in_trade_bars (15)       (no edge after 3 weeks)

Initial stop: max(2*ATR(14) below entry, entry * 0.92).

This is NOT the same as the v3.0 ``trend_pullback`` strategy (RSI 40–55
band + 50-SMA pullback). Mean-reversion fires when the market PUNISHES a
name briefly in an otherwise healthy trend; trend_pullback fires when a
name COOLS without overcorrecting.

Charter compliance: long-only (charter §3.2 + v3.0 finding #3), CNC
DELIVERY product type (charter §3.1), vol-target sized via the engine.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from research.swing_backtester import OpenPosition, StrategySpec


# ── Charter §3.11 retune-budget defaults (NOT operator-tunable in V36) ──
DEFAULT_RSI_PERIOD = 14
DEFAULT_RSI_OVERSOLD = 25.0
DEFAULT_RSI_OVERBOUGHT = 55.0
DEFAULT_SMA_REGIME = 200
DEFAULT_VOL_WINDOW = 20
DEFAULT_VOL_MIN_RATIO = 0.8
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_CAP_PCT = 6.0
DEFAULT_MAX_PCT_BELOW_SMA200 = 3.0    # close must be within -3% of SMA200
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_STOP_FALLBACK_PCT = 0.92      # -8% if ATR is NaN
DEFAULT_MAX_TIME_BARS = 15


def _rsi(close: pd.Series, period: int = 14) -> float:
    """RSI(14) at the last bar using Wilder smoothing (matches TA-Lib)."""
    if len(close) < period + 1:
        return float("nan")
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1] if avg_loss.iloc[-1] > 0 else float("inf")
    if not np.isfinite(rs):
        return 100.0  # all gains, no losses → fully overbought
    return float(100.0 - (100.0 / (1.0 + rs)))


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
    rsi_period = int(params.get("rsi_period", DEFAULT_RSI_PERIOD))
    rsi_oversold = float(params.get("rsi_oversold", DEFAULT_RSI_OVERSOLD))
    vol_window = int(params.get("vol_window", DEFAULT_VOL_WINDOW))
    vol_min_ratio = float(params.get("vol_min_ratio", DEFAULT_VOL_MIN_RATIO))
    atr_period = int(params.get("atr_period", DEFAULT_ATR_PERIOD))
    atr_cap_pct = float(params.get("atr_cap_pct", DEFAULT_ATR_CAP_PCT))
    max_pct_below_sma = float(params.get("max_pct_below_sma200", DEFAULT_MAX_PCT_BELOW_SMA200))

    needed = max(sma_regime + 1, rsi_period * 3, vol_window + 1, atr_period * 2 + 1)
    if len(df_today) < needed:
        return False, {"reason": f"insufficient_history (have {len(df_today)} need {needed})"}

    close_today = float(df_today["close"].iloc[-1])
    volume_today = float(df_today["volume"].iloc[-1])

    # Gate 1 — regime filter (200-SMA uptrend)
    sma200 = float(df_today["close"].rolling(sma_regime).mean().iloc[-1])
    if not np.isfinite(sma200) or close_today <= sma200:
        return False, {"reason": "regime_filter_failed", "close": close_today, "sma200": sma200}

    # Gate 5 — bounded dip (close must be within max_pct_below_sma200 below SMA200)
    # This is *redundant* with Gate 1 (close > SMA200) when measured strictly,
    # BUT we want to also reject "deep dip" entries below SMA200. Implemented
    # as: close >= SMA200 * (1 - max_pct/100). Always passes when Gate 1 passes.
    # Kept for charter-doc parity even when always True today.
    if close_today < sma200 * (1.0 - max_pct_below_sma / 100.0):
        return False, {"reason": "deep_dip"}

    # Gate 2 — RSI oversold
    rsi_today = _rsi(df_today["close"], period=rsi_period)
    if not np.isfinite(rsi_today) or rsi_today >= rsi_oversold:
        return False, {"reason": "rsi_not_oversold", "rsi": rsi_today, "thresh": rsi_oversold}

    # Gate 3 — volume floor
    vol_mean = float(df_today["volume"].iloc[-vol_window - 1 : -1].mean())
    if not np.isfinite(vol_mean) or vol_mean <= 0 or volume_today < vol_min_ratio * vol_mean:
        return False, {"reason": "volume_too_low", "vol_today": volume_today, "vol_mean": vol_mean}

    # Gate 4 — ATR cap
    atr_val = _atr(df_today, period=atr_period)
    atr_pct = (atr_val / close_today) * 100.0 if close_today > 0 else float("inf")
    if not np.isfinite(atr_pct) or atr_pct > atr_cap_pct:
        return False, {"reason": "atr_too_high", "atr_pct": atr_pct, "cap": atr_cap_pct}

    return True, {
        "rsi": rsi_today,
        "sma200": sma200,
        "atr_pct": atr_pct,
        "volume_ratio": volume_today / vol_mean if vol_mean > 0 else None,
    }


def _exit(
    df_today: pd.DataFrame,
    position: OpenPosition,
    params: Dict[str, Any],
    context: Dict[str, Any],  # v4.1: engine passes per-bar context; unused here
) -> Optional[str]:
    rsi_period = int(params.get("rsi_period", DEFAULT_RSI_PERIOD))
    rsi_overbought = float(params.get("rsi_overbought", DEFAULT_RSI_OVERBOUGHT))
    max_time = int(params.get("max_time_in_trade_bars", DEFAULT_MAX_TIME_BARS))

    today_close = float(df_today["close"].iloc[-1])
    today_pos = len(df_today) - 1

    # (a) hard stop
    if position.initial_stop > 0 and today_close < position.initial_stop:
        return "stop_loss"

    # (b) RSI mean-reverted up to fair value
    rsi_today = _rsi(df_today["close"], period=rsi_period)
    if np.isfinite(rsi_today) and rsi_today > rsi_overbought:
        return "rsi_overbought"

    # (c) time-in-trade
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
    name="V36_mean_reversion_swing",
    description="RSI(14)<25 reversal in 200-SMA uptrend; 2*ATR stop; 15-day timeout.",
    required_warmup_bars=220,
    entry_fn=_entry,
    exit_fn=_exit,
    initial_stop_fn=_initial_stop,
    default_params={
        "rsi_period": DEFAULT_RSI_PERIOD,
        "rsi_oversold": DEFAULT_RSI_OVERSOLD,
        "rsi_overbought": DEFAULT_RSI_OVERBOUGHT,
        "sma_regime": DEFAULT_SMA_REGIME,
        "vol_window": DEFAULT_VOL_WINDOW,
        "vol_min_ratio": DEFAULT_VOL_MIN_RATIO,
        "atr_period": DEFAULT_ATR_PERIOD,
        "atr_cap_pct": DEFAULT_ATR_CAP_PCT,
        "max_pct_below_sma200": DEFAULT_MAX_PCT_BELOW_SMA200,
        "stop_atr_mult": DEFAULT_STOP_ATR_MULT,
        "stop_fallback_pct": DEFAULT_STOP_FALLBACK_PCT,
        "max_time_in_trade_bars": DEFAULT_MAX_TIME_BARS,
    },
    cost_product="DELIVERY",
)
