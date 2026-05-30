"""
v3.0 — Rule 1: Trend Pullback (the workhorse)
==============================================

Per ``docs/freeze/freeze_v3.0_charter_2026-05-30.md`` §2 Rule 1.

Buy a stock that is in a confirmed uptrend (above 50-DMA AND 200-DMA)
when it pulls back to the 20-DMA AND has cooled but not broken (RSI
40-55) AND the pullback is happening on healthy volume (>= 80% of 20d
avg). Hold for swing duration (3-10 days typical), exit on either an
8% target, a 3% stop, or a close below the 50-DMA.

Entry signal mechanics
----------------------
* Daily candles only (variants set ``backtest.fill_mode: next_bar_open``
  so the actual fill is the day-after-close-decision's open).
* BUY-only: this rule does not emit SELL entries. Pair with
  ``risk.allow_shorts: false`` in the variant config to drop any
  SELL emissions cleanly.

Exit signal mechanics
---------------------
The strategy itself emits SELL on a held position when the close
breaches the 50-DMA (charter "exit on breach below 50-DMA"). The
backtester engine handles this via its existing opposite-signal exit
path (``packages/research/backtest_ensemble.py`` lines ~487-528).

Hard SL/TP are returned on the entry signal and consumed by the
engine's intra-bar exit detector:

* **SL** = ``entry_close * (1 - sl_pct / 100)`` — fixed 3% below the
  signal-bar close (default). Approximates "3% below entry" since the
  fill is one bar later (next-day open). The deviation is bounded by
  one overnight gap, typically < 0.5% for Nifty 50 names.
* **TP** = ``entry_close * (1 + tp_pct / 100)`` — fixed 8% above.

Charter simplifications accepted in Phase A
-------------------------------------------
* **Trail-stop is not implemented in this strategy.** Charter §2 calls
  for "lock 50% of unrealised P&L once trade is +5% open". This
  requires per-position peak tracking with dynamic SL updates, which
  is a backtester-engine change, not a strategy-side change. Deferred
  to Phase B per Phase A1 §10 risk discussion. The 3%/8% binary SL/TP
  remains in force; the trail would only IMPROVE expectancy on winners
  by reducing giveback. Phase A's hypothesis test is whether the
  underlying entry edge exists; the trail is a polish on a passing
  thesis.
* **Earnings / event blackouts are not enforced** in this strategy.
  Variants relying on event-blackout discipline must consume the
  existing market_safety / event_calendar gates outside the strategy.

Required history bars
---------------------
200-DMA needs 200 bars. We require 220 to give the rolling means a
~10% guard against a partial NaN window.

Cross-references
----------------
* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §2 (rule definition).
* `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` §3, §4
  (multi-day holds + next-day-open fill).
* `packages/research/backtest_ensemble.py` (intra-bar SL/TP +
  opposite-signal exit pipeline this strategy plugs into).
"""
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class TrendPullback(BaseStrategy):
    """v3.0 Rule 1 — trend pullback on daily candles."""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            # Trend filters (long-only entry preconditions).
            "long_sma_period": 200,
            "med_sma_period": 50,
            "pullback_sma_period": 20,
            # Pullback proximity to 20-DMA (close within X% of 20-DMA).
            "pullback_proximity_pct": 2.0,
            # RSI(14) bounds for "cooled but not broken".
            "rsi_period": 14,
            "rsi_lower": 40.0,
            "rsi_upper": 55.0,
            # Volume confirmation: today's volume >= floor * 20d avg.
            # 0.80 = "no panic-low-volume pullbacks".
            "volume_avg_period": 20,
            "volume_floor_ratio": 0.80,
            # Exit thresholds (binary; trail deferred to Phase B).
            "sl_pct": 3.0,
            "tp_pct": 8.0,
            # Confidence emitted on a clean BUY signal. v3 ensemble runs
            # with min_strategies_agree=1 and a low confidence_threshold
            # so this is largely cosmetic but kept >= 0.55 to cleanly
            # pass the default ensemble gate.
            "confidence": 0.80,
        }
        merged = {**defaults, **params}
        super().__init__(name="trend_pullback", params=merged)

        self.long_sma_period: int = int(merged["long_sma_period"])
        self.med_sma_period: int = int(merged["med_sma_period"])
        self.pullback_sma_period: int = int(merged["pullback_sma_period"])
        self.pullback_proximity_pct: float = float(merged["pullback_proximity_pct"])
        self.rsi_period: int = int(merged["rsi_period"])
        self.rsi_lower: float = float(merged["rsi_lower"])
        self.rsi_upper: float = float(merged["rsi_upper"])
        self.volume_avg_period: int = int(merged["volume_avg_period"])
        self.volume_floor_ratio: float = float(merged["volume_floor_ratio"])
        self.sl_pct: float = float(merged["sl_pct"])
        self.tp_pct: float = float(merged["tp_pct"])
        self.confidence: float = float(merged["confidence"])

    @property
    def required_history_bars(self) -> int:
        # 200-DMA + ~10% headroom so a partial-NaN guard doesn't
        # accidentally fire at the warmup boundary.
        return self.long_sma_period + 20

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
        """RSI matching ``rsi_momentum.RSIMomentum._compute_rsi`` so the
        v3 strategy and any v2.1 RSI tooling agree on degenerate-window
        semantics. Kept inline rather than imported so this strategy
        stays self-contained for v3 isolation.
        """
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        flat_up = (avg_loss == 0) & (avg_gain > 0)
        flat_down = (avg_gain == 0) & (avg_loss > 0)
        flat_flat = (avg_loss == 0) & (avg_gain == 0)
        rsi = rsi.where(~flat_up, 100.0)
        rsi = rsi.where(~flat_down, 0.0)
        rsi = rsi.where(~flat_flat, 50.0)
        return rsi

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "insufficient_data"},
            )

        close_series = data["close"]
        close = float(close_series.iloc[-1])
        if close <= 0:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "non_positive_close"},
            )

        # SMAs computed on the full available series (no copy needed —
        # rolling().mean() is read-only). Take the last value only.
        sma_200 = close_series.rolling(self.long_sma_period).mean().iloc[-1]
        sma_50 = close_series.rolling(self.med_sma_period).mean().iloc[-1]
        sma_20 = close_series.rolling(self.pullback_sma_period).mean().iloc[-1]

        # Defensive: rolling on insufficient data yields NaN even when
        # is_data_sufficient passes (e.g. NaN closes mid-series). Bail
        # so downstream divisions don't propagate NaN into trade math.
        if pd.isna(sma_200) or pd.isna(sma_50) or pd.isna(sma_20):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "sma_nan"},
            )

        sma_200 = float(sma_200)
        sma_50 = float(sma_50)
        sma_20 = float(sma_20)

        # Charter exit rule: close below 50-DMA → emit SELL so any held
        # long position exits via the engine's opposite-signal path.
        # Pair with ``risk.allow_shorts: false`` so a SELL never opens
        # a fresh short on a flat book.
        if close < sma_50:
            return self._make_signal(
                Signal.SELL, symbol, data,
                confidence=self.confidence,
                metadata={
                    "reason": "exit_close_below_50dma",
                    "close": round(close, 2),
                    "sma_50": round(sma_50, 2),
                },
            )

        # Trend filter: must be above BOTH 50-DMA and 200-DMA. The 50-DMA
        # check is a strict-greater (we already short-circuited == case
        # above into SELL via the < check; the boundary close == sma_50
        # falls through to HOLD below).
        if not (close > sma_200 and close > sma_50):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "trend_filter_failed",
                    "close": round(close, 2),
                    "sma_200": round(sma_200, 2),
                    "sma_50": round(sma_50, 2),
                },
            )

        # Pullback proximity: close within X% of 20-DMA. The charter
        # phrasing is symmetric ("within 2%") so we accept both above
        # and below (typical pullback reads as close <= sma_20 * 1.02
        # AND close >= sma_20 * 0.98).
        if sma_20 <= 0:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "sma_20_non_positive"},
            )
        proximity = abs(close - sma_20) / sma_20 * 100.0
        if proximity > self.pullback_proximity_pct:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "not_in_pullback_zone",
                    "proximity_pct": round(proximity, 2),
                    "max_proximity_pct": self.pullback_proximity_pct,
                },
            )

        # RSI window: cooled (>= 40) but not broken (<= 55).
        rsi_series = self._compute_rsi(close_series, self.rsi_period)
        rsi = rsi_series.iloc[-1]
        if pd.isna(rsi):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "rsi_nan"},
            )
        rsi = float(rsi)
        if not (self.rsi_lower <= rsi <= self.rsi_upper):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "rsi_out_of_window",
                    "rsi": round(rsi, 2),
                    "rsi_lower": self.rsi_lower,
                    "rsi_upper": self.rsi_upper,
                },
            )

        # Volume confirmation: today's volume >= floor * 20d avg.
        vol_window = data["volume"].iloc[-self.volume_avg_period:]
        vol_avg = float(vol_window.mean()) if not vol_window.empty else 0.0
        cur_vol = float(data["volume"].iloc[-1])
        if vol_avg <= 0 or cur_vol < self.volume_floor_ratio * vol_avg:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "low_volume_pullback",
                    "cur_volume": cur_vol,
                    "vol_avg_20d": round(vol_avg, 0),
                    "vol_floor": round(self.volume_floor_ratio * vol_avg, 0),
                },
            )

        # All entry conditions passed. SL = -sl_pct%, TP = +tp_pct%
        # relative to signal-bar close. The engine's fill_mode shift
        # to next-bar-open introduces a small (sub-1%) deviation from
        # "3% below ACTUAL entry" but is bounded by one overnight gap
        # — accepted simplification per docstring.
        stop_loss = close * (1.0 - self.sl_pct / 100.0)
        take_profit = close * (1.0 + self.tp_pct / 100.0)

        logger.info(
            f"[{self.name}] BUY {symbol} | close={close:.2f} "
            f"SMA200={sma_200:.2f} SMA50={sma_50:.2f} SMA20={sma_20:.2f} "
            f"RSI={rsi:.1f} prox={proximity:.2f}% vol={cur_vol/vol_avg:.2f}x"
        )
        return self._make_signal(
            Signal.BUY, symbol, data,
            confidence=self.confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "rule": "trend_pullback",
                "close": round(close, 2),
                "sma_200": round(sma_200, 2),
                "sma_50": round(sma_50, 2),
                "sma_20": round(sma_20, 2),
                "rsi": round(rsi, 2),
                "proximity_pct": round(proximity, 2),
                "volume_ratio": round(cur_vol / vol_avg, 2) if vol_avg > 0 else None,
                "sl_pct": self.sl_pct,
                "tp_pct": self.tp_pct,
            },
        )
