"""
RSI Momentum Strategy
Uses the Relative Strength Index to identify overbought/oversold conditions.
Incorporates RSI trend direction for higher-confidence signals.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class RSIMomentum(BaseStrategy):
    """
    RSI-based momentum strategy.

    Generates BUY when RSI drops below the oversold threshold and reverses,
    and SELL when RSI rises above the overbought threshold and reverses.
    Incorporates volume confirmation for stronger signals.

    Parameters:
        period: RSI look-back period (default 14).
        overbought: Upper RSI threshold (default 70).
        oversold: Lower RSI threshold (default 30).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "period": 14,
            "overbought": 70,
            "oversold": 30,
            # 2026-05-08: trend filter added after ATHERENERG (+15% above 50d SMA)
            # was shorted by this strategy on an intraday RSI overbought reversal,
            # ignoring the strong daily uptrend. Block when entry side fights
            # the 50d daily SMA by more than this %. Set to None to disable.
            "trend_filter_pct": 5.0,
        }
        merged = {**defaults, **params}
        super().__init__(name="rsi_momentum", params=merged)

        self.period: int = merged["period"]
        self.overbought: float = merged["overbought"]
        self.oversold: float = merged["oversold"]
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )

    @property
    def required_history_bars(self) -> int:
        return self.period + 10

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = data.copy()
        df["rsi"] = self._compute_rsi(df["close"], self.period)
        df["rsi_prev"] = df["rsi"].shift(1)
        df["rsi_prev2"] = df["rsi"].shift(2)

        rsi = df["rsi"].iloc[-1]
        rsi_prev = df["rsi_prev"].iloc[-1]
        rsi_prev2 = df["rsi_prev2"].iloc[-1]
        current_price = df["close"].iloc[-1]

        # Volume surge check: current volume > 1.5x average of last 20 bars
        vol_avg = df["volume"].iloc[-20:].mean() if len(df) >= 20 else df["volume"].mean()
        current_vol = df["volume"].iloc[-1]
        volume_surge = current_vol > 1.5 * vol_avg if vol_avg > 0 else False

        metadata = {
            "rsi": round(rsi, 2),
            "rsi_prev": round(rsi_prev, 2),
            "volume_surge": volume_surge,
        }

        atr = self._atr(df)

        # BUY: RSI was in oversold zone and is now rising
        if rsi_prev2 < self.oversold and rsi_prev < self.oversold and rsi >= self.oversold:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "BUY", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] BUY blocked for {symbol} | RSI={rsi:.1f} | "
                    f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_buy"},
                )

            distance_from_extreme = (self.oversold - min(rsi_prev, rsi_prev2)) / self.oversold
            confidence = min(0.5 + distance_from_extreme + (0.2 if volume_surge else 0.0), 1.0)
            stop_loss = current_price - 1.5 * atr if atr > 0 else current_price * 0.985
            take_profit = current_price + 2.5 * atr if atr > 0 else current_price * 1.03
            logger.info(f"[{self.name}] BUY signal for {symbol} | RSI={rsi:.1f} reversal from oversold")
            return self._make_signal(
                Signal.BUY, symbol, df,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
            )

        # SELL: RSI was in overbought zone and is now falling
        if rsi_prev2 > self.overbought and rsi_prev > self.overbought and rsi <= self.overbought:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "SELL", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] SELL blocked for {symbol} | RSI={rsi:.1f} | "
                    f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_sell"},
                )

            distance_from_extreme = (max(rsi_prev, rsi_prev2) - self.overbought) / (100 - self.overbought)
            confidence = min(0.5 + distance_from_extreme + (0.2 if volume_surge else 0.0), 1.0)
            stop_loss = current_price + 1.5 * atr if atr > 0 else current_price * 1.015
            take_profit = current_price - 2.5 * atr if atr > 0 else current_price * 0.97
            logger.info(f"[{self.name}] SELL signal for {symbol} | RSI={rsi:.1f} reversal from overbought")
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
