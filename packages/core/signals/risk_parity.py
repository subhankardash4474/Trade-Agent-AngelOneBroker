"""Inverse-volatility risk-parity capital allocator (charter v4 §3.5).

For each candidate i with 20-day return std σ_i:

    weight_i = (1 / σ_i) / Σ_j (1 / σ_j)
    allocation_i = total_capital * weight_i
    allocation_i = min(allocation_i, total_capital * max_per_name_pct)

Lower-vol instruments (large caps, ETFs) get larger allocations; higher-vol
instruments get smaller. Equal-risk, not equal-cash.

This is a portfolio-level concern (it requires the FULL set of candidates
in one call), distinct from `volatility_sizer` which sizes a single
position independently.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable

import numpy as np
import pandas as pd

# Charter §3.5 + §3.6 defaults
DEFAULT_VOL_WINDOW = 20
DEFAULT_MAX_PER_NAME_PCT = 8.0
DEFAULT_MIN_VOL_FLOOR = 1e-4  # avoid div-by-zero on flat-line instruments


def daily_return_std(
    df: pd.DataFrame,
    *,
    window: int = DEFAULT_VOL_WINDOW,
    price_col: str = "close",
) -> float:
    """Sample std of last `window` daily simple returns (charter §3.5).

    Returns NaN if insufficient history. Lower bound applied at
    `DEFAULT_MIN_VOL_FLOOR` to keep the inverse-vol weight finite.
    """
    if len(df) < window + 1:
        return float("nan")
    closes = df[price_col].iloc[-window - 1 :]
    rets = closes.pct_change().dropna()
    if len(rets) < window // 2:
        return float("nan")
    val = float(rets.std(ddof=1))
    if not math.isfinite(val):
        return float("nan")
    return max(val, DEFAULT_MIN_VOL_FLOOR)


def inverse_vol_weights(
    sigmas: Dict[str, float],
) -> Dict[str, float]:
    """Compute risk-parity weights from a {symbol: sigma} dict.

    NaN-sigmas are dropped (instrument not weight-eligible).
    Returns a dict {symbol: weight in [0, 1]} that sums to 1.0
    (or the empty dict if no valid sigmas).

    Note: this is the UNCAPPED weight. The per-name cap is applied in
    `allocate()`, not here, because the cap can require iterative
    re-normalisation across capped vs uncapped names.
    """
    valid = {s: v for s, v in sigmas.items() if math.isfinite(v) and v > 0}
    if not valid:
        return {}
    inv = {s: 1.0 / v for s, v in valid.items()}
    total = sum(inv.values())
    if total <= 0:
        return {}
    return {s: w / total for s, w in inv.items()}


def allocate(
    total_capital_inr: float,
    sigmas: Dict[str, float],
    *,
    max_per_name_pct: float = DEFAULT_MAX_PER_NAME_PCT,
    max_iterations: int = 10,
) -> Dict[str, float]:
    """Return per-symbol INR allocation, capped per name (charter §3.5).

    Algorithm:
        1. Compute raw inverse-vol weights w_i.
        2. Compute INR allocation a_i = total * w_i.
        3. If any a_i > total * cap_pct/100:
            a. Clamp the over-cap names to total * cap_pct/100.
            b. Re-distribute the freed capital across the under-cap
               names IN PROPORTION TO their original (uncapped) weights.
            c. Repeat until no name is over-cap, or max_iterations reached.
        4. Return final {symbol: INR allocation}.

    The capped names get exactly `total * cap_pct/100`. The uncapped
    names absorb the residual proportionally. This preserves the
    inverse-vol shape among the uncapped names.

    Args:
        total_capital_inr: budget to allocate across the candidates.
        sigmas: dict {symbol: 20-day return std}. Use `daily_return_std`
            to compute per-symbol values from price history.
        max_per_name_pct: per-name cap as % of total. Default 8.0 per
            charter §3.3 / §3.6.
        max_iterations: re-distribution iteration limit. Default 10.

    Returns:
        {symbol: allocation_inr} with sum ≤ total_capital_inr (may be
        slightly less if all names hit the cap and there's residual cash
        left — that residual goes to LIQUIDBEES per charter §3.6).
    """
    if not (math.isfinite(total_capital_inr) and total_capital_inr > 0):
        return {}

    weights = inverse_vol_weights(sigmas)
    if not weights:
        return {}

    cap_inr = total_capital_inr * (max_per_name_pct / 100.0)
    if cap_inr <= 0:
        return {}

    # Initial allocation.
    alloc: Dict[str, float] = {s: total_capital_inr * w for s, w in weights.items()}

    # Iteratively cap & redistribute.
    for _ in range(max_iterations):
        over = {s: a for s, a in alloc.items() if a > cap_inr + 1e-9}
        if not over:
            break

        # Clamp the over-cap names.
        for s in over:
            alloc[s] = cap_inr

        # Freed capital = sum(over_excess).
        freed = sum(a - cap_inr for a in over.values())

        # Re-distribute across STILL-UNCAPPED names in proportion to
        # their ORIGINAL (uncapped) weights.
        under_names = [s for s in alloc if s not in over and alloc[s] < cap_inr - 1e-9]
        if not under_names:
            # Everyone is at cap; residual sits as "unallocated"
            # (returned to caller as a non-sum-to-1 dict).
            break
        under_weight_sum = sum(weights[s] for s in under_names)
        if under_weight_sum <= 0:
            break
        for s in under_names:
            alloc[s] += freed * (weights[s] / under_weight_sum)

    return alloc


def allocate_from_prices(
    total_capital_inr: float,
    price_history: Dict[str, pd.DataFrame],
    *,
    window: int = DEFAULT_VOL_WINDOW,
    max_per_name_pct: float = DEFAULT_MAX_PER_NAME_PCT,
    price_col: str = "close",
) -> Dict[str, float]:
    """Convenience: compute sigmas + allocate in one call.

    Args:
        price_history: {symbol: OHLCV DataFrame ending at "today"}.
        Other args as in `allocate()`.
    """
    sigmas = {
        s: daily_return_std(df, window=window, price_col=price_col)
        for s, df in price_history.items()
    }
    return allocate(
        total_capital_inr,
        sigmas,
        max_per_name_pct=max_per_name_pct,
    )


def shares_from_allocation(
    allocation_inr: Dict[str, float],
    prices: Dict[str, float],
    lot_sizes: Dict[str, int] | None = None,
) -> Dict[str, int]:
    """Round each INR allocation down to a tradeable lot.

    Args:
        allocation_inr: output of `allocate()`.
        prices: {symbol: last close} for share-count conversion.
        lot_sizes: optional {symbol: lot}; default 1 for unknown symbols.
    """
    if lot_sizes is None:
        lot_sizes = {}
    out: Dict[str, int] = {}
    for s, inr in allocation_inr.items():
        price = prices.get(s)
        if price is None or not (math.isfinite(price) and price > 0):
            out[s] = 0
            continue
        lot = max(1, lot_sizes.get(s, 1))
        raw = inr / price
        shares = int(math.floor(raw / lot)) * lot
        out[s] = max(0, shares)
    return out


__all__ = [
    "daily_return_std",
    "inverse_vol_weights",
    "allocate",
    "allocate_from_prices",
    "shares_from_allocation",
    "DEFAULT_VOL_WINDOW",
    "DEFAULT_MAX_PER_NAME_PCT",
]
