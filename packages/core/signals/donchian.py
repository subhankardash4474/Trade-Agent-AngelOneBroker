"""Donchian-channel breakout signals (charter v4 §3.2).

V27 entry / exit logic, factored out of the strategy module so the same
primitives can be reused by V28+ retunes and by Mode B's futures
trend-pullback (charter §4.3, F&O paper-mode).

Functions:
    rolling_high(df, n): max(high) over the LAST n bars EXCLUDING today.
        This is the standard "channel that today must break out of".
    rolling_low(df, m): min(low) over the LAST m bars EXCLUDING today.
    entry_signal(df, ...): full V27 long-entry gate stack.
    exit_signal(df, ...): full V27 long-exit gate (Donchian + trailing stop).
    chandelier_stop(df, period, multiplier): trailing stop value (charter §3.4).

Conventions
-----------
* `df` is an OHLCV DataFrame with columns: `open`, `high`, `low`, `close`,
  `volume`. DatetimeIndex (any timezone; daily bars assumed for V27).
* All functions return scalars (for `entry_signal` / `exit_signal`: bool)
  computed AS OF the LAST bar in `df`. The caller is responsible for the
  rolling-window slice.
* SHORT side: NOT implemented per charter §3.2 + v3.0 finding #3
  ("Indian-equity short side structurally -EV at retail"). Calling
  `entry_signal(..., side="short")` raises NotImplementedError loudly.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd


# ── Defaults (charter §3.2; not operator-tunable in V27) ──
DEFAULT_ENTRY_N = 55
DEFAULT_EXIT_M = 20
DEFAULT_SMA_REGIME = 200
DEFAULT_VOLUME_WINDOW = 20
DEFAULT_VOLUME_MULTIPLIER = 1.2
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_CAP_PCT = 5.0
DEFAULT_WHIPSAW_DAYS = 10
DEFAULT_ADX_PERIOD = 14
DEFAULT_ADX_MIN = 20
DEFAULT_SMA50_PERIOD = 50
DEFAULT_CHANDELIER_MULT = 3.0


def rolling_high(df: pd.DataFrame, n: int) -> float:
    """Max(high) over the last `n` bars EXCLUDING the current bar.

    Returns NaN if insufficient history.

    Example for the V27 entry rule `close[today] > max(high[t-N:t])`:

        ch_high = rolling_high(df, n=55)
        if df['close'].iloc[-1] > ch_high:
            # breakout
    """
    if len(df) < n + 1:
        return float("nan")
    return float(df["high"].iloc[-n - 1 : -1].max())


def rolling_low(df: pd.DataFrame, m: int) -> float:
    """Min(low) over the last `m` bars EXCLUDING the current bar."""
    if len(df) < m + 1:
        return float("nan")
    return float(df["low"].iloc[-m - 1 : -1].min())


def _atr_ewm(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> float:
    """ATR(period) using EWM (matches `FeatureEngine` + `BaseStrategy._atr`).

    Returns the LAST bar's ATR value (in price units). NaN if insufficient.
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
    atr = tr.ewm(span=period, adjust=False).mean()
    val = float(atr.iloc[-1])
    return val if np.isfinite(val) else float("nan")


def _adx_wilder(
    df: pd.DataFrame, period: int = DEFAULT_ADX_PERIOD
) -> float:
    """ADX(period) Wilder smoothing; returns scalar at the last bar."""
    if len(df) < period * 2 + 1:
        return float("nan")
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100.0
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    val = float(adx.iloc[-1])
    return val if np.isfinite(val) else float("nan")


def chandelier_stop(
    df: pd.DataFrame,
    entry_index: int,
    period: int = DEFAULT_ATR_PERIOD,
    multiplier: float = DEFAULT_CHANDELIER_MULT,
) -> float:
    """Compute Chandelier exit for a long position (charter §3.4).

        chandelier_stop = max(high since entry) - multiplier * ATR(period)

    Args:
        df: full OHLCV history up to and including the current bar.
        entry_index: positional index in `df` at which the long was opened.
            Use `df.index.get_loc(entry_timestamp)` to convert from
            timestamp-keyed index.
        period: ATR lookback. Default 14 per charter §3.4.
        multiplier: ATR multiplier. Default 3.0; classic CTA range 2.5-3.5.

    Returns:
        Stop price; NaN if insufficient history.
    """
    if entry_index < 0 or entry_index >= len(df):
        return float("nan")
    atr_val = _atr_ewm(df, period=period)
    if not np.isfinite(atr_val):
        return float("nan")
    high_since_entry = float(df["high"].iloc[entry_index:].max())
    return high_since_entry - multiplier * atr_val


def entry_signal(
    df: pd.DataFrame,
    *,
    side: str = "long",
    entry_n: int = DEFAULT_ENTRY_N,
    sma_regime: int = DEFAULT_SMA_REGIME,
    volume_window: int = DEFAULT_VOLUME_WINDOW,
    volume_multiplier: float = DEFAULT_VOLUME_MULTIPLIER,
    atr_period: int = DEFAULT_ATR_PERIOD,
    atr_cap_pct: float = DEFAULT_ATR_CAP_PCT,
    whipsaw_days: int = DEFAULT_WHIPSAW_DAYS,
    adx_period: int = DEFAULT_ADX_PERIOD,
    adx_min: float = DEFAULT_ADX_MIN,
    sma50_period: int = DEFAULT_SMA50_PERIOD,
    last_entry_bar_index: int | None = None,
) -> Tuple[bool, dict]:
    """V27 long-entry gate stack (charter v4 §3.2).

    Returns:
        (fires, diagnostics) — `fires` is True iff ALL gates pass.
        `diagnostics` is a dict of the intermediate values + per-gate
        pass/fail, useful for `audit_signal` logging and tests.

    Side note: SHORT entries are explicitly disabled in V27 per charter
    §3.2 and v3.0 finding #3. Calling with `side="short"` raises.

    All conditions (must ALL be True):
      1. close[today] > max(high[t-N:t])               (Donchian breakout)
      2. close[today] > SMA(200)[today]                (regime up)
      3. volume[today] >= 1.2 * mean(volume[t-20:t])   (volume confirm)
      4. ATR%(14)[today] <= 5.0%                       (vol cap)
      5. days_since_last_entry_in_same_symbol >= 10    (whipsaw guard)
      6. ADX(14)[today] >= 20                          (trending env)
      7. slope_50d_SMA > 0                             (medium-term up)
    """
    if side != "long":
        raise NotImplementedError(
            f"V27 charter §3.2 disables side='{side}'. Only 'long' is supported. "
            "(Per v3.0 finding #3: Indian-equity short side structurally -EV "
            "at retail. Re-enabling requires a v5 charter.)"
        )

    diag: dict = {
        "fires": False,
        "gates": {},
        "reason": "ok",
    }

    needed = max(entry_n + 1, sma_regime + 1, volume_window + 1, atr_period * 2 + 1, sma50_period + 2)
    if len(df) < needed:
        diag["reason"] = f"insufficient_history (have {len(df)} need {needed})"
        return False, diag

    close_today = float(df["close"].iloc[-1])
    volume_today = float(df["volume"].iloc[-1])

    # Gate 1 — Donchian breakout
    ch_high = rolling_high(df, n=entry_n)
    g1 = close_today > ch_high
    diag["gates"]["donchian_breakout"] = {
        "pass": bool(g1),
        "close": close_today,
        "channel_high": float(ch_high) if np.isfinite(ch_high) else None,
    }

    # Gate 2 — regime filter (200-SMA)
    sma200 = float(df["close"].rolling(sma_regime).mean().iloc[-1])
    g2 = close_today > sma200 if np.isfinite(sma200) else False
    diag["gates"]["regime_filter_sma200"] = {
        "pass": bool(g2),
        "sma200": sma200 if np.isfinite(sma200) else None,
    }

    # Gate 3 — volume confirm
    vol_mean = float(df["volume"].iloc[-volume_window - 1 : -1].mean())
    g3 = volume_today >= volume_multiplier * vol_mean if np.isfinite(vol_mean) and vol_mean > 0 else False
    diag["gates"]["volume_confirm"] = {
        "pass": bool(g3),
        "volume_today": volume_today,
        "volume_mean_20d": vol_mean if np.isfinite(vol_mean) else None,
        "required_ratio": volume_multiplier,
    }

    # Gate 4 — ATR cap
    atr_val = _atr_ewm(df, period=atr_period)
    atr_pct = (atr_val / close_today) * 100.0 if close_today > 0 else float("inf")
    g4 = bool(np.isfinite(atr_pct) and atr_pct <= atr_cap_pct)
    diag["gates"]["atr_cap"] = {
        "pass": g4,
        "atr_pct": float(atr_pct) if np.isfinite(atr_pct) else None,
        "cap_pct": atr_cap_pct,
    }

    # Gate 5 — whipsaw guard
    if last_entry_bar_index is None:
        g5 = True  # no prior entry recorded → no whipsaw concern
        gap = None
    else:
        gap = (len(df) - 1) - last_entry_bar_index
        g5 = gap >= whipsaw_days
    diag["gates"]["whipsaw_guard"] = {
        "pass": bool(g5),
        "bars_since_last_entry": gap,
        "min_required": whipsaw_days,
    }

    # Gate 6 — ADX trending env
    adx_val = _adx_wilder(df, period=adx_period)
    g6 = bool(np.isfinite(adx_val) and adx_val >= adx_min)
    diag["gates"]["adx_trending"] = {
        "pass": g6,
        "adx_14": float(adx_val) if np.isfinite(adx_val) else None,
        "min_required": adx_min,
    }

    # Gate 7 — 50-SMA slope up
    sma50_series = df["close"].rolling(sma50_period).mean()
    if len(sma50_series) >= 2 and np.isfinite(sma50_series.iloc[-1]) and np.isfinite(sma50_series.iloc[-2]):
        slope = float(sma50_series.iloc[-1] - sma50_series.iloc[-2])
        g7 = slope > 0
    else:
        slope = None
        g7 = False
    diag["gates"]["sma50_slope_up"] = {
        "pass": bool(g7),
        "slope_1d": slope,
    }

    fires = g1 and g2 and g3 and g4 and g5 and g6 and g7
    if not fires:
        failing = [k for k, v in diag["gates"].items() if not v["pass"]]
        diag["reason"] = f"failed_gates: {failing}"

    diag["fires"] = bool(fires)
    return bool(fires), diag


def exit_signal(
    df: pd.DataFrame,
    *,
    exit_m: int = DEFAULT_EXIT_M,
    entry_bar_index: int | None = None,
    max_time_in_trade_bars: int = 60,
    chandelier_period: int = DEFAULT_ATR_PERIOD,
    chandelier_multiplier: float = DEFAULT_CHANDELIER_MULT,
) -> Tuple[bool, dict]:
    """V27 long-exit gate stack (charter v4 §3.2 + §3.4).

    Exit fires if ANY of:
      1. close[today] < min(low[t-M:t])               (Donchian exit)
      2. close[today] < chandelier_stop                (trailing stop)
      3. time_in_trade > 60 trading days               (forced exit)

    Args:
        df: full OHLCV history including the current bar.
        exit_m: Donchian exit channel period. Default 20.
        entry_bar_index: positional index of the entry bar; required for
            the trailing stop and the time-in-trade check. If None, those
            two gates are skipped (so only the Donchian exit fires).
        max_time_in_trade_bars: forced-exit threshold. Default 60.
        chandelier_period: ATR period for trailing stop. Default 14.
        chandelier_multiplier: ATR multiplier. Default 3.0.

    Returns:
        (fires, diagnostics)
    """
    diag: dict = {
        "fires": False,
        "gates": {},
        "reason": "ok",
    }

    if len(df) < exit_m + 1:
        diag["reason"] = f"insufficient_history (have {len(df)} need {exit_m + 1})"
        return False, diag

    close_today = float(df["close"].iloc[-1])

    # Gate 1 — Donchian exit
    ch_low = rolling_low(df, m=exit_m)
    g1 = bool(np.isfinite(ch_low) and close_today < ch_low)
    diag["gates"]["donchian_exit"] = {
        "fires": g1,
        "close": close_today,
        "channel_low": float(ch_low) if np.isfinite(ch_low) else None,
    }

    # Gate 2 — chandelier trailing stop
    g2 = False
    chandelier_val = None
    if entry_bar_index is not None and 0 <= entry_bar_index < len(df):
        chandelier_val = chandelier_stop(
            df, entry_index=entry_bar_index,
            period=chandelier_period, multiplier=chandelier_multiplier,
        )
        if np.isfinite(chandelier_val):
            g2 = close_today < chandelier_val
    diag["gates"]["chandelier_stop"] = {
        "fires": bool(g2),
        "stop_price": float(chandelier_val) if chandelier_val is not None and np.isfinite(chandelier_val) else None,
        "close": close_today,
    }

    # Gate 3 — time-in-trade forced exit
    g3 = False
    time_in_trade = None
    if entry_bar_index is not None:
        time_in_trade = (len(df) - 1) - entry_bar_index
        g3 = time_in_trade > max_time_in_trade_bars
    diag["gates"]["max_time_in_trade"] = {
        "fires": bool(g3),
        "bars": time_in_trade,
        "max_bars": max_time_in_trade_bars,
    }

    fires = g1 or g2 or g3
    if fires:
        firing = [k for k, v in diag["gates"].items() if v["fires"]]
        diag["reason"] = f"fires: {firing}"
    diag["fires"] = bool(fires)
    return bool(fires), diag


__all__ = [
    "rolling_high",
    "rolling_low",
    "chandelier_stop",
    "entry_signal",
    "exit_signal",
    # Defaults (importable for tests / variant manifests)
    "DEFAULT_ENTRY_N",
    "DEFAULT_EXIT_M",
    "DEFAULT_SMA_REGIME",
    "DEFAULT_VOLUME_WINDOW",
    "DEFAULT_VOLUME_MULTIPLIER",
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_ATR_CAP_PCT",
    "DEFAULT_WHIPSAW_DAYS",
    "DEFAULT_ADX_PERIOD",
    "DEFAULT_ADX_MIN",
    "DEFAULT_SMA50_PERIOD",
    "DEFAULT_CHANDELIER_MULT",
]
