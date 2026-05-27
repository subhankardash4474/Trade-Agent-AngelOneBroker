"""
Moving Average Crossover Strategy
Generates BUY when the short-term MA crosses above the long-term MA,
and SELL when it crosses below. Uses EMA for faster responsiveness.
"""

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class MovingAverageCrossover(BaseStrategy):
    """
    Exponential Moving Average crossover strategy.

    Parameters:
        short_window: Period for the fast EMA (default 9).
        long_window:  Period for the slow EMA (default 21).
        signal_threshold: Minimum percentage gap between MAs to trigger
                          a signal, filtering out noise (default 0.0).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {"short_window": 9, "long_window": 21, "signal_threshold": 0.05}
        merged = {**defaults, **params}
        super().__init__(name="moving_average_crossover", params=merged)

        self.short_window: int = merged["short_window"]
        self.long_window: int = merged["long_window"]
        self.signal_threshold: float = merged["signal_threshold"]

    @property
    def required_history_bars(self) -> int:
        return self.long_window + 5  # extra buffer for crossover detection

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        # P-04 (perf 2026-05-27): dropped ``df = data.copy()``. The four
        # derived columns (ema_short, ema_long, ma_diff, ma_diff_prev)
        # were written purely to read ``.iloc[-1]`` values. We compute
        # them as local Series instead -- zero copies, zero caller-frame
        # mutation. ``_atr`` and ``_make_signal`` are read-only on the
        # input frame.
        close = data["close"]
        ema_short_series = close.ewm(span=self.short_window, adjust=False).mean()
        ema_long_series = close.ewm(span=self.long_window, adjust=False).mean()
        ma_diff_series = ema_short_series - ema_long_series

        current_diff = ma_diff_series.iloc[-1]
        # is_data_sufficient gate above guarantees len >= long_window + 5
        # (>= 14 bars), so .iloc[-2] is always safe here.
        prev_diff = ma_diff_series.iloc[-2]
        current_price = close.iloc[-1]
        ema_long_val = ema_long_series.iloc[-1]

        pct_gap = abs(current_diff / ema_long_val) * 100 if ema_long_val != 0 else 0

        metadata = {
            "ema_short": round(ema_short_series.iloc[-1], 2),
            "ema_long": round(ema_long_val, 2),
            "ma_diff": round(current_diff, 2),
            "pct_gap": round(pct_gap, 2),
        }

        atr = self._atr(data)

        # Bullish crossover: short EMA crosses above long EMA
        if prev_diff <= 0 < current_diff and pct_gap >= self.signal_threshold:
            confidence = min(pct_gap / 2.0, 1.0)
            stop_loss = current_price - 1.5 * atr if atr > 0 else current_price * 0.985
            take_profit = current_price + 2.5 * atr if atr > 0 else current_price * 1.03
            logger.info(f"[{self.name}] BUY signal for {symbol} | gap={pct_gap:.2f}%")
            return self._make_signal(
                Signal.BUY, symbol, data,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
            )

        # Bearish crossover: short EMA crosses below long EMA
        if prev_diff >= 0 > current_diff and pct_gap >= self.signal_threshold:
            confidence = min(pct_gap / 2.0, 1.0)
            stop_loss = current_price + 1.5 * atr if atr > 0 else current_price * 1.015
            take_profit = current_price - 2.5 * atr if atr > 0 else current_price * 0.97
            logger.info(f"[{self.name}] SELL signal for {symbol} | gap={pct_gap:.2f}%")
            return self._make_signal(
                Signal.SELL, symbol, data,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, data, metadata=metadata)
