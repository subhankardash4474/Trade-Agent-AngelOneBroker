"""
Base Strategy Module
Abstract base class that all trading strategies must implement.
Provides the contract for signal generation and parameter management.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import pandas as pd


class Signal(Enum):
    """Trading signal types."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """Encapsulates a trading signal with metadata."""
    signal: Signal
    symbol: str
    price: float
    timestamp: pd.Timestamp
    strategy_name: str
    confidence: float = 0.0  # 0.0 to 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None
    # Per-strategy vote share: {strategy_name: fractional_credit}
    # Populated by the ensemble so the learner can attribute PnL correctly.
    contributing_strategies: Optional[Dict[str, float]] = None

    def __repr__(self) -> str:
        return (
            f"TradeSignal({self.signal.value} {self.symbol} @ {self.price:.2f} "
            f"[{self.strategy_name}] conf={self.confidence:.2f})"
        )


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    Subclasses must implement:
      - generate_signal(): Produce a BUY/SELL/HOLD signal from market data.
      - required_history_bars: Minimum number of bars needed before signals are valid.
    """

    def __init__(self, name: str, params: Dict[str, Any]):
        self.name = name
        self.params = params
        self._is_ready = False

    @property
    @abstractmethod
    def required_history_bars(self) -> int:
        """Minimum number of historical bars needed to produce a valid signal."""
        pass

    @abstractmethod
    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        """
        Analyze market data and produce a trading signal.

        Args:
            data: OHLCV DataFrame with DatetimeIndex. Must have at least
                  `required_history_bars` rows.
            symbol: The ticker symbol being analyzed.

        Returns:
            TradeSignal with the recommended action.
        """
        pass

    def is_data_sufficient(self, data: pd.DataFrame) -> bool:
        return len(data) >= self.required_history_bars

    @staticmethod
    def _atr(data: pd.DataFrame, period: int = 14) -> float:
        """Compute ATR(period) from OHLCV data. Returns 0 on failure."""
        try:
            tr = pd.concat([
                data["high"] - data["low"],
                (data["high"] - data["close"].shift()).abs(),
                (data["low"] - data["close"].shift()).abs(),
            ], axis=1).max(axis=1)
            val = tr.rolling(period).mean().iloc[-1]
            return float(val) if not pd.isna(val) else 0.0
        except Exception:
            return 0.0

    def _make_signal(
        self,
        signal: Signal,
        symbol: str,
        data: pd.DataFrame,
        confidence: float = 0.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TradeSignal:
        """Helper to construct a TradeSignal from the latest bar."""
        return TradeSignal(
            signal=signal,
            symbol=symbol,
            price=float(data["close"].iloc[-1]),
            timestamp=data.index[-1],
            strategy_name=self.name,
            confidence=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, params={self.params})"
