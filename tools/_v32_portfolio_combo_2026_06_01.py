"""Combine V32 equity curve with NIFTYBEES buy-and-hold to quantify
mixed-allocation portfolios (50/50, 70/30, 30/70 etc.).

Used to inform the Phase 12 decision: how should the operator
allocate capital between NIFTYBEES (passive beta) and V32 (active
stock-picking) once V32 is approved for live trading?

Approach: V32's equity curve at ₹100k is read from
logs/backtests/v27_v32_maxc6_2026_06_01/equity_curve.csv. NIFTYBEES
b&h is computed from the entry/exit prices in V32's results.json
(the same window, same data feed). We then linearly combine:

    combined_t = w_nb * (cap_nb * nb_return_t) + w_v32 * v32_equity_t

where w_nb + w_v32 = 1.0 and the per-side capital scales accordingly.

Outputs CAGR / Max DD / Calmar for several blend ratios so the
operator can pick.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load_v32_equity() -> tuple[pd.Series, float]:
    """Returns (daily equity curve, initial_capital) for V32."""
    bt_dir = Path("logs/backtests/v27_v32_maxc6_2026_06_01")
    ec = pd.read_csv(bt_dir / "equity_curve.csv")
    ec["date"] = pd.to_datetime(ec["date"])
    ec = ec.set_index("date")["equity"]
    with open(bt_dir / "results.json") as f:
        results = json.load(f)
    cap0 = float(results["metrics"]["initial_capital_inr"])
    return ec, cap0


def load_niftybees_curve(v32_index: pd.DatetimeIndex) -> pd.Series:
    """Build a NIFTYBEES buy-and-hold ratio curve aligned to V32's
    daily index. Uses real yfinance daily prices so the Max DD on
    blended portfolios is realistic.
    """
    import yfinance as yf
    start = v32_index[0] - pd.Timedelta(days=5)
    end = v32_index[-1] + pd.Timedelta(days=5)
    nb = yf.Ticker("NIFTYBEES.NS").history(start=start, end=end)
    if nb.empty or "Close" not in nb.columns:
        raise RuntimeError("yfinance returned no NIFTYBEES data")
    nb.index = nb.index.tz_localize(None).normalize()
    nb_close = nb["Close"]
    # Reindex to V32's daily index; forward-fill weekends/holidays
    nb_close = nb_close.reindex(v32_index, method="ffill")
    ratio = nb_close / nb_close.iloc[0]
    return ratio


def metrics(equity: pd.Series, label: str) -> dict:
    """CAGR / Max DD / Calmar from a daily equity series."""
    start_eq = equity.iloc[0]
    end_eq = equity.iloc[-1]
    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25
    total_ret = end_eq / start_eq - 1
    cagr = (end_eq / start_eq) ** (1 / years) - 1 if years > 0 else 0
    rolling_max = equity.cummax()
    dd = (equity - rolling_max) / rolling_max
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("inf")
    return {
        "label": label,
        "start_inr": start_eq,
        "end_inr": end_eq,
        "total_return_pct": total_ret * 100,
        "cagr_pct": cagr * 100,
        "max_dd_pct": max_dd * 100,
        "calmar": calmar,
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    v32_eq, cap0 = load_v32_equity()
    nb_ratio = load_niftybees_curve(v32_eq.index)
    # Align indices (NIFTYBEES uses the same daily index)
    nb_ratio = nb_ratio.reindex(v32_eq.index, method="nearest")

    print(f"Initial capital: ₹{cap0:,.0f}")
    print(f"Window: {v32_eq.index[0].date()} → {v32_eq.index[-1].date()}")
    print()

    # Pure baselines
    print(f"{'='*70}")
    print("PURE BASELINES")
    print(f"{'='*70}")
    for m in [
        metrics(v32_eq, "V32 alone (100% allocation)"),
        metrics(nb_ratio * cap0, "NIFTYBEES alone (100% allocation)"),
    ]:
        print(f"{m['label']:<45} CAGR={m['cagr_pct']:>+6.2f}%  "
              f"MaxDD={m['max_dd_pct']:>+6.2f}%  Calmar={m['calmar']:.2f}")
    print()

    # Blends
    print(f"{'='*70}")
    print("BLENDED PORTFOLIOS (constant 50/50, 70/30 NB-heavy, 30/70 V32-heavy)")
    print(f"{'='*70}")
    for w_nb in (0.30, 0.50, 0.70):
        w_v32 = 1.0 - w_nb
        # Start: w_nb * cap0 in NIFTYBEES, w_v32 * cap0 in V32-controlled.
        # V32 equity is denominated in ₹100k starting; scale by w_v32.
        v32_scaled = v32_eq * (w_v32 * cap0 / cap0)
        nb_scaled = nb_ratio * (w_nb * cap0)
        combined = v32_scaled + nb_scaled
        m = metrics(combined, f"{int(w_nb*100)}% NIFTYBEES + {int(w_v32*100)}% V32")
        print(f"{m['label']:<45} CAGR={m['cagr_pct']:>+6.2f}%  "
              f"MaxDD={m['max_dd_pct']:>+6.2f}%  Calmar={m['calmar']:.2f}")
    print()
    print("Note: V32 daily equity loaded from "
          "logs/backtests/v27_v32_maxc6_2026_06_01/equity_curve.csv. "
          "NIFTYBEES daily curve from benchmark_equity column if present, "
          "otherwise linearly interpolated from entry→exit (CAGR exact, "
          "Max DD may understate true intra-period drawdown).")


if __name__ == "__main__":
    main()
