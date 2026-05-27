"""
Supertrend Follow Strategy
Follows trend direction signaled by Supertrend indicator flips.
Uses ATR-based stops that adapt to market volatility.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class SupertrendFollow(BaseStrategy):
    """
    Supertrend trend-following strategy.

    BUY when Supertrend flips from downtrend to uptrend (direction: -1 -> 1).
    SELL when Supertrend flips from uptrend to downtrend (direction: 1 -> -1).
    Confirmed by ADX > 25 to avoid choppy markets.

    Parameters:
        period: ATR period for Supertrend calculation (default 10).
        multiplier: ATR multiplier for bands (default 3.0).
        adx_threshold: Minimum ADX to confirm trend strength (default 25).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "period": 10,
            "multiplier": 3.0,
            "adx_threshold": 25,
            "timeframe": "5min",
            # 2026-05-08: trend filter added after CAMS (+20% above 50d SMA)
            # was shorted by this strategy, hit SL, lost Rs 112. The intraday
            # supertrend flip was real but fought the daily uptrend. Block when
            # entry side fights the 50d daily SMA by more than this %.
            # Set to None to disable the filter.
            "trend_filter_pct": 5.0,
        }
        merged = {**defaults, **params}
        super().__init__(name="supertrend_follow", params=merged)

        self.period: int = merged["period"]
        self.multiplier: float = merged["multiplier"]
        self.adx_threshold: float = merged["adx_threshold"]
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )
        # P-03 (perf 2026-05-27): per-event ATR cache keyed by (id(df), period).
        # _compute_atr is called twice per generate_signal with the same df +
        # period (inside _compute_supertrend AND for atr_val at SL/TP sizing
        # time). Caching by frame identity collapses the two calls into one
        # compute + one cache-hit within a single event. The cache invalidates
        # automatically when the caller passes a different DataFrame (next
        # bar's slice has a new id()).
        self._atr_cache_key: Optional[tuple] = None
        self._atr_cache_value: Optional[pd.Series] = None

    @property
    def required_history_bars(self) -> int:
        return max(self.period * 3, 30)

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def _compute_atr_cached(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Per-event ATR cache (P-03, 2026-05-27).

        Returns the same Series ``_compute_atr`` would produce, but caches
        the result keyed by ``(id(df), period)`` so the second call within
        the same ``generate_signal`` invocation is a dictionary lookup
        instead of an O(n) recompute. Behavior-preserving: any caller that
        passes a different DataFrame instance (e.g. the next event's slice)
        misses the cache and falls through to ``_compute_atr``.
        """
        key = (id(df), period)
        if self._atr_cache_key == key and self._atr_cache_value is not None:
            return self._atr_cache_value
        result = self._compute_atr(df, period)
        self._atr_cache_key = key
        self._atr_cache_value = result
        return result

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0

        tr = pd.concat([
            high - low, (high - close.shift()).abs(), (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(span=period, adjust=False).mean()

    def _compute_supertrend(self, df: pd.DataFrame) -> tuple:
        """Returns (supertrend_values, direction_series).

        P-03 (perf 2026-05-27): the previous implementation drove a
        Python ``for`` loop with ``pd.Series.iloc[i] = ...`` writes,
        which is O(n) but with a multi-microsecond pandas BlockManager
        cost per element. This rewrite preserves the exact algorithm
        (same scalar comparisons, same trailing-band carry-forward,
        same direction-flip semantics) but operates on numpy arrays, so
        each iteration is a memory-direct scalar op instead of a
        BlockManager lookup. On a 1500-bar slice this brings supertrend
        compute from ~12 ms to ~0.25 ms (~50x faster). Output is
        byte-identical to the loop version -- see
        ``tests/unit/test_supertrend_vectorized.py``.
        """
        atr_arr = self._compute_atr_cached(df, self.period).to_numpy()
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()

        n = close.shape[0]
        hl2 = (high + low) * 0.5
        upper = hl2 + self.multiplier * atr_arr
        lower = hl2 - self.multiplier * atr_arr

        # The loop mutates upper/lower in-place to carry the trailing
        # band forward within a single direction segment. We work on
        # copies so we never alias the input arrays.
        upper = upper.astype(np.float64, copy=True)
        lower = lower.astype(np.float64, copy=True)

        direction = np.ones(n, dtype=np.int64)
        st = np.full(n, np.nan, dtype=np.float64)

        for i in range(1, n):
            if close[i] > upper[i - 1]:
                direction[i] = 1
            elif close[i] < lower[i - 1]:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]

            if direction[i] == 1:
                if direction[i - 1] == 1:
                    if lower[i - 1] > lower[i]:
                        lower[i] = lower[i - 1]
                st[i] = lower[i]
            else:
                if direction[i - 1] == -1:
                    if upper[i - 1] < upper[i]:
                        upper[i] = upper[i - 1]
                st[i] = upper[i]

        return (
            pd.Series(st, index=df.index),
            pd.Series(direction, index=df.index),
        )

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        # P-03 (perf 2026-05-27): dropped ``df = data.copy()``. The old
        # code copied the entire OHLCV frame just to write three derived
        # columns (supertrend, st_dir, adx) that were only consumed
        # locally via ``.iloc[-1]`` / ``.iloc[-2]``. We now hold the
        # derived series as plain local variables -- zero copies, zero
        # caller-frame mutation. ``_make_signal`` only reads
        # ``data["close"]`` and ``data.index``, so passing the original
        # frame is safe.
        st_values, st_direction = self._compute_supertrend(data)
        adx_series = self._compute_adx(data)

        curr_dir = st_direction.iloc[-1]
        prev_dir = st_direction.iloc[-2]
        adx = adx_series.iloc[-1]
        price = float(data["close"].iloc[-1])
        atr_val = float(self._compute_atr_cached(data, self.period).iloc[-1])

        st_last = st_values.iloc[-1]
        metadata = {
            "supertrend": round(float(st_last), 2) if not pd.isna(st_last) else None,
            "direction": int(curr_dir),
            "adx": round(float(adx), 2) if not pd.isna(adx) else None,
            "atr": round(atr_val, 2),
        }

        # BUY: Supertrend flips to uptrend AND ADX confirms trend strength
        if prev_dir == -1 and curr_dir == 1:
            if not pd.isna(adx) and adx >= self.adx_threshold:
                if self.trend_filter_pct is not None and is_against_trend(
                    symbol, "BUY", threshold_pct=self.trend_filter_pct
                ):
                    logger.info(
                        f"[{self.name}] BUY blocked for {symbol} | ADX={adx:.1f} | "
                        f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                    )
                    return self._make_signal(
                        Signal.HOLD, symbol, data,
                        metadata={**metadata, "reason": "trend_filter_buy"},
                    )

                stop_loss = price - self.multiplier * atr_val
                take_profit = price + 2 * self.multiplier * atr_val
                confidence = min(0.5 + (adx - self.adx_threshold) / 50, 1.0)

                logger.info(f"[{self.name}] BUY {symbol} | ST flip UP, ADX={adx:.1f}")
                return self._make_signal(
                    Signal.BUY, symbol, data,
                    confidence=confidence, stop_loss=stop_loss,
                    take_profit=take_profit, metadata=metadata,
                )
            else:
                metadata["reason"] = f"adx_too_low ({adx:.1f} < {self.adx_threshold})"

        # SELL: Supertrend flips to downtrend
        if prev_dir == 1 and curr_dir == -1:
            if not pd.isna(adx) and adx >= self.adx_threshold:
                if self.trend_filter_pct is not None and is_against_trend(
                    symbol, "SELL", threshold_pct=self.trend_filter_pct
                ):
                    logger.info(
                        f"[{self.name}] SELL blocked for {symbol} | ADX={adx:.1f} | "
                        f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                    )
                    return self._make_signal(
                        Signal.HOLD, symbol, data,
                        metadata={**metadata, "reason": "trend_filter_sell"},
                    )

                stop_loss = price + self.multiplier * atr_val
                take_profit = price - 2 * self.multiplier * atr_val
                confidence = min(0.5 + (adx - self.adx_threshold) / 50, 1.0)
                logger.info(f"[{self.name}] SELL {symbol} | ST flip DOWN, ADX={adx:.1f}")
                return self._make_signal(
                    Signal.SELL, symbol, data,
                    confidence=confidence, stop_loss=stop_loss,
                    take_profit=take_profit, metadata=metadata,
                )

        return self._make_signal(Signal.HOLD, symbol, data, metadata=metadata)
