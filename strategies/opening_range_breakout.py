"""
Opening Range Breakout (ORB) Strategy
Captures momentum from the first 15-minute range breakout.
One of the most reliable intraday setups for Indian markets.
"""

from datetime import time as dtime
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class OpeningRangeBreakout(BaseStrategy):
    """
    Opening Range Breakout strategy.

    Identifies the high and low of the first N minutes after market open (9:15).
    BUY when price breaks above the range high with volume.
    SELL when price breaks below the range low with volume.

    Parameters:
        range_minutes: Duration of opening range in minutes (default 15).
        volume_confirm_ratio: Volume must be this multiple of average (default 1.3x).
        atr_stop_multiplier: Stop-loss distance as ATR multiple (default 1.5).
    """

    MARKET_OPEN = dtime(9, 15)

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "range_minutes": 15,
            "volume_confirm_ratio": 1.3,
            "atr_stop_multiplier": 1.5,
            "timeframe": "5min",
            # 2026-05-08: trend filter added (parity with all other strategies).
            # Block ORB breakouts that fight the daily 50d SMA by more than
            # this %. ORB short into a strong uptrend (or long into a
            # plummeting bear) is a known low-success setup. Set to None to
            # disable.
            "trend_filter_pct": 5.0,
        }
        merged = {**defaults, **params}
        super().__init__(name="opening_range_breakout", params=merged)

        self.range_minutes: int = merged["range_minutes"]
        self.volume_confirm_ratio: float = merged["volume_confirm_ratio"]
        self.atr_stop_multiplier: float = merged["atr_stop_multiplier"]
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )

    @property
    def required_history_bars(self) -> int:
        return 20

    def _identify_opening_range(self, df: pd.DataFrame) -> Optional[tuple]:
        """Find the high and low of the opening range for the latest trading day."""
        if not hasattr(df.index, 'time'):
            return None

        today_data = df[df.index.date == df.index[-1].date()]
        if today_data.empty:
            return None

        # Bars within the opening range period
        range_end_time = dtime(
            self.MARKET_OPEN.hour,
            self.MARKET_OPEN.minute + self.range_minutes,
        )
        range_bars = today_data[
            (today_data.index.time >= self.MARKET_OPEN) &
            (today_data.index.time < range_end_time)
        ]

        if range_bars.empty:
            return None

        return float(range_bars["high"].max()), float(range_bars["low"].min())

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = data.copy()
        opening_range = self._identify_opening_range(df)

        if opening_range is None:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "no_opening_range"})

        range_high, range_low = opening_range
        range_size = range_high - range_low
        current_price = float(df["close"].iloc[-1])
        prev_price = float(df["close"].iloc[-2])

        # Volume check
        vol_avg = df["volume"].rolling(20).mean().iloc[-1]
        current_vol = df["volume"].iloc[-1]
        vol_ratio = current_vol / vol_avg if vol_avg > 0 else 0

        # ATR for stop-loss
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else range_size * 0.3

        metadata = {
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "range_size": round(range_size, 2),
            "vol_ratio": round(vol_ratio, 2),
            "atr": round(atr, 2),
        }

        # Ensure we're past the opening range period
        if hasattr(df.index, 'time'):
            current_time = df.index[-1].time()
            range_end = dtime(
                self.MARKET_OPEN.hour,
                self.MARKET_OPEN.minute + self.range_minutes,
            )
            if current_time < range_end:
                return self._make_signal(Signal.HOLD, symbol, df, metadata={**metadata, "reason": "within_range_period"})

        # BUY: breakout above range high with volume
        if (prev_price <= range_high
                and current_price > range_high
                and vol_ratio >= self.volume_confirm_ratio):
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "BUY", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] BUY blocked for {symbol} | range_high={range_high:.2f} | "
                    f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_buy"},
                )

            stop_loss = range_high - self.atr_stop_multiplier * atr
            take_profit = current_price + 2 * (current_price - stop_loss)
            confidence = min(0.5 + (current_price - range_high) / range_size + vol_ratio / 10, 1.0)

            logger.info(f"[{self.name}] BUY {symbol} | ORB breakout above {range_high:.2f}")
            return self._make_signal(
                Signal.BUY, symbol, df,
                confidence=confidence, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        # SELL: breakdown below range low with volume
        if (prev_price >= range_low
                and current_price < range_low
                and vol_ratio >= self.volume_confirm_ratio):
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "SELL", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] SELL blocked for {symbol} | range_low={range_low:.2f} | "
                    f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_sell"},
                )

            stop_loss = range_low + self.atr_stop_multiplier * atr
            take_profit = current_price - 2 * (stop_loss - current_price)
            confidence = min(0.5 + (range_low - current_price) / range_size + vol_ratio / 10, 1.0)

            logger.info(f"[{self.name}] SELL {symbol} | ORB breakdown below {range_low:.2f}")
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=confidence, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
