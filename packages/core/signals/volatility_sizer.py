"""Vol-target position sizer (charter v4 §3.3).

CTA-standard "equal risk per position" sizing, NOT v2.1's fixed-fraction:

    risk_per_trade_inr = equity * 0.005           (charter default 0.5%)
    position_size_shares = round(risk_per_trade_inr / atr_14_inr_per_share)
    capped at max_position_inr = equity * 0.08    (8% per name)

This is a pure utility module — no orchestrator coupling. The
`backtest_ensemble.py` extension (tomorrow's work) wires this in via a
`--sizer volatility_target` flag so V27 backtests use it instead of the
v2.1 `position_sizer` for the true-V27 number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Charter §3.3 defaults
DEFAULT_RISK_PCT = 0.5  # 0.5% of equity per trade
DEFAULT_MAX_POSITION_PCT = 8.0  # 8% of equity per name


@dataclass
class SizingResult:
    """Output of vol_target_size().

    Attributes:
        shares: integer share count (rounded DOWN to a tradeable lot).
        notional_inr: shares * price.
        risk_inr: shares * atr_14_inr_per_share (the daily 1σ risk).
        binding_constraint: which limit determined the final size:
            "risk_target" — the 0.5% risk budget set the size.
            "max_position" — the 8% per-name cap clamped it down.
            "atr_zero" — ATR is zero/NaN; no position taken.
            "price_zero" — price is zero/NaN; no position taken.
            "equity_too_small" — equity * 0.5% / ATR < 1 share; skipped.
    """
    shares: int
    notional_inr: float
    risk_inr: float
    binding_constraint: str


def vol_target_size(
    equity_inr: float,
    price_inr: float,
    atr_14_inr_per_share: float,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    lot_size: int = 1,
) -> SizingResult:
    """Vol-target sizing per charter §3.3.

    Args:
        equity_inr: current portfolio equity in INR.
        price_inr: instrument's last close in INR (used for the 8% cap).
        atr_14_inr_per_share: ATR(14) expressed in INR per share (NOT a
            percentage — use ATR_pct * price if you have a percent-ATR
            value).
        risk_pct: target daily 1σ risk as a percentage of equity.
            Default 0.5 per charter §3.3 + Q2 default.
        max_position_pct: per-name notional cap. Default 8.0 per charter §3.3.
        lot_size: round-down lot. Default 1 (cash CNC). For F&O futures
            this is the contract lot size (e.g. NIFTY lot = 75 shares).

    Returns:
        SizingResult with the integer share count + diagnostics.

    Notes:
        * Returns shares=0 (binding_constraint="atr_zero") if ATR is
          NaN / zero. The orchestrator must skip the trade — letting a
          zero-ATR instrument through silently with shares=1 was the
          v2.1 F-34 bug (audit 2026-05-27 §B3).
        * The 8% cap is computed PRE-rounding (`shares_at_max_inr`) and
          PRE-lot-size adjustment. If both limits would round to 0
          shares, returns shares=0 (binding_constraint="equity_too_small")
          rather than 1-share "floor" (the F-34 anti-pattern).
    """
    # ── Defensive input handling ──
    if not (math.isfinite(equity_inr) and equity_inr > 0):
        return SizingResult(0, 0.0, 0.0, "equity_too_small")
    if not (math.isfinite(price_inr) and price_inr > 0):
        return SizingResult(0, 0.0, 0.0, "price_zero")
    if not (math.isfinite(atr_14_inr_per_share) and atr_14_inr_per_share > 0):
        return SizingResult(0, 0.0, 0.0, "atr_zero")
    if lot_size < 1:
        lot_size = 1

    # ── Charter §3.3 computation ──
    risk_target_inr = equity_inr * (risk_pct / 100.0)
    shares_at_risk = risk_target_inr / atr_14_inr_per_share

    max_position_inr = equity_inr * (max_position_pct / 100.0)
    shares_at_max = max_position_inr / price_inr

    # Binding constraint = whichever is smaller
    if shares_at_risk <= shares_at_max:
        raw_shares = shares_at_risk
        binding = "risk_target"
    else:
        raw_shares = shares_at_max
        binding = "max_position"

    # Round DOWN to lot size (charter §3.3: "round to broker-acceptable lot")
    shares = int(math.floor(raw_shares / lot_size)) * lot_size

    if shares < 1:
        return SizingResult(0, 0.0, 0.0, "equity_too_small")

    notional = shares * price_inr
    risk_realised = shares * atr_14_inr_per_share

    return SizingResult(
        shares=shares,
        notional_inr=notional,
        risk_inr=risk_realised,
        binding_constraint=binding,
    )


__all__ = [
    "SizingResult",
    "vol_target_size",
    "DEFAULT_RISK_PCT",
    "DEFAULT_MAX_POSITION_PCT",
]
