"""Multi-strategy portfolio combination tool (Phase 14, 2026-06-01).

Generalizes ``_v32_portfolio_combo_2026_06_01.py`` to N strategies.
Loads the equity curves for V35 (= V32 baseline), V38, V40 from the
multi_swing_firstrun run, plus NIFTYBEES daily prices via yfinance,
and computes:

    1. Pure baselines for each strategy + NIFTYBEES.
    2. Daily-return CORRELATION MATRIX across all 4 series.
       This is the load-bearing analysis for arguing multi-strategy
       paper-mode — if V32/V38/V40 are highly correlated, running them
       together doesn't actually diversify; if they're lowly-correlated,
       a multi-strategy book has lower DD than the best single strategy.
    3. NIFTYBEES + single-strategy blends (50/50, 70/30 NB-heavy).
    4. Multi-strategy blends:
         - Equal-weight active sleeve: V32/V38/V40 each 33%
         - PF-weighted active sleeve: V38 gets the most (PF 2.02),
           V32 in middle (1.36), V40 least (1.30)
         - NIFTYBEES + multi-strategy blends at 30/70 and 50/50 NB:active
    5. Maximum drawdown decomposition: when V32 drawdown peaks, what is
       V38 / V40 doing? Cross-strategy DD timing matters more than the
       correlation coefficient for tail-risk reasoning.

Reproducer:
    python tools/_multi_strategy_combo_2026_06_01.py
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "logs" / "backtests" / "multi_swing_firstrun_2026_06_01"
V40_V41_RUN_DIR = ROOT / "logs" / "backtests" / "multi_swing_v40_v41fix_2026_06_01"

# Variant config — alias to (RUN_DIR_FOR_THIS_VARIANT, subdir, display label, PF).
# V40 was upgraded to v4.1 (rank-drop exits replacing forced month-end rebal)
# in Phase 14; we use the v4.1 result rather than the v4.0 first-run number.
VARIANTS: List[Tuple[str, Path, str, str, float]] = [
    ("V35", RUN_DIR, "V35_donchian55_20", "V35 (= V32 Donchian-55/20)", 1.36),
    ("V38", RUN_DIR, "V38_weekly_breakout", "V38 weekly_breakout", 2.02),
    ("V40", V40_V41_RUN_DIR, "V40_dual_momentum_relstrength", "V40 dual_momentum (v4.1)", 2.13),
]


def load_variant_equity(run_dir: Path, subdir: str) -> Tuple[pd.Series, float]:
    """Returns (daily equity series indexed by date, initial_capital)."""
    bt_dir = run_dir / subdir
    ec = pd.read_csv(bt_dir / "equity_curve.csv")
    ec["date"] = pd.to_datetime(ec["date"])
    ec = ec.set_index("date")["equity"]
    with open(bt_dir / "results.json", encoding="utf-8") as f:
        results = json.load(f)
    cap0 = float(results["metrics"]["initial_capital_inr"])
    return ec, cap0


def load_niftybees_curve(reference_index: pd.DatetimeIndex) -> pd.Series:
    """Build a NIFTYBEES buy-and-hold RATIO curve aligned to the reference index."""
    import yfinance as yf  # type: ignore
    start = reference_index[0] - pd.Timedelta(days=5)
    end = reference_index[-1] + pd.Timedelta(days=5)
    nb = yf.Ticker("NIFTYBEES.NS").history(start=start, end=end)
    if nb.empty or "Close" not in nb.columns:
        raise RuntimeError("yfinance returned no NIFTYBEES data")
    nb.index = nb.index.tz_localize(None).normalize()
    nb_close = nb["Close"]
    nb_close = nb_close.reindex(reference_index, method="ffill")
    ratio = nb_close / nb_close.iloc[0]
    return ratio


def equity_metrics(equity: pd.Series, label: str) -> dict:
    start_eq = float(equity.iloc[0])
    end_eq = float(equity.iloc[-1])
    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25
    total_ret = end_eq / start_eq - 1
    cagr = ((end_eq / start_eq) ** (1 / years) - 1) if years > 0 else 0.0
    rolling_max = equity.cummax()
    dd_series = (equity - rolling_max) / rolling_max
    max_dd = float(dd_series.min())
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("inf")
    # Sharpe (annualized, rf=0).
    daily_ret = equity.pct_change().dropna()
    sharpe = (
        (daily_ret.mean() / daily_ret.std()) * np.sqrt(252)
        if daily_ret.std() > 0
        else float("nan")
    )
    return {
        "label": label,
        "end_inr": end_eq,
        "total_return_pct": total_ret * 100,
        "cagr_pct": cagr * 100,
        "max_dd_pct": max_dd * 100,
        "calmar": calmar,
        "sharpe": sharpe,
    }


def print_metric_row(m: dict, width: int = 50) -> None:
    print(f"{m['label']:<{width}} "
          f"CAGR={m['cagr_pct']:>+7.2f}%  "
          f"MaxDD={m['max_dd_pct']:>+7.2f}%  "
          f"Calmar={m['calmar']:>5.2f}  "
          f"Sharpe={m['sharpe']:>5.2f}")


def main() -> None:
    print("Loading equity curves for V35, V38, V40 (v4.1) ...")
    equities: Dict[str, pd.Series] = {}
    cap0 = None
    for alias, run_dir, subdir, _, _ in VARIANTS:
        eq, c = load_variant_equity(run_dir, subdir)
        equities[alias] = eq
        cap0 = c
        print(f"  {alias}: {len(eq)} daily bars, "
              f"{eq.index[0].date()} → {eq.index[-1].date()}, end ₹{eq.iloc[-1]:,.0f}")

    # Align all to the intersection of indices.
    common_idx = equities["V35"].index
    for alias in ("V38", "V40"):
        common_idx = common_idx.intersection(equities[alias].index)
    common_idx = common_idx.sort_values()
    for alias in equities:
        equities[alias] = equities[alias].reindex(common_idx, method="ffill")

    print(f"\nLoading NIFTYBEES daily prices via yfinance ...")
    nb_ratio = load_niftybees_curve(common_idx)
    nb_equity = nb_ratio * cap0
    print(f"  NIFTYBEES: {len(nb_equity)} bars, end ₹{nb_equity.iloc[-1]:,.0f}")

    window_start = common_idx[0].date()
    window_end = common_idx[-1].date()
    print()
    print(f"Window:          {window_start} → {window_end} "
          f"({(common_idx[-1] - common_idx[0]).days / 365.25:.2f} years)")
    print(f"Initial capital: ₹{cap0:,.0f}")
    print()

    # ── (1) Pure baselines ───────────────────────────────────────────
    print("=" * 110)
    print("PURE BASELINES (100% capital in one instrument)")
    print("=" * 110)
    print_metric_row(equity_metrics(nb_equity, "NIFTYBEES alone (100%)"))
    for alias, _, _, label, _ in VARIANTS:
        print_metric_row(equity_metrics(equities[alias], f"{label} alone (100%)"))
    print()

    # ── (2) Daily-return correlation matrix ──────────────────────────
    print("=" * 110)
    print("DAILY-RETURN CORRELATION MATRIX")
    print("=" * 110)
    rets = pd.DataFrame({
        "V35": equities["V35"].pct_change(),
        "V38": equities["V38"].pct_change(),
        "V40": equities["V40"].pct_change(),
        "NB": nb_equity.pct_change(),
    }).dropna()
    corr = rets.corr()
    print(corr.round(3).to_string())
    print()
    print("Interpretation:")
    print(f"  V35 ↔ V38:  {corr.loc['V35', 'V38']:.3f}  "
          f"(load-bearing — are V32 and V38 actually different strategies?)")
    print(f"  V35 ↔ V40:  {corr.loc['V35', 'V40']:.3f}  "
          f"(load-bearing — V40 is the cleanest individual-stock-driven candidate)")
    print(f"  V38 ↔ V40:  {corr.loc['V38', 'V40']:.3f}  "
          f"(if low, V38 + V40 is a strong pair without V32)")
    print(f"  V35 ↔ NB:   {corr.loc['V35', 'NB']:.3f}  "
          f"(V32 vs passive — should be low; if not, V32 IS closet-indexing)")
    print(f"  V38 ↔ NB:   {corr.loc['V38', 'NB']:.3f}  (V38 vs passive)")
    print(f"  V40 ↔ NB:   {corr.loc['V40', 'NB']:.3f}  (V40 vs passive)")
    print()

    # ── (3) NIFTYBEES + single-strategy blends ───────────────────────
    print("=" * 110)
    print("NIFTYBEES + SINGLE-STRATEGY BLENDS")
    print("=" * 110)
    for alias, _, _, label, _ in VARIANTS:
        for w_nb in (0.30, 0.50, 0.70):
            w_a = 1.0 - w_nb
            blend = (nb_equity * w_nb) + (equities[alias] * w_a)
            tag = f"{int(w_nb*100)}% NB + {int(w_a*100)}% {alias}"
            print_metric_row(equity_metrics(blend, tag))
        print()

    # ── (4) Multi-strategy blends ────────────────────────────────────
    print("=" * 110)
    print("MULTI-STRATEGY ACTIVE SLEEVES (V32 + V38 + V40 in various weightings)")
    print("=" * 110)

    # PF-weighted uses v4.1 V40 PF (2.13), the new headline number.
    pf_sum = 1.36 + 2.02 + 2.13
    sleeves = {
        "equal_thirds": {"V35": 1 / 3, "V38": 1 / 3, "V40": 1 / 3},
        "pf_weighted":  {
            "V35": 1.36 / pf_sum,
            "V38": 2.02 / pf_sum,
            "V40": 2.13 / pf_sum,
        },
        "v38_v40_only_eq": {"V35": 0.0, "V38": 0.5, "V40": 0.5},
        "v40_heavy":    {"V35": 0.10, "V38": 0.30, "V40": 0.60},
        "v38_heavy":    {"V35": 0.10, "V38": 0.60, "V40": 0.30},
    }

    for sleeve_name, weights in sleeves.items():
        sleeve_eq = sum(equities[a] * w for a, w in weights.items())
        tag_w = ", ".join(f"{a}={int(w*100)}%" for a, w in weights.items())
        print_metric_row(equity_metrics(sleeve_eq, f"100% {sleeve_name} ({tag_w})"))
    print()

    print("-" * 110)
    print("NIFTYBEES + MULTI-STRATEGY BLENDS (sleeve = equal_thirds)")
    print("-" * 110)
    eq_sleeve = sum(equities[a] * w for a, w in sleeves["equal_thirds"].items())
    for w_nb in (0.30, 0.50, 0.70):
        w_a = 1.0 - w_nb
        blend = (nb_equity * w_nb) + (eq_sleeve * w_a)
        print_metric_row(
            equity_metrics(blend, f"{int(w_nb*100)}% NB + {int(w_a*100)}% multi(eq3)")
        )
    print()

    print("-" * 110)
    print("NIFTYBEES + MULTI-STRATEGY BLENDS (sleeve = pf_weighted)")
    print("-" * 110)
    eq_sleeve_pf = sum(equities[a] * w for a, w in sleeves["pf_weighted"].items())
    for w_nb in (0.30, 0.50, 0.70):
        w_a = 1.0 - w_nb
        blend = (nb_equity * w_nb) + (eq_sleeve_pf * w_a)
        print_metric_row(
            equity_metrics(blend, f"{int(w_nb*100)}% NB + {int(w_a*100)}% multi(pf-w)")
        )
    print()

    # ── (5) Cross-strategy DD timing ─────────────────────────────────
    print("=" * 110)
    print("MAX-DRAWDOWN TIMING DECOMPOSITION")
    print("=" * 110)
    print("When each strategy hits its own MaxDD trough, what are the OTHERS doing?")
    print("(if they're also at -ve DD it's a synchronized DD; if they're flat/up it's")
    print(" genuine diversification)")
    print()
    for alias in ("V35", "V38", "V40"):
        eq = equities[alias]
        dd = (eq - eq.cummax()) / eq.cummax()
        trough_date = dd.idxmin()
        trough_dd = dd.min() * 100
        print(f"\n{alias} MaxDD: {trough_dd:+.2f}% at {trough_date.date()}")
        for other in ("V35", "V38", "V40", "NB"):
            if other == alias:
                continue
            other_eq = equities[other] if other in equities else nb_equity
            other_dd_series = (other_eq - other_eq.cummax()) / other_eq.cummax()
            if trough_date in other_dd_series.index:
                other_dd_at_trough = other_dd_series.loc[trough_date] * 100
                print(f"    on this date {other} dd = {other_dd_at_trough:+.2f}%")
    print()
    print("=" * 110)
    print("END")
    print("=" * 110)


if __name__ == "__main__":
    main()
