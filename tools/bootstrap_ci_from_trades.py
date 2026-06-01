"""Bootstrap confidence intervals on backtest metrics — post-hoc analysis.

Given a `trades.csv` from a swing_battery / multi_swing run, resamples
the trades with replacement N times to build an empirical distribution
for CAGR, profit factor, and max drawdown, then prints 95% CI bands.

Answer to the operator's question "is 11% CAGR really 11% or could it
be 5% to 17%?" — this is the script that produces that answer.

The bootstrap treats each trade as the unit of resampling, so the CI
captures "what if I had drawn a different sequence of the same trade
distribution" — it does NOT account for parameter-search overfit (the
walkforward holdout in queue job #9 does that separately).

Usage:
    # Most-common case — point at a swing_battery run dir:
    python tools/bootstrap_ci_from_trades.py \\
        logs/backtests/swing_walkforward_v38_oos_20260601T180000/V38_weekly_breakout/trades.csv \\
        --capital 100000 --n-bootstrap 2000

    # Multiple in one shot (writes summary.json to first run's dir):
    python tools/bootstrap_ci_from_trades.py \\
        logs/backtests/swing_*/V*/trades.csv \\
        --capital 100000

Output: prints CI table to stdout AND writes a `bootstrap_ci.json` next
to the trades.csv with the raw samples for later replotting.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd


def _annualized_cagr(equity_path: List[float], days: int) -> float:
    """Approximate CAGR from a resampled equity path. Days is the
    calendar-day span the original window covered."""
    if not equity_path or equity_path[-1] <= 0:
        return float("nan")
    years = max(days / 365.25, 1e-6)
    return float((equity_path[-1] / equity_path[0]) ** (1 / years) - 1) * 100


def _profit_factor(pnls: np.ndarray) -> float:
    pos = pnls[pnls > 0].sum()
    neg = -pnls[pnls < 0].sum()
    if neg <= 0:
        return float("inf") if pos > 0 else float("nan")
    return float(pos / neg)


def _max_drawdown_pct(equity_path: List[float]) -> float:
    if not equity_path:
        return 0.0
    e = np.asarray(equity_path, dtype=float)
    peak = np.maximum.accumulate(e)
    dd = (e - peak) / peak * 100.0
    return float(dd.min())


def bootstrap_one_file(trades_csv: Path, capital: float, n_bootstrap: int,
                       seed: int = 42) -> dict:
    if not trades_csv.exists():
        raise FileNotFoundError(trades_csv)

    df = pd.read_csv(trades_csv)
    if df.empty:
        return {
            "trades_csv": str(trades_csv),
            "n_trades": 0,
            "error": "no trades in file",
        }

    # Engine B trades.csv schema: net_pnl_inr column is the per-trade INR P&L
    pnl_col = "net_pnl_inr" if "net_pnl_inr" in df.columns else \
              ("pnl_inr" if "pnl_inr" in df.columns else None)
    if pnl_col is None:
        return {
            "trades_csv": str(trades_csv),
            "error": f"no pnl column in {list(df.columns)}",
        }

    pnls_inr = df[pnl_col].astype(float).values
    n_trades = len(pnls_inr)

    # Reconstruct window span from exit_date if present, else use a default.
    if "exit_date" in df.columns and "entry_date" in df.columns:
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        span_days = max((df["exit_date"].max() - df["entry_date"].min()).days, 1)
    else:
        span_days = 5 * 365  # fallback: 5-year window

    rng = np.random.default_rng(seed)
    cagrs: List[float] = []
    pfs: List[float] = []
    dds: List[float] = []

    for _ in range(n_bootstrap):
        sampled = rng.choice(pnls_inr, size=n_trades, replace=True)
        equity = [capital]
        e = capital
        for p in sampled:
            e += float(p)
            equity.append(e)
        cagrs.append(_annualized_cagr(equity, span_days))
        pfs.append(_profit_factor(sampled))
        dds.append(_max_drawdown_pct(equity))

    cagr_arr = np.array([c for c in cagrs if np.isfinite(c)])
    pf_arr = np.array([p for p in pfs if np.isfinite(p)])
    dd_arr = np.array([d for d in dds if np.isfinite(d)])

    def _ci(arr: np.ndarray, low=2.5, high=97.5) -> tuple[float, float, float]:
        if arr.size == 0:
            return float("nan"), float("nan"), float("nan")
        return float(np.percentile(arr, low)), \
               float(np.percentile(arr, 50)), \
               float(np.percentile(arr, high))

    cagr_lo, cagr_med, cagr_hi = _ci(cagr_arr)
    pf_lo, pf_med, pf_hi = _ci(pf_arr)
    dd_lo, dd_med, dd_hi = _ci(dd_arr)  # dd is negative; lo is more negative

    summary = {
        "trades_csv": str(trades_csv),
        "n_trades": n_trades,
        "span_days": span_days,
        "capital_inr": capital,
        "n_bootstrap": n_bootstrap,
        "seed": seed,
        "ci_95": {
            "cagr_pct":  {"lo": round(cagr_lo, 3), "median": round(cagr_med, 3), "hi": round(cagr_hi, 3)},
            "profit_factor": {"lo": round(pf_lo, 3), "median": round(pf_med, 3), "hi": round(pf_hi, 3)},
            "max_dd_pct": {"lo": round(dd_lo, 3), "median": round(dd_med, 3), "hi": round(dd_hi, 3)},
        },
    }

    out_json = trades_csv.parent / "bootstrap_ci.json"
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _print_summary(s: dict) -> None:
    if "error" in s:
        print(f"  ! {s.get('trades_csv', '?')}: {s['error']}")
        return
    ci = s["ci_95"]
    print(f"  {Path(s['trades_csv']).parent.name}")
    print(f"    n_trades={s['n_trades']}  span={s['span_days']}d  "
          f"capital=₹{s['capital_inr']:,.0f}  bootstrap={s['n_bootstrap']}")
    c = ci['cagr_pct']
    p = ci['profit_factor']
    d = ci['max_dd_pct']
    print(f"    CAGR %     : {c['lo']:+.2f}  ··  {c['median']:+.2f}  ··  {c['hi']:+.2f}   (95% CI)")
    print(f"    PF         : {p['lo']:.2f}  ··  {p['median']:.2f}  ··  {p['hi']:.2f}")
    print(f"    MaxDD %    : {d['lo']:+.2f}  ··  {d['median']:+.2f}  ··  {d['hi']:+.2f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("trades_csv", nargs="+",
                    help="One or more trades.csv paths (or glob patterns).")
    ap.add_argument("--capital", type=float, default=100_000.0,
                    help="Starting capital INR for equity reconstruction (default: 100,000).")
    ap.add_argument("--n-bootstrap", type=int, default=2000,
                    help="Number of bootstrap resamples (default: 2000; bumps CI tightness).")
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed for reproducibility (default: 42).")
    args = ap.parse_args(argv)

    # Expand globs (PowerShell doesn't auto-expand; do it ourselves).
    paths: list[Path] = []
    for pat in args.trades_csv:
        matched = [Path(p) for p in glob.glob(pat)]
        if not matched and Path(pat).exists():
            matched = [Path(pat)]
        paths.extend(matched)
    if not paths:
        print(f"[bootstrap_ci] no trades.csv files matched any of {args.trades_csv}",
              file=sys.stderr)
        return 1

    print(f"[bootstrap_ci] {len(paths)} file(s) | "
          f"capital=₹{args.capital:,.0f} | bootstrap={args.n_bootstrap}")
    print()
    for p in paths:
        try:
            summary = bootstrap_one_file(p, args.capital, args.n_bootstrap, args.seed)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {p}: {type(exc).__name__}: {exc}")
            continue
        _print_summary(summary)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
