"""
VWAP Bounce Strategy
Intraday strategy that identifies price bouncing off VWAP from below
with volume confirmation. Targets mean-reversion entries near VWAP.
"""

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies._trend_context import is_against_trend
from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class VWAPBounce(BaseStrategy):
    """
    VWAP Bounce strategy for intraday trading.

    BUY when price touches VWAP from below with a volume spike,
    indicating institutional buying support at the fair value level.
    SELL when price fails to hold above VWAP after a previous bounce.

    Parameters:
        vwap_proximity_pct: How close price must be to VWAP to qualify (default 0.3%).
        volume_spike_ratio: Minimum volume ratio vs 20-bar avg (default 1.5x).
        confirmation_bars: Bars to confirm bounce direction (default 2).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "vwap_proximity_pct": 0.3,
            "volume_spike_ratio": 1.5,
            "confirmation_bars": 2,
            "timeframe": "5min",
            # 2026-05-08: trend filter added (parity with mean_reversion / xgb /
            # supertrend / rsi). Block VWAP-break signals that fight the daily
            # 50d SMA by more than this %. Set to None to disable.
            "trend_filter_pct": 5.0,
        }
        merged = {**defaults, **params}
        super().__init__(name="vwap_bounce", params=merged)

        self.vwap_proximity_pct: float = merged["vwap_proximity_pct"]
        self.volume_spike_ratio: float = merged["volume_spike_ratio"]
        self.confirmation_bars: int = merged["confirmation_bars"]
        self.trend_filter_pct: Optional[float] = (
            float(merged["trend_filter_pct"])
            if merged.get("trend_filter_pct") is not None else None
        )

    @property
    def required_history_bars(self) -> int:
        return 30

    @staticmethod
    def _compute_vwap(df: pd.DataFrame) -> pd.Series:
        """Session-reset VWAP: cumulative sums restart each calendar day."""
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        tp_vol = typical_price * df["volume"]
        if hasattr(df.index, "date"):
            day = df.index.date
            cum_tp_vol = tp_vol.groupby(day).cumsum()
            cum_vol = df["volume"].groupby(day).cumsum()
        else:
            cum_tp_vol = tp_vol.cumsum()
            cum_vol = df["volume"].cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(Signal.HOLD, symbol, data, metadata={"reason": "insufficient_data"})

        df = data.copy()
        df["vwap"] = self._compute_vwap(df)
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

        current = df.iloc[-1]
        prev = df.iloc[-2]
        price = current["close"]
        vwap = current["vwap"]

        if pd.isna(vwap) or vwap == 0:
            return self._make_signal(Signal.HOLD, symbol, df, metadata={"reason": "vwap_nan"})

        # F-47 (audit 2026-05-27): VWAP is session-reset (see
        # ``_compute_vwap``). Comparing ``prev["close"] vs prev["vwap"]``
        # across the session boundary -- e.g. current bar is 09:15 of
        # today, prev is 15:30 of yesterday -- is meaningless because
        # the two VWAPs belong to different sessions. Worse, the very
        # first bar of a new session has VWAP == typical_price by
        # construction (cumsum start), so the "broke below VWAP" check
        # triggers nearly always near the open. Bail to HOLD when the
        # two bars are on different calendar dates (the convention
        # FeatureEngine and _compute_vwap already use for sessioning).
        try:
            prev_idx = df.index[-2]
            cur_idx = df.index[-1]
            if hasattr(prev_idx, "date") and hasattr(cur_idx, "date"):
                if prev_idx.date() != cur_idx.date():
                    return self._make_signal(
                        Signal.HOLD, symbol, df,
                        metadata={"reason": "session_boundary"},
                    )
        except Exception:
            # If index isn't datetime-like, fall through with the old
            # behaviour rather than crash; we lose the guard but the
            # backtest harness already constructs same-session windows.
            pass

        distance_pct = abs(price - vwap) / vwap * 100
        vol_ratio = current["vol_ratio"] if not pd.isna(current["vol_ratio"]) else 0

        metadata = {
            "vwap": round(vwap, 2),
            "distance_pct": round(distance_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
        }

        # BUY: price near VWAP, coming from below, with volume spike
        if (distance_pct <= self.vwap_proximity_pct
                and prev["close"] < prev["vwap"]
                and price >= vwap
                and vol_ratio >= self.volume_spike_ratio):

            # Confirm upward momentum over confirmation_bars
            recent_closes = df["close"].iloc[-(self.confirmation_bars + 1):]
            if all(recent_closes.diff().dropna() > 0):
                if self.trend_filter_pct is not None and is_against_trend(
                    symbol, "BUY", threshold_pct=self.trend_filter_pct
                ):
                    logger.info(
                        f"[{self.name}] BUY blocked for {symbol} @ {price:.2f} | "
                        f"trend filter (price < 50d SMA - {self.trend_filter_pct}%)"
                    )
                    return self._make_signal(
                        Signal.HOLD, symbol, df,
                        metadata={**metadata, "reason": "trend_filter_buy"},
                    )

                confidence = min(0.5 + vol_ratio / 5.0, 1.0)
                # F-46 (audit 2026-05-27): BUY used to compute its own
                # "ATR" as (14-bar high - 14-bar low) / 14 -- that is
                # the AVERAGE per-bar range, ~14x smaller than the
                # ATR(14) the SELL branch (and the rest of the codebase)
                # uses via ``BaseStrategy._atr``. Result: BUY SL hugged
                # VWAP at sub-noise distance and was stopped out on the
                # next 5-min wick, while SELL had a properly sized SL.
                # Use the same ``_atr(df)`` helper as the SELL branch
                # so both directions get a comparable risk-per-share.
                atr = self._atr(df)
                stop_loss = vwap - 0.5 * atr if atr > 0 else price * 0.99
                take_profit = price + 1.5 * (price - stop_loss)

                logger.info(f"[{self.name}] BUY {symbol} @ {price:.2f} (VWAP={vwap:.2f}, vol_ratio={vol_ratio:.1f}x)")
                return self._make_signal(
                    Signal.BUY, symbol, df,
                    confidence=confidence, stop_loss=stop_loss,
                    take_profit=take_profit, metadata=metadata,
                )

        # SELL: price drops below VWAP after being above
        # F-46 (audit 2026-05-27): SELL used to accept ``vol_ratio >= 1.0``
        # while BUY required ``>= self.volume_spike_ratio`` (default 1.5x).
        # That asymmetry made SHORTs fire on near-average volume while
        # LONGs needed institutional confirmation -- a one-sided edge that
        # backtests on choppy days revealed as a SHORT-bias bleed. Apply
        # the same ``volume_spike_ratio`` to both sides.
        if (prev["close"] > prev["vwap"] and price < vwap
                and vol_ratio >= self.volume_spike_ratio):
            if self.trend_filter_pct is not None and is_against_trend(
                symbol, "SELL", threshold_pct=self.trend_filter_pct
            ):
                logger.info(
                    f"[{self.name}] SELL blocked for {symbol} @ {price:.2f} | "
                    f"trend filter (price > 50d SMA + {self.trend_filter_pct}%)"
                )
                return self._make_signal(
                    Signal.HOLD, symbol, df,
                    metadata={**metadata, "reason": "trend_filter_sell"},
                )

            confidence = min(0.4 + vol_ratio / 5.0, 0.9)
            atr_sell = self._atr(df)
            stop_loss = vwap + 0.5 * atr_sell if atr_sell > 0 else price * 1.01
            take_profit = price - 1.5 * (stop_loss - price) if stop_loss > price else price * 0.985
            logger.info(f"[{self.name}] SELL {symbol} @ {price:.2f} (broke below VWAP)")
            return self._make_signal(
                Signal.SELL, symbol, df,
                confidence=confidence, stop_loss=stop_loss,
                take_profit=take_profit, metadata=metadata,
            )

        return self._make_signal(Signal.HOLD, symbol, df, metadata=metadata)
