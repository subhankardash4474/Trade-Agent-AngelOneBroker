"""Phase 15 Profile A challenger search — grid-search over NIFTYBEES +
V38-variant + V40-variant weights to find a blend that beats the current
Profile A (70% NIFTYBEES + 30% V38(default) → CAGR +11.00% / MaxDD -12.86%
/ Calmar 0.86 / Sharpe 1.14).

What it does:
    1. Load every equity curve under logs/backtests/multi_swing_phase15sweep_2026_06_01/
       (produced by _phase15_sweep_2026_06_01.py).
    2. Plus the existing V38(default) and V40(v4.1 default) curves.
    3. Plus NIFTYBEES buy-and-hold (from yfinance).
    4. For each V38 variant × V40 variant pair, sweep NB allocation
       in {50, 60, 65, 70, 75, 80} and active split between V38/V40
       in {(100,0), (70,30), (50,50), (30,70), (0,100)}.
    5. Print sorted candidate list against current A.

Candidate selection criterion:
    A "better A" candidate must satisfy ALL of:
        - CAGR ≥ current_A.CAGR (currently +11.00%)
        - MaxDD ≥ current_A.MaxDD (i.e. equal or less negative; -12.86%)
        - Optional bonus: Calmar or Sharpe improvement

If no candidate beats current A on BOTH dimensions, report the
Pareto-frontier (best CAGR at given MaxDD or vice versa) and let the
operator decide whether to accept a trade-off.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PHASE15_RUN_DIR = ROOT / "logs" / "backtests" / "multi_swing_phase15sweep_2026_06_01"
FIRSTRUN_DIR = ROOT / "logs" / "backtests" / "multi_swing_firstrun_2026_06_01"
V40_V41_RUN_DIR = ROOT / "logs" / "backtests" / "multi_swing_v40_v41fix_2026_06_01"

# Current Profile A — what we're trying to beat.
CURRENT_A_CAGR = 11.00
CURRENT_A_MAXDD = -12.86
CURRENT_A_CALMAR = 0.86
CURRENT_A_SHARPE = 1.14


def load_equity(run_dir: Path, subdir: str) -> Tuple[pd.Series, float]:
    bt_dir = run_dir / subdir
    ec_path = bt_dir / "equity_curve.csv"
    if not ec_path.exists():
        raise FileNotFoundError(ec_path)
    ec = pd.read_csv(ec_path)
    ec["date"] = pd.to_datetime(ec["date"])
    ec = ec.set_index("date")["equity"]
    with open(bt_dir / "results.json", encoding="utf-8") as f:
        res = json.load(f)
    cap0 = float(res["metrics"]["initial_capital_inr"])
    return ec, cap0


def load_niftybees_curve(index: pd.DatetimeIndex) -> pd.Series:
    import yfinance as yf  # type: ignore
    start = index[0] - pd.Timedelta(days=5)
    end = index[-1] + pd.Timedelta(days=5)
    nb = yf.Ticker("NIFTYBEES.NS").history(start=start, end=end)
    if nb.empty:
        raise RuntimeError("yfinance returned no NIFTYBEES data")
    nb.index = nb.index.tz_localize(None).normalize()
    nb_close = nb["Close"].reindex(index, method="ffill")
    return nb_close / nb_close.iloc[0]


def metrics(equity: pd.Series, label: str) -> Dict[str, float]:
    start_eq = float(equity.iloc[0])
    end_eq = float(equity.iloc[-1])
    days = (equity.index[-1] - equity.index[0]).days
    years = days / 365.25 if days else 0.0
    cagr = ((end_eq / start_eq) ** (1 / years) - 1) if years > 0 else 0.0
    rolling_max = equity.cummax()
    dd = (equity - rolling_max) / rolling_max
    max_dd = float(dd.min())
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("inf")
    daily_ret = equity.pct_change().dropna()
    sharpe = ((daily_ret.mean() / daily_ret.std()) * np.sqrt(252)
              if daily_ret.std() > 0 else float("nan"))
    return {
        "label": label,
        "cagr_pct": cagr * 100,
        "max_dd_pct": max_dd * 100,
        "calmar": calmar,
        "sharpe": sharpe,
    }


def main() -> int:
    # ── Load V38 variants (Phase 14 default + Phase 15 sweeps).
    v38_variants: Dict[str, pd.Series] = {}

    # Default V38 from firstrun (n=20, m=10, default sma_regime=40).
    eq, cap0 = load_equity(FIRSTRUN_DIR, "V38_weekly_breakout")
    v38_variants["V38_default(n20)"] = eq

    # Phase 14 sensitivity sweeps already on disk.
    n15_dir = ROOT / "logs" / "backtests" / "multi_swing_v38_n15_2026_06_01"
    n25_dir = ROOT / "logs" / "backtests" / "multi_swing_v38_n25_2026_06_01"
    if (n15_dir / "V38_weekly_breakout" / "equity_curve.csv").exists():
        eq, _ = load_equity(n15_dir, "V38_weekly_breakout")
        v38_variants["V38_n15_m8"] = eq
    if (n25_dir / "V38_weekly_breakout" / "equity_curve.csv").exists():
        eq, _ = load_equity(n25_dir, "V38_weekly_breakout")
        v38_variants["V38_n25_m12"] = eq

    # Phase 15 V38 sweeps.
    if PHASE15_RUN_DIR.exists():
        for sub in PHASE15_RUN_DIR.iterdir():
            if sub.is_dir() and sub.name.startswith("V38_"):
                ec_path = sub / "equity_curve.csv"
                if ec_path.exists():
                    ec = pd.read_csv(ec_path)
                    ec["date"] = pd.to_datetime(ec["date"])
                    v38_variants[sub.name] = ec.set_index("date")["equity"]

    # ── Load V40 variants (v4.1 default + Phase 15 sweeps).
    v40_variants: Dict[str, pd.Series] = {}
    eq, _ = load_equity(V40_V41_RUN_DIR, "V40_dual_momentum_relstrength")
    v40_variants["V40_v41_default(decile20)"] = eq
    if PHASE15_RUN_DIR.exists():
        for sub in PHASE15_RUN_DIR.iterdir():
            if sub.is_dir() and sub.name.startswith("V40_"):
                ec_path = sub / "equity_curve.csv"
                if ec_path.exists():
                    ec = pd.read_csv(ec_path)
                    ec["date"] = pd.to_datetime(ec["date"])
                    v40_variants[sub.name] = ec.set_index("date")["equity"]

    print(f"[search] V38 variants loaded: {len(v38_variants)}")
    for k in v38_variants:
        print(f"  {k}")
    print(f"[search] V40 variants loaded: {len(v40_variants)}")
    for k in v40_variants:
        print(f"  {k}")
    print()

    # ── Common date index (intersection of all curves).
    all_curves = {**v38_variants, **v40_variants}
    common_idx = None
    for ec in all_curves.values():
        common_idx = ec.index if common_idx is None else common_idx.intersection(ec.index)
    common_idx = common_idx.sort_values()
    for k in list(all_curves.keys()):
        all_curves[k] = all_curves[k].reindex(common_idx, method="ffill")
    v38_variants = {k: all_curves[k] for k in v38_variants}
    v40_variants = {k: all_curves[k] for k in v40_variants}

    # ── NIFTYBEES curve on the same index.
    nb_ratio = load_niftybees_curve(common_idx)
    nb_equity = nb_ratio * 100_000.0

    print(f"[search] common window: {common_idx[0].date()} → {common_idx[-1].date()} "
          f"({(common_idx[-1] - common_idx[0]).days / 365.25:.2f} years)")
    print()

    # ── Print SINGLE-VARIANT metrics first (so operator sees each variant
    # at 100% allocation, alone).
    print("=" * 100)
    print("STEP 1 — SINGLE-VARIANT BASELINES (100% allocation, no blend)")
    print("=" * 100)
    print(f"{'variant':<35} CAGR%     MaxDD%   Calmar  Sharpe")
    single_rows = []
    for label, eq in {"NIFTYBEES": nb_equity, **v38_variants, **v40_variants}.items():
        m = metrics(eq, label)
        print(f"{label:<35} {m['cagr_pct']:+6.2f}%  {m['max_dd_pct']:+7.2f}%  "
              f"{m['calmar']:.2f}    {m['sharpe']:.2f}")
        single_rows.append({"variant": label, **m})
    print()

    # ── Identify best V38 + best V40 by absolute CAGR (will be used in
    # the blend search below).
    v38_singletons = {k: metrics(eq, k) for k, eq in v38_variants.items()}
    v40_singletons = {k: metrics(eq, k) for k, eq in v40_variants.items()}
    best_v38 = max(v38_singletons.items(), key=lambda kv: kv[1]["cagr_pct"])
    best_v40 = max(v40_singletons.items(), key=lambda kv: kv[1]["cagr_pct"])
    print(f"[search] best single V38 by CAGR: {best_v38[0]} "
          f"({best_v38[1]['cagr_pct']:+.2f}%)")
    print(f"[search] best single V40 by CAGR: {best_v40[0]} "
          f"({best_v40[1]['cagr_pct']:+.2f}%)")
    print()

    # ── STEP 2 — Single-strategy blends (NB + V38 alone, NB + V40 alone).
    print("=" * 100)
    print("STEP 2 — NIFTYBEES + SINGLE-STRATEGY BLENDS (every V38/V40 variant)")
    print("=" * 100)
    blend_rows = []
    nb_weights = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80]
    for v_kind, variants in (("V38", v38_variants), ("V40", v40_variants)):
        for v_label, eq in variants.items():
            for w_nb in nb_weights:
                w_a = 1.0 - w_nb
                blend = nb_equity * w_nb + eq * w_a
                tag = f"{int(w_nb*100)}NB+{int(w_a*100)}{v_label}"
                m = metrics(blend, tag)
                blend_rows.append({"variant_kind": v_kind, **m})
    df_blends = pd.DataFrame(blend_rows)

    # Sort by Sharpe descending (best risk-adjusted).
    df_blends_top10 = (
        df_blends.sort_values("sharpe", ascending=False).head(15).reset_index(drop=True)
    )
    print("TOP 15 SINGLE-STRATEGY BLENDS BY SHARPE:")
    print(df_blends_top10[
        ["label", "cagr_pct", "max_dd_pct", "calmar", "sharpe"]
    ].to_string(index=False, float_format=lambda x: f"{x:+.2f}"))
    print()

    # ── STEP 3 — Two-strategy blends (NB + best V38 + best V40).
    print("=" * 100)
    print(f"STEP 3 — NIFTYBEES + BEST_V38 ({best_v38[0]}) + BEST_V40 ({best_v40[0]})")
    print("=" * 100)
    best_v38_eq = v38_variants[best_v38[0]]
    best_v40_eq = v40_variants[best_v40[0]]
    multi_rows = []
    for w_nb in nb_weights:
        w_active = 1.0 - w_nb
        for v38_share in (1.0, 0.7, 0.5, 0.3, 0.0):
            v40_share = 1.0 - v38_share
            w_v38 = w_active * v38_share
            w_v40 = w_active * v40_share
            blend = (nb_equity * w_nb) + (best_v38_eq * w_v38) + (best_v40_eq * w_v40)
            tag = f"{int(w_nb*100)}NB + {int(w_v38*100)}V38 + {int(w_v40*100)}V40"
            m = metrics(blend, tag)
            multi_rows.append(m)
    df_multi = pd.DataFrame(multi_rows)
    df_multi_top10 = (
        df_multi.sort_values("sharpe", ascending=False).head(15).reset_index(drop=True)
    )
    print("TOP 15 TWO-STRATEGY BLENDS BY SHARPE:")
    print(df_multi_top10[
        ["label", "cagr_pct", "max_dd_pct", "calmar", "sharpe"]
    ].to_string(index=False, float_format=lambda x: f"{x:+.2f}"))
    print()

    # ── STEP 4 — Candidate selection: does anything BEAT current A?
    print("=" * 100)
    print(f"STEP 4 — CANDIDATES THAT BEAT CURRENT PROFILE A")
    print(f"  (current A: CAGR ≥ {CURRENT_A_CAGR:.2f}%, MaxDD ≤ {-CURRENT_A_MAXDD:.2f}%, "
          f"Calmar ≥ {CURRENT_A_CALMAR}, Sharpe ≥ {CURRENT_A_SHARPE})")
    print("=" * 100)
    all_combos = pd.concat([df_blends, df_multi], ignore_index=True)

    # Strict dominance: ALL four metrics must be ≥ current A (MaxDD comparison flipped — more negative is worse).
    strict = all_combos[
        (all_combos["cagr_pct"] >= CURRENT_A_CAGR) &
        (all_combos["max_dd_pct"] >= CURRENT_A_MAXDD) &
        (all_combos["calmar"] >= CURRENT_A_CALMAR) &
        (all_combos["sharpe"] >= CURRENT_A_SHARPE)
    ].sort_values("sharpe", ascending=False)
    print(f"\nSTRICT DOMINANCE (all 4 metrics ≥ current A): {len(strict)} candidates")
    if not strict.empty:
        print(strict[
            ["label", "cagr_pct", "max_dd_pct", "calmar", "sharpe"]
        ].to_string(index=False, float_format=lambda x: f"{x:+.2f}"))
    else:
        print("  No candidate strictly dominates current Profile A on all 4 metrics.")

    # Pareto frontier: dominates current A on at least 2 metrics.
    pareto_relax = all_combos[
        ((all_combos["cagr_pct"] >= CURRENT_A_CAGR) &
         (all_combos["max_dd_pct"] >= CURRENT_A_MAXDD))  # CAGR no worse, DD no worse
        |
        ((all_combos["cagr_pct"] >= CURRENT_A_CAGR) &
         (all_combos["sharpe"] >= CURRENT_A_SHARPE))      # CAGR + Sharpe both better
        |
        ((all_combos["calmar"] >= CURRENT_A_CALMAR + 0.05) &
         (all_combos["sharpe"] >= CURRENT_A_SHARPE + 0.05))  # risk-adjusted improvement
    ].sort_values("sharpe", ascending=False)
    pareto_relax = pareto_relax.drop_duplicates(subset="label").head(20)
    print(f"\nRELAXED PARETO (improves on CAGR+DD, OR CAGR+Sharpe, OR Calmar+Sharpe meaningfully): "
          f"{len(pareto_relax)} candidates (top 20):")
    if not pareto_relax.empty:
        print(pareto_relax[
            ["label", "cagr_pct", "max_dd_pct", "calmar", "sharpe"]
        ].to_string(index=False, float_format=lambda x: f"{x:+.2f}"))

    # ── STEP 5 — Print headline recommendation.
    print()
    print("=" * 100)
    print("STEP 5 — VERDICT")
    print("=" * 100)
    if not strict.empty:
        top = strict.iloc[0]
        print(f"NEW PROFILE A FOUND: {top['label']}")
        print(f"  CAGR   {top['cagr_pct']:+.2f}%  (vs current A {CURRENT_A_CAGR:+.2f}%, delta {top['cagr_pct']-CURRENT_A_CAGR:+.2f}pp)")
        print(f"  MaxDD  {top['max_dd_pct']:+.2f}%  (vs current A {CURRENT_A_MAXDD:+.2f}%, delta {top['max_dd_pct']-CURRENT_A_MAXDD:+.2f}pp)")
        print(f"  Calmar {top['calmar']:.2f}   (vs current A {CURRENT_A_CALMAR}, delta {top['calmar']-CURRENT_A_CALMAR:+.2f})")
        print(f"  Sharpe {top['sharpe']:.2f}   (vs current A {CURRENT_A_SHARPE}, delta {top['sharpe']-CURRENT_A_SHARPE:+.2f})")
    else:
        print("NO STRICT-DOMINANT REPLACEMENT FOUND.")
        print(f"Current Profile A (70% NB + 30% V38 default) stands:")
        print(f"  CAGR {CURRENT_A_CAGR:+.2f}%, MaxDD {CURRENT_A_MAXDD:+.2f}%, "
              f"Calmar {CURRENT_A_CALMAR}, Sharpe {CURRENT_A_SHARPE}")
        if not pareto_relax.empty:
            top = pareto_relax.iloc[0]
            print()
            print(f"BEST RELAXED-PARETO CANDIDATE (trade-off, not strict win): {top['label']}")
            print(f"  CAGR {top['cagr_pct']:+.2f}% / MaxDD {top['max_dd_pct']:+.2f}% / "
                  f"Calmar {top['calmar']:.2f} / Sharpe {top['sharpe']:.2f}")
            print("  Operator must decide if the trade-off is acceptable.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
