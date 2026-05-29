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
        """Compute ATR(period) from OHLCV data. Returns 0 on failure.

        F-45 (audit 2026-05-27): switched from a simple rolling mean of
        the true-range series to ``tr.ewm(span=period, adjust=False).mean()``
        so this helper produces the SAME value as
        ``FeatureEngine._add_volatility_features`` and the ATR used by
        the ADX/Supertrend computations. Pre-fix, a strategy that
        consulted ``self._atr(df)`` for SL sizing got an SMA-ATR while
        the rest of the pipeline (regime classifier, conviction-aware
        ATR gate, dist_from_supertrend_atr feature) used EWM-ATR --
        same name, different numbers (typically EWM is more responsive
        to a recent volatility shock). The divergence was responsible
        for inconsistent SL placement vs the gating that decided
        whether to take the trade at all.
        """
        try:
            tr = pd.concat([
                data["high"] - data["low"],
                (data["high"] - data["close"].shift()).abs(),
                (data["low"] - data["close"].shift()).abs(),
            ], axis=1).max(axis=1)
            val = tr.ewm(span=period, adjust=False).mean().iloc[-1]
            if pd.isna(val):
                # OBS-10 (audit 2026-05-28): pre-fix this returned 0.0
                # silently when the EWM result was NaN (e.g. <14 bars,
                # all-zero-range data). Strategies sized SLs off
                # zero-ATR -> stops were sub-noise -> whipsaw exits.
                # Log at WARNING so the data gap is visible, then
                # surface 0.0 so the caller's existing guards
                # (atr > 0 checks throughout RiskManager) trigger.
                from loguru import logger as _logger
                _logger.warning(
                    f"[base_strategy._atr] EWM produced NaN for "
                    f"period={period} on {len(data)} bars -- caller "
                    f"will receive 0.0 and is expected to short-circuit "
                    f"on the zero-ATR guard."
                )
                return 0.0
            return float(val)
        except Exception as exc:
            # OBS-10 (audit 2026-05-28): pre-fix this swallowed every
            # exception silently and returned 0.0 -- the caller saw a
            # plausible "no volatility" reading and built a wrong SL.
            # Now log repr(exc) at WARNING so a malformed DataFrame
            # surfaces in the daemon log instead of corrupting the SL
            # math invisibly.
            from loguru import logger as _logger
            _logger.warning(
                f"[base_strategy._atr] computation RAISED for "
                f"period={period} on {len(data) if data is not None else 'None'} "
                f"bars: {type(exc).__name__}: {exc!r}. Returning 0.0; "
                f"caller's zero-ATR guard MUST short-circuit."
            )
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
