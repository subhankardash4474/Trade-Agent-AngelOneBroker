"""v4 Mode A · V27 — Cross-Asset Trend (Donchian-55/20)
=======================================================

Per ``docs/reviews/strategy_charter_v4_2026-06-01.md`` §3, this is the
**`swing_cash_v27` signal generator**: a long-only Donchian-channel
breakout strategy with regime, volume, volatility, and trend-strength
gates. The orchestrator handles position sizing (`packages.research.signals.volatility_sizer`)
and capital allocation (`packages.research.signals.risk_parity`); this
module owns ONLY the per-symbol per-bar BUY / SELL / HOLD decision.

Entry rule (all of the following must be True; charter §3.2):

    1. close[today] > max(high[t-N:t])                (Donchian breakout, N=55)
    2. close[today] > SMA(200)[today]                 (regime filter — long-side only)
    3. volume[today] >= 1.2 * mean(volume[t-20:t])    (volume confirm)
    4. ATR%(14)[today] <= 5.0%                        (vol cap; avoid blow-off tops)
    5. ADX(14)[today] >= 20                           (trending environment)
    6. slope(SMA(50), 1d) > 0                         (medium-term uptrend)

Exit rule (any of the following):

    1. close[today] < min(low[t-M:t])                 (Donchian exit, M=20)
    2. close[today] < chandelier_stop                 (trailing stop, ATR*3.0)
    3. time_in_trade > 60 trading days                (forced exit; medium-term)

SHORT side: DISABLED (charter §3.2 + v3.0 finding #3 — Indian-equity
short side structurally -EV at retail). Calling with `side="short"`
raises NotImplementedError loudly.

Whipsaw guard (charter §3.2 condition 5: `days_since_last_entry >= 10`)
is NOT applied at the strategy layer — it requires per-symbol per-portfolio
state (when did THIS portfolio last open THIS symbol). The orchestrator
applies it from its position book; the strategy layer remains stateless
(matches the `trend_pullback` precedent).

The strategy uses the reusable Donchian primitives from
`packages.research.signals.donchian` so this module is thin: gate the
inputs, call `entry_signal`/`exit_signal`, package as a `TradeSignal`.

Required history bars: 220 (200-SMA + ~10% guard, matching the
`trend_pullback` Phase A convention).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy, Signal, TradeSignal
from core.signals.donchian import (
    DEFAULT_ADX_MIN,
    DEFAULT_ADX_PERIOD,
    DEFAULT_ATR_CAP_PCT,
    DEFAULT_ATR_PERIOD,
    DEFAULT_CHANDELIER_MULT,
    DEFAULT_ENTRY_N,
    DEFAULT_EXIT_M,
    DEFAULT_SMA50_PERIOD,
    DEFAULT_SMA_REGIME,
    DEFAULT_VOLUME_MULTIPLIER,
    DEFAULT_VOLUME_WINDOW,
    _atr_ewm,
    entry_signal,
    exit_signal,
)

# Charter §3.2 — these defaults are NOT operator-tunable in V27.
# V28+ may change EXACTLY ONE per charter §3.11 retune budget.
V27_DEFAULTS: Dict[str, Any] = {
    "entry_n": DEFAULT_ENTRY_N,                  # 55
    "exit_m": DEFAULT_EXIT_M,                    # 20
    "sma_regime": DEFAULT_SMA_REGIME,            # 200
    "volume_window": DEFAULT_VOLUME_WINDOW,      # 20
    "volume_multiplier": DEFAULT_VOLUME_MULTIPLIER,  # 1.2
    "atr_period": DEFAULT_ATR_PERIOD,            # 14
    "atr_cap_pct": DEFAULT_ATR_CAP_PCT,          # 5.0
    "adx_period": DEFAULT_ADX_PERIOD,            # 14
    "adx_min": DEFAULT_ADX_MIN,                  # 20
    "sma50_period": DEFAULT_SMA50_PERIOD,        # 50
    "chandelier_mult": DEFAULT_CHANDELIER_MULT,  # 3.0
    "max_time_in_trade_bars": 60,
    "confidence": 0.80,
}


class CrossAssetTrendV27(BaseStrategy):
    """V27 long-only Donchian-channel breakout (charter v4 §3)."""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        merged: Dict[str, Any] = {**V27_DEFAULTS, **(params or {})}
        super().__init__(name="cross_asset_trend_v27", params=merged)

        self.entry_n: int = int(merged["entry_n"])
        self.exit_m: int = int(merged["exit_m"])
        self.sma_regime: int = int(merged["sma_regime"])
        self.volume_window: int = int(merged["volume_window"])
        self.volume_multiplier: float = float(merged["volume_multiplier"])
        self.atr_period: int = int(merged["atr_period"])
        self.atr_cap_pct: float = float(merged["atr_cap_pct"])
        self.adx_period: int = int(merged["adx_period"])
        self.adx_min: float = float(merged["adx_min"])
        self.sma50_period: int = int(merged["sma50_period"])
        self.chandelier_mult: float = float(merged["chandelier_mult"])
        self.max_time_in_trade_bars: int = int(merged["max_time_in_trade_bars"])
        self.confidence: float = float(merged["confidence"])

    @property
    def required_history_bars(self) -> int:
        # 200-SMA dominates. +20 bars headroom matches `trend_pullback`.
        return self.sma_regime + 20

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self.is_data_sufficient(data):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "insufficient_data", "have_bars": len(data),
                          "need_bars": self.required_history_bars},
            )

        close = float(data["close"].iloc[-1])
        if not (np.isfinite(close) and close > 0):
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": "non_positive_close"},
            )

        # ── Exit check first — if the price action signals an exit, that
        #    takes precedence over a fresh entry. (The engine's
        #    opposite-signal exit path closes the long on SELL emission.)
        #    NOTE: We don't have entry_bar_index here (strategy is
        #    stateless); the engine layers its own chandelier stop via the
        #    `stop_loss` field. So at the strategy layer we only check the
        #    Donchian exit (charter §3.2 condition 1). Chandelier + time-
        #    in-trade are engine-side concerns.
        exit_fires, exit_diag = exit_signal(
            data,
            exit_m=self.exit_m,
            entry_bar_index=None,  # stateless layer
        )
        if exit_fires:
            return self._make_signal(
                Signal.SELL, symbol, data,
                confidence=self.confidence,
                metadata={
                    "rule": "v27_donchian_exit",
                    "close": round(close, 2),
                    **{f"exit_{k}": v for k, v in exit_diag["gates"].items()},
                },
            )

        # ── Entry check.
        fires, diag = entry_signal(
            data,
            side="long",
            entry_n=self.entry_n,
            sma_regime=self.sma_regime,
            volume_window=self.volume_window,
            volume_multiplier=self.volume_multiplier,
            atr_period=self.atr_period,
            atr_cap_pct=self.atr_cap_pct,
            adx_period=self.adx_period,
            adx_min=self.adx_min,
            sma50_period=self.sma50_period,
            last_entry_bar_index=None,  # whipsaw guard: orchestrator-side
        )

        if not fires:
            return self._make_signal(
                Signal.HOLD, symbol, data,
                metadata={"reason": diag["reason"]},
            )

        # ── Entry fires: compute initial chandelier stop as the
        #    stop_loss attached to this signal. The engine reads
        #    `stop_loss` and arms an intra-bar stop check; whether it
        #    TRAILS (recomputing chandelier daily) is engine-side.
        atr_val = _atr_ewm(data, period=self.atr_period)
        chandelier_initial = (
            close - self.chandelier_mult * atr_val
            if np.isfinite(atr_val) and atr_val > 0
            else close * 0.92  # 8% safety floor when ATR unavailable
        )
        # Sanity: never set a stop above the entry price.
        if chandelier_initial >= close:
            chandelier_initial = close * 0.92

        logger.info(
            f"[cross_asset_trend_v27] BUY {symbol} | close={close:.2f} "
            f"ATR14={atr_val:.2f} chandelier={chandelier_initial:.2f} "
            f"channel_high(N={self.entry_n})={diag['gates']['donchian_breakout']['channel_high']}"
        )
        return self._make_signal(
            Signal.BUY, symbol, data,
            confidence=self.confidence,
            stop_loss=float(chandelier_initial),
            take_profit=None,  # V27 has NO fixed TP; rides until exit signal
            metadata={
                "rule": "v27_donchian_entry",
                "close": round(close, 2),
                "atr_14": round(float(atr_val), 4) if np.isfinite(atr_val) else None,
                "channel_high_n55": diag["gates"]["donchian_breakout"]["channel_high"],
                "sma_200": diag["gates"]["regime_filter_sma200"]["sma200"],
                "adx_14": diag["gates"]["adx_trending"]["adx_14"],
                "volume_ratio": (
                    diag["gates"]["volume_confirm"]["volume_today"]
                    / diag["gates"]["volume_confirm"]["volume_mean_20d"]
                    if diag["gates"]["volume_confirm"]["volume_mean_20d"]
                    else None
                ),
                "atr_pct": diag["gates"]["atr_cap"]["atr_pct"],
                "sma50_slope": diag["gates"]["sma50_slope_up"]["slope_1d"],
                "chandelier_mult": self.chandelier_mult,
                "chandelier_stop_initial": round(float(chandelier_initial), 2),
            },
        )


__all__ = ["CrossAssetTrendV27", "V27_DEFAULTS"]
