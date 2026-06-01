"""Phase 15 hit-and-trial sweep — try to find a better Profile A than
70% NIFTYBEES + 30% V38(default).

What it does:
    1. Fetch the V4 swing universe ONCE (~20s of yfinance).
    2. Run a battery of V38 parameter combinations (mostly weekly_entry_n
       extensions of the Phase 14 monotonic-improvement finding) on the
       shared history.
    3. Run a battery of V40 v4.1 parameter combinations (top_decile_pct
       sensitivity).
    4. Print a summary table of every variant's CAGR/PF/MaxDD/Calmar/Sharpe.

Output per variant lives at
    logs/backtests/multi_swing_phase15sweep_<run_tag>_2026_06_01/<variant_key>/
The script writes a top-level comparison_sweep.md aggregating all variants
plus the manifest of param overrides used.

Reproducer:
    python tools/_phase15_sweep_2026_06_01.py
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "tools"))

import pandas as pd  # noqa: E402

from research.swing_backtester import EngineParams, run_swing_backtest  # noqa: E402
from multi_swing_backtest_2026_06_01 import (  # noqa: E402
    _build_universe_history,
    _load_spec,
)


# ────────────────────────────────────────────────────────────────────────
# Sweep definitions — each is (variant_key, module_path, param_overrides)
# Bear in mind Phase 14 already ran (V38 defaults), (V38 n=15/exit=8),
# (V38 n=25/exit=12), (V40 v4.1 defaults). Phase 15 fills the rest.
# ────────────────────────────────────────────────────────────────────────

# V38 entry_n extension (exit_m fixed at default 10 to isolate entry effect).
V38_ENTRY_SWEEP: List[Tuple[str, str, Dict[str, Any]]] = [
    ("V38_n25_m10",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 25, "weekly_exit_m": 10}),
    ("V38_n30_m10",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 30, "weekly_exit_m": 10}),
    ("V38_n35_m10",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 35, "weekly_exit_m": 10}),
    ("V38_n40_m10",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 40, "weekly_exit_m": 10}),
]

# V38 trend-filter sensitivity (weekly_sma_regime; default = 40 weeks).
V38_REGIME_SWEEP: List[Tuple[str, str, Dict[str, Any]]] = [
    ("V38_n20_sma20",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 20, "weekly_exit_m": 10, "weekly_sma_regime": 20}),
    ("V38_n20_sma60",
     "strategies.swing_cash.weekly_breakout_v1",
     {"weekly_entry_n": 20, "weekly_exit_m": 10, "weekly_sma_regime": 60}),
]

# V40 v4.1 top_decile_pct sensitivity (default = 0.20).
V40_DECILE_SWEEP: List[Tuple[str, str, Dict[str, Any]]] = [
    ("V40_decile15",
     "strategies.swing_cash.dual_momentum_relstrength_v1",
     {"top_decile_pct": 0.15, "exit_tolerance_pct": 0.05}),
    ("V40_decile25",
     "strategies.swing_cash.dual_momentum_relstrength_v1",
     {"top_decile_pct": 0.25, "exit_tolerance_pct": 0.05}),
    ("V40_decile30",
     "strategies.swing_cash.dual_momentum_relstrength_v1",
     {"top_decile_pct": 0.30, "exit_tolerance_pct": 0.05}),
]

ALL_SWEEPS: List[Tuple[str, str, Dict[str, Any]]] = (
    V38_ENTRY_SWEEP + V38_REGIME_SWEEP + V40_DECILE_SWEEP
)


def main() -> int:
    # Fixed window — matches Phase 13/14 full window for direct comparison.
    end = datetime.now().date()
    start = end - timedelta(days=int(5.0 * 365))
    start_s, end_s = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    tag = "phase15"
    out_root = ROOT / "logs" / "backtests" / f"multi_swing_{tag}sweep_2026_06_01"
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[phase15] output root: {out_root}")
    print(f"[phase15] window: {start_s} → {end_s}")
    print(f"[phase15] sweeps queued: {len(ALL_SWEEPS)}")
    print()

    # ── Fetch shared universe history.
    history = _build_universe_history(start_s, end_s)
    print(f"[phase15] history ready: {len(history)} symbols")
    print()

    # ── Engine params — match Phase 14 defaults exactly. Leave the
    # risk/sizing knobs at module defaults so we cleanly isolate the
    # effect of the STRATEGY param sweep below.
    eng = EngineParams(
        max_concurrent_positions=6,
        sector_cap=None,
    )

    # ── Excluded-from-signals: NIFTYBEES is the benchmark/passive core.
    excluded = {"NIFTYBEES"}

    # ── Run every sweep variant.
    summary_rows: List[Dict[str, Any]] = []
    for variant_key, mod_path, overrides in ALL_SWEEPS:
        print("=" * 80)
        print(f"[phase15] {variant_key} — overrides: {overrides}")
        print("=" * 80)
        t0 = time.time()
        spec = _load_spec(mod_path)
        variant_dir = out_root / variant_key
        result = run_swing_backtest(
            spec,
            history=history,
            capital_inr=100_000.0,
            start=start_s, end=end_s,
            engine_params=eng,
            strategy_params_override=overrides,
            output_dir=variant_dir,
            excluded_from_signals=excluded,
        )
        m = result["metrics"]
        elapsed = time.time() - t0
        print(f"[phase15] {variant_key} done in {elapsed:.1f}s — "
              f"CAGR {m['cagr_pct']:+.2f}% | PF {m['profit_factor']:.2f} | "
              f"MaxDD {m['max_dd_pct']:.2f}% | trades {m['n_trades']}")
        print()
        summary_rows.append({
            "variant": variant_key,
            "module": mod_path.split(".")[-1],
            "overrides": json.dumps(overrides, sort_keys=True),
            "cagr_pct": round(m["cagr_pct"], 2),
            "pf": round(m["profit_factor"], 2),
            "max_dd_pct": round(m["max_dd_pct"], 2),
            "trades": m["n_trades"],
            "wr_pct": round(m["win_rate_pct"], 1),
        })

    # ── Top-level summary.
    print("=" * 90)
    print("PHASE 15 SWEEP SUMMARY")
    print("=" * 90)
    df = pd.DataFrame(summary_rows)
    df = df.sort_values("cagr_pct", ascending=False).reset_index(drop=True)
    print(df.to_string(index=False))
    print()

    # ── Write summary md + manifest.
    md_path = out_root / "comparison_sweep.md"
    md_lines = [
        "# Phase 15 hit-and-trial sweep summary",
        "",
        f"- Window: {start_s} → {end_s}",
        f"- Capital: ₹100,000",
        f"- Engine: max_concurrent=6, sector_cap=None",
        f"- Excluded from signals: NIFTYBEES (passive core reserve)",
        "",
        "Sorted by CAGR (descending):",
        "",
        "| variant | module | overrides | CAGR % | PF | MaxDD % | trades | WR % |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in df.to_dict("records"):
        md_lines.append(
            f"| {r['variant']} | {r['module']} | `{r['overrides']}` | "
            f"{r['cagr_pct']:+.2f} | {r['pf']:.2f} | {r['max_dd_pct']:.2f} | "
            f"{r['trades']} | {r['wr_pct']:.1f} |"
        )
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[phase15] wrote {md_path}")

    manifest_path = out_root / "manifest_sweep.json"
    manifest_path.write_text(
        json.dumps({
            "window": {"start": start_s, "end": end_s},
            "capital_inr": 100_000.0,
            "engine_params": {
                "max_concurrent_positions": 6,
                "sector_cap": None,
            },
            "excluded_from_signals": list(excluded),
            "sweeps": [
                {"variant": v, "module": m, "overrides": o}
                for v, m, o in ALL_SWEEPS
            ],
            "results": summary_rows,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[phase15] wrote {manifest_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
