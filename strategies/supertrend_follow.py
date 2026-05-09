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
        """Returns (supertrend_values, direction_series)."""
        atr = self._compute_atr(df, self.period)
        hl2 = (df["high"] + df["low"]) / 2

        upper = hl2 + self.multiplier * atr
        lower = hl2 - self.multiplier * atr

        direction = pd.Series(1, index=df.index)
        st = pd.Series(np.nan, index=df.index)

        for i in range(1, len(df)):
            if df["close"].iloc[i] > upper.iloc[i - 1]:
                direction.iloc[i] = 1
            elif df["close"].iloc[i] < lower.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                lower.iloc[i] = max(lower.iloc[i], lower.iloc[i - 1]) if direction.iloc[i - 1] == 1 else lower.iloc[i]
                st.iloc[i] = lower.iloc[i]
            else:
                upper.iloc[i] = min(upper.iloc[i], upper.iloc[i - 1]) if direction.iloc[i - 1] == -1 else upper.iloc[i]
                st.iloc[i] = upper.iloc[i]

        return st, direction

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = data.copy()
        st_values, st_direction = self._compute_supertrend(df)
        df["supertrend"] = st_values
        df["st_dir"] = st_direction
        df["adx"] = self._compute_adx(df)

        curr_dir = df["st_dir"].iloc[-1]
        prev_dir = df["st_dir"].iloc[-2]
        adx = df["adx"].iloc[-1]
        price = float(df["close"].iloc[-1])
        atr_val = float(self._compute_atr(df, self.period).iloc[-1])

        metadata = {
            "supertrend": round(float(df["supertrend"].iloc[-1]), 2) if not pd.isna(df["supertrend"].iloc[-1]) else None,
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
                        Signal.HOLD, symbol, df,
                        metadata={**metadata, "reason": "trend_filter_buy"},
                    )

                stop_loss = price - self.multiplier * atr_val
                take_profit = price + 2 * self.multiplier * atr_val
                confidence = min(0.5 + (adx - self.adx_threshold) / 50, 1.0)

                logger.info(f"[{self.name}] BUY {symbol} | ST flip UP, ADX={adx:.1f}")
                return self._make_signal(
                    Signal.BUY, symbol, df,
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
                        Signal.HOLD, symbol, df,
                        metadata={**metadata, "reason": "trend_filter_sell"},
                    )

                stop_loss = price + self.multiplier * atr_val
                take_profit = price - 2 * self.multiplier * atr_val
                confidence = min(0.5 + (adx - self.adx_threshold) / 50, 1.0)
                logger.info(f"[{self.name}] SELL {symbol} | ST flip DOWN, ADX={adx:.1f}")
                return self._make_signal(
                    Signal.SELL, symbol, df,
                    confidence=confidence, stop_loss=stop_loss,
                    take_profit=take_profit, metadata=metadata,
                )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
