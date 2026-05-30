"""
v3.0 — Rule 2: 20-day High Breakout (the kicker)
================================================

Per ``docs/freeze/freeze_v3.0_charter_2026-05-30.md`` §2 Rule 2.

Buy when a stock breaks above its prior 20-day high on a day with
strong volume (>= 1.5x 20-day average), provided it's still in an
uptrend (above 50-DMA) and the trend has measurable momentum
(ADX(14) > 20). Hold for swing duration, exit on either a 12% target
or a stop set 4% below entry OR below the breakout-day's low,
whichever is tighter (less downside).

Entry signal mechanics
----------------------
* Daily candles only. Variants set ``backtest.fill_mode: next_bar_open``.
* BUY-only. Pair with ``risk.allow_shorts: false``.
* "20-day high" means the rolling max of the PRIOR 20 sessions, not
  including today. The breakout test is ``close > prior_20d_max``.

Exit signal mechanics
---------------------
The engine's intra-bar SL/TP detector consumes the ``stop_loss`` and
``take_profit`` returned on the entry signal. There is no equivalent
of Rule 1's "exit on 50-DMA breach" — Rule 2 is a momentum-following
rule, not a mean-reverting one, so the natural exit is the SL/TP
binary.

Charter simplifications accepted in Phase A
-------------------------------------------
* **Trail-stop is not implemented.** Charter §2 calls for "lock 50% of
  unrealised once +6% open". Same rationale as ``trend_pullback``:
  this requires per-position peak tracking with dynamic SL updates,
  deferred to Phase B per Phase A1 §10. The 4% (or breakout-low)
  fixed SL and 12% fixed TP remain in force; the trail would only
  improve expectancy on winners.

Required history bars
---------------------
The 20-day high needs the prior 20 days of OHLC; the 50-DMA needs 50
of close; ADX(14) needs ~28 bars to stabilise (14 of warmup +
14 of smoothed DX). 70 bars is sufficient for all three with a
~10-bar guard.

Cross-references
----------------
* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §2 (rule definition).
* `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` §3, §4.
* `packages/strategies/trend_pullback.py` (sister rule; same fixture
  conventions, paired in v3 swing variants).
"""
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy, Signal, TradeSignal


class Breakout20D(BaseStrategy):
    """v3.0 Rule 2 — 20-day high breakout on daily candles."""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        defaults = {
            "high_window": 20,           # prior-N-day high
            "med_sma_period": 50,        # uptrend filter
            "volume_avg_period": 20,
            "volume_multiplier": 1.5,    # today's vol >= 1.5x 20d avg
            "adx_period": 14,
            "adx_threshold": 20.0,       # "trending environment"
            "sl_pct": 4.0,               # 4% below entry
            "tp_pct": 12.0,              # 12% target
            "confidence": 0.80,
        }
        merged = {**defaults, **params}
        super().__init__(name="breakout_20d", params=merged)

        self.high_window: int = int(merged["high_window"])
        self.med_sma_period: int = int(merged["med_sma_period"])
        self.volume_avg_period: int = int(merged["volume_avg_period"])
        self.volume_multiplier: float = float(merged["volume_multiplier"])
        self.adx_period: int = int(merged["adx_period"])
        self.adx_threshold: float = float(merged["adx_threshold"])
        self.sl_pct: float = float(merged["sl_pct"])
        self.tp_pct: float = float(merged["tp_pct"])
        self.confidence: float = float(merged["confidence"])

    @property
    def required_history_bars(self) -> int:
        # 50-DMA + ADX warmup (~28) + ~10 bar guard = 70 minimum.
        return max(self.med_sma_period, self.adx_period * 2) + 20

    @staticmethod
    def _compute_adx(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int,
    ) -> pd.Series:
        """ADX(period) using Wilder smoothing (RMA). Returns the ADX
        series; the caller takes ``.iloc[-1]``.

        Implementation:
        * True Range = max(H-L, |H-prev_close|, |L-prev_close|)
        * +DM = (H - prev_H) if (H-prev_H) > (prev_L - L) and > 0 else 0
        * -DM = (prev_L - L) if (prev_L - L) > (H - prev_H) and > 0 else 0
        * Smoothed via Wilder's RMA: ewm(alpha=1/period, adjust=False).
        * +DI = 100 * smoothed_+DM / smoothed_TR
        * -DI = 100 * smoothed_-DM / smoothed_TR
        * DX = 100 * |+DI - -DI| / (+DI + -DI)
        * ADX = RMA(DX, period)
        """
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=high.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=high.index,
        )

        # Wilder's smoothing == ewm with alpha = 1/period and adjust=False.
        rma = lambda s: s.ewm(alpha=1.0 / period, adjust=False).mean()  # noqa: E731

        atr = rma(tr)
        plus_di = 100.0 * rma(plus_dm) / atr.replace(0, np.nan)
        minus_di = 100.0 * rma(minus_dm) / atr.replace(0, np.nan)

        di_sum = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
        adx = rma(dx)
        return adx

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "insufficient_data"},
            )

        close_series = data["close"]
        high_series = data["high"]
        low_series = data["low"]
        vol_series = data["volume"]

        close = float(close_series.iloc[-1])
        breakout_low = float(low_series.iloc[-1])
        if close <= 0:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "non_positive_close"},
            )

        # Prior-N-day high: rolling max of high over the LAST ``high_window``
        # bars BEFORE today. ``shift(1)`` excludes today; ``rolling(N)``
        # then takes the max of the prior N bars.
        prior_n_high = (
            high_series.shift(1).rolling(self.high_window).max().iloc[-1]
        )
        if pd.isna(prior_n_high):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "prior_high_nan"},
            )
        prior_n_high = float(prior_n_high)

        if not (close > prior_n_high):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "no_breakout",
                    "close": round(close, 2),
                    "prior_20d_high": round(prior_n_high, 2),
                },
            )

        # Trend filter: must still be above 50-DMA.
        sma_50 = close_series.rolling(self.med_sma_period).mean().iloc[-1]
        if pd.isna(sma_50):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "sma_50_nan"},
            )
        sma_50 = float(sma_50)
        if not (close > sma_50):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "below_50dma",
                    "close": round(close, 2),
                    "sma_50": round(sma_50, 2),
                },
            )

        # Volume confirmation: today's volume >= multiplier * 20d avg.
        vol_window = vol_series.iloc[-self.volume_avg_period:]
        vol_avg = float(vol_window.mean()) if not vol_window.empty else 0.0
        cur_vol = float(vol_series.iloc[-1])
        if vol_avg <= 0 or cur_vol < self.volume_multiplier * vol_avg:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "weak_volume_breakout",
                    "cur_volume": cur_vol,
                    "vol_avg_20d": round(vol_avg, 0),
                    "vol_required": round(self.volume_multiplier * vol_avg, 0),
                },
            )

        # Momentum filter: ADX(14) > threshold.
        adx_series = self._compute_adx(
            high_series, low_series, close_series, self.adx_period,
        )
        adx = adx_series.iloc[-1]
        if pd.isna(adx):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "adx_nan"},
            )
        adx = float(adx)
        if adx <= self.adx_threshold:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={
                    "reason": "adx_below_threshold",
                    "adx": round(adx, 2),
                    "adx_threshold": self.adx_threshold,
                },
            )

        # All entry conditions passed. Compute SL = max(percentage_sl,
        # breakout_day_low) — "tighter" SL = closer to entry = HIGHER
        # value for a long, less downside. Use signal-bar close as entry
        # proxy (fill_mode shifts actual fill to next-bar open; the
        # < 1% deviation is documented in the strategy header).
        pct_sl = close * (1.0 - self.sl_pct / 100.0)
        stop_loss = max(pct_sl, breakout_low)
        take_profit = close * (1.0 + self.tp_pct / 100.0)

        logger.info(
            f"[{self.name}] BUY {symbol} | close={close:.2f} > "
            f"prior_20d_high={prior_n_high:.2f} | SMA50={sma_50:.2f} | "
            f"vol={cur_vol/vol_avg:.2f}x | ADX={adx:.1f} | "
            f"SL={stop_loss:.2f} (pct={pct_sl:.2f}, low={breakout_low:.2f}) "
            f"TP={take_profit:.2f}"
        )
        return self._make_signal(
            Signal.BUY, symbol, data,
            confidence=self.confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "rule": "breakout_20d",
                "close": round(close, 2),
                "prior_20d_high": round(prior_n_high, 2),
                "sma_50": round(sma_50, 2),
                "adx": round(adx, 2),
                "volume_ratio": round(cur_vol / vol_avg, 2) if vol_avg > 0 else None,
                "sl_source": "breakout_low" if breakout_low > pct_sl else "pct",
                "sl_pct": self.sl_pct,
                "tp_pct": self.tp_pct,
            },
        )
