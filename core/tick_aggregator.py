"""
Tick Aggregator Module
Converts raw tick data from WebSocket into OHLCV candles at
configurable intervals (1m, 5m, 15m). Fires callbacks when
a new candle completes.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")

INTERVAL_SECONDS = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
}


class CandleBuilder:
    """Builds a single OHLCV candle from a stream of ticks."""

    def __init__(self):
        self.open: Optional[float] = None
        self.high: float = float("-inf")
        self.low: float = float("inf")
        self.close: float = 0.0
        self.volume: float = 0.0
        self.tick_count: int = 0

    def add_tick(self, price: float, volume: float = 0):
        if self.open is None:
            self.open = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.tick_count += 1

    def to_dict(self) -> dict:
        return {
            "open": self.open or 0,
            "high": self.high if self.high != float("-inf") else 0,
            "low": self.low if self.low != float("inf") else 0,
            "close": self.close,
            "volume": self.volume,
        }

    @property
    def is_empty(self) -> bool:
        return self.tick_count == 0

    def reset(self):
        self.open = None
        self.high = float("-inf")
        self.low = float("inf")
        self.close = 0.0
        self.volume = 0.0
        self.tick_count = 0


class TickAggregator:
    """
    Aggregates real-time ticks into OHLCV candles at multiple timeframes.

    When a candle period closes, it fires the registered callback with
    the completed candle data.

    Usage:
        agg = TickAggregator(["1min", "5min", "15min"])
        agg.on_candle_close = my_callback
        agg.process_tick("RELIANCE", 2450.50, volume=1200)
    """

    def __init__(self, intervals: Optional[List[str]] = None):
        self.intervals = intervals or ["1min", "5min", "15min"]
        # {interval -> {symbol -> CandleBuilder}}
        self._builders: Dict[str, Dict[str, CandleBuilder]] = {
            iv: defaultdict(CandleBuilder) for iv in self.intervals
        }
        # {interval -> {symbol -> candle_start_time}}
        self._candle_starts: Dict[str, Dict[str, datetime]] = {
            iv: {} for iv in self.intervals
        }
        # Completed candle history: {interval -> {symbol -> list[dict]}}
        self._history: Dict[str, Dict[str, List[dict]]] = {
            iv: defaultdict(list) for iv in self.intervals
        }
        self.on_candle_close: Optional[Callable] = None

    def process_tick(self, symbol: str, price: float, volume: float = 0,
                     timestamp: Optional[datetime] = None):
        """Process a single tick and update all candle builders."""
        now = timestamp or datetime.now(IST)

        for interval in self.intervals:
            builder = self._builders[interval][symbol]
            period_seconds = INTERVAL_SECONDS.get(interval, 300)

            candle_start = self._get_candle_start(now, period_seconds)
            prev_start = self._candle_starts[interval].get(symbol)

            if prev_start is not None and candle_start != prev_start:
                # Period boundary crossed — close the current candle
                if not builder.is_empty:
                    candle = builder.to_dict()
                    candle["timestamp"] = prev_start
                    candle["symbol"] = symbol
                    candle["interval"] = interval
                    self._history[interval][symbol].append(candle)

                    if self.on_candle_close:
                        try:
                            self.on_candle_close(symbol, interval, candle)
                        except Exception as e:
                            logger.error(f"Candle close callback error: {e}")

                builder.reset()

            builder.add_tick(price, volume)
            self._candle_starts[interval][symbol] = candle_start

    @staticmethod
    def _get_candle_start(now: datetime, period_seconds: int) -> datetime:
        """Align timestamp to the start of the current candle period."""
        epoch = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_since_midnight = (now - epoch).total_seconds()
        period_start = int(seconds_since_midnight // period_seconds) * period_seconds
        return epoch + timedelta(seconds=period_start)

    def get_candle_history(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """Get historical candles built from ticks."""
        candles = self._history.get(interval, {}).get(symbol, [])
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles[-limit:])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_current_candle(self, symbol: str, interval: str) -> Optional[dict]:
        """Get the in-progress candle (not yet closed)."""
        builder = self._builders.get(interval, {}).get(symbol)
        if builder and not builder.is_empty:
            candle = builder.to_dict()
            candle["symbol"] = symbol
            candle["interval"] = interval
            candle["timestamp"] = self._candle_starts.get(interval, {}).get(symbol)
            return candle
        return None

    def flush_all(self):
        """Force-close all open candles (e.g., at market close)."""
        for interval in self.intervals:
            for symbol, builder in self._builders[interval].items():
                if not builder.is_empty:
                    candle = builder.to_dict()
                    candle["timestamp"] = self._candle_starts[interval].get(symbol)
                    candle["symbol"] = symbol
                    candle["interval"] = interval
                    self._history[interval][symbol].append(candle)
                    if self.on_candle_close:
                        self.on_candle_close(symbol, interval, candle)
                    builder.reset()
