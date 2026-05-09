"""
Mean Reversion Strategy
Assumes prices tend to revert to their rolling mean.
Uses Z-score of price relative to a rolling window to generate signals.
Incorporates Bollinger Band width for volatility context.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class MeanReversion(BaseStrategy):
    """
    Mean reversion strategy based on rolling Z-score.

    BUY when Z-score drops below -entry_z_score (price oversold relative to mean).
    SELL when Z-score rises above +entry_z_score (price overbought relative to mean).
    EXIT positions when Z-score returns to within exit_z_score of zero.

    Parameters:
        lookback_period: Rolling window for mean and std (default 20).
        entry_z_score: Absolute Z-score to enter a trade (default 2.0).
        exit_z_score: Absolute Z-score to exit a trade (default 0.5).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "lookback_period": 20,
            "entry_z_score": 2.0,
            "exit_z_score": 0.5,
            # 2026-05-07: TP at 100% reversion (rolling mean) almost never gets
            # hit in practice — price reverses before reaching the mean. Set
            # TP at 80% of the distance from current price to mean. Confirmed
            # by today's MEESHO post-mortem: TP=200.42 was 11 paise below the
            # actual bar low (200.53), never triggered.
            "tp_reversion_pct": 0.80,
            # 2026-05-07: trend filter. Block SHORTs in strong uptrends,
            # LONGs in strong downtrends. CROMPTON (+15.8% above 50d SMA)
            # was the canonical mistake. Set to None to disable.
            "trend_filter_pct": 5.0,
        }
        merged = {**defaults, **params}
        super().__init__(name="mean_reversion", params=merged)

        self.lookback_period: int = merged["lookback_period"]
        self.entry_z_score: float = merged["entry_z_score"]
        self.exit_z_score: float = merged["exit_z_score"]
        self.tp_reversion_pct: float = float(merged["tp_reversion_pct"])
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )

    @property
    def required_history_bars(self) -> int:
        return self.lookback_period + 5

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = data.copy()
        df["rolling_mean"] = df["close"].rolling(window=self.lookback_period).mean()
        df["rolling_std"] = df["close"].rolling(window=self.lookback_period).std()
        df["z_score"] = (df["close"] - df["rolling_mean"]) / df["rolling_std"].replace(0, np.nan)

        # Bollinger Band width as volatility gauge
        df["bb_upper"] = df["rolling_mean"] + 2 * df["rolling_std"]
        df["bb_lower"] = df["rolling_mean"] - 2 * df["rolling_std"]
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["rolling_mean"]

        z = df["z_score"].iloc[-1]
        z_prev = df["z_score"].iloc[-2] if len(df) > 1 else 0
        current_price = df["close"].iloc[-1]
        rolling_mean = df["rolling_mean"].iloc[-1]
        bb_width = df["bb_width"].iloc[-1]

        if pd.isna(z) or pd.isna(rolling_mean):
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "nan_values"})

        metadata = {
            "z_score": round(z, 3),
            "rolling_mean": round(rolling_mean, 2),
            "bb_width": round(bb_width, 4) if not pd.isna(bb_width) else None,
        }

        # Avoid trading in extremely low volatility (Bollinger squeeze)
        if not pd.isna(bb_width) and bb_width < 0.01:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={**metadata, "reason": "low_volatility"})

        # BUY: price significantly below mean and starting to revert
        if z <= -self.entry_z_score and z > z_prev:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "BUY", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] BUY blocked for {symbol} | Z={z:.2f} | "
                    f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_long"},
                )
            confidence = min(abs(z) / (self.entry_z_score * 2), 1.0)
            stop_loss = current_price * (1 - 0.005 * abs(z))
            # TP at `tp_reversion_pct` of the distance to mean, not full reversion.
            take_profit = current_price + (rolling_mean - current_price) * self.tp_reversion_pct
            logger.info(f"[{self.name}] BUY signal for {symbol} | Z={z:.2f}")
            return self._make_signal(
                Signal.BUY, symbol, df,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata={**metadata, "intent": "entry"},
            )

        # SELL: price significantly above mean and starting to revert
        if z >= self.entry_z_score and z < z_prev:
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "SELL", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] SELL blocked for {symbol} | Z={z:.2f} | "
                    f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_short"},
                )
            confidence = min(abs(z) / (self.entry_z_score * 2), 1.0)
            stop_loss = current_price * (1 + 0.005 * abs(z))
            # SHORT: TP below current price, fraction of distance to mean.
            take_profit = current_price - (current_price - rolling_mean) * self.tp_reversion_pct
            logger.info(f"[{self.name}] SELL signal for {symbol} | Z={z:.2f}")
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata={**metadata, "intent": "entry"},
            )

        # EXIT: Z has reverted to within the exit band — thesis fulfilled.
        # Emit a moderate-confidence SELL to let the ensemble close longs
        # before price drifts to the other extreme (and BUY to close shorts).
        # Requires the previous bar to have been outside the exit band, so we
        # only signal on the transition (not continuously while inside).
        if (
            not pd.isna(z_prev)
            and abs(z) <= self.exit_z_score
            and abs(z_prev) > self.exit_z_score
        ):
            # Direction-aware: if we came from the oversold side (z_prev < 0),
            # price has risen back to mean → exit long via SELL signal.
            # If we came from overbought (z_prev > 0), price has dropped back
            # → exit short via BUY signal.
            exit_side = Signal.SELL if z_prev < 0 else Signal.BUY
            logger.info(
                f"[{self.name}] EXIT signal for {symbol} | Z={z:.2f} from Z_prev={z_prev:.2f} "
                f"(thesis fulfilled, emitting {exit_side.name})"
            )
            return self._make_signal(
                exit_side, symbol, df,
                confidence=0.45,  # moderate — only acts if ensemble agrees
                stop_loss=None,
                take_profit=None,
                metadata={**metadata, "intent": "mean_reversion_exit"},
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
