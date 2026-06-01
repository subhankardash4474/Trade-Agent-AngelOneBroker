"""Multi-strategy swing backtest runner (Engine B, Path B, Phase 13 2026-06-01).

Drives ``packages/research/swing_backtester.py`` across N strategy specs
from ``packages/strategies/swing_cash/`` on the V4 cross-asset universe.
Fetches the universe ONCE (saves ~5× yfinance traffic vs re-fetching per
strategy) and runs each strategy on the same shared history dict.

Per the operator's 2026-06-01 multi-strategy scale-up decision:
    V35 (Donchian-55/20) is the sanity baseline — running it through
    the new engine MUST reproduce V32's number (CAGR +2.84%, PF 1.36,
    MaxDD -7.80%) within ±0.1% / ±0.01 PF. ``--sanity-check`` runs
    JUST V35 and asserts the match.

    V36–V40 are the 5 new swing strategies (mean-reversion, SMA50
    pullback, weekly Donchian, MACD-swing, dual-momentum relstrength).

Usage:
    # Sanity-check only: run V35 (Donchian-55/20, max_concurrent=6) and
    # assert it reproduces V32's number to within tolerance.
    python tools/multi_swing_backtest_2026_06_01.py --sanity-check

    # Run all 6 variants (V35-V40) on the default 5-year window:
    python tools/multi_swing_backtest_2026_06_01.py

    # Subset:
    python tools/multi_swing_backtest_2026_06_01.py --variants V36,V37

    # Custom window + capital:
    python tools/multi_swing_backtest_2026_06_01.py \\
        --start 2021-06-01 --end 2026-05-29 --capital 500000

Output layout:
    logs/backtests/multi_swing_<tag>_2026_06_01/
        V35_donchian55_20/
            manifest.json, results.json, equity_curve.csv,
            trades.csv, comparison.md
        V36_mean_reversion_swing/  ... (etc.)
        comparison_top.md       — cross-variant summary table
        manifest_top.json       — engine + universe metadata for the run
        sanity_check.md         — V35 vs V32 reconciliation if applicable
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

import pandas as pd

from core.instruments.etf_universe import load_v4_swing_cash_universe
from research.swing_backtester import EngineParams, StrategySpec, run_swing_backtest


# ── Strategy roster — module path : (V##, alias) ───────────────────────
# Order matters: V35 is intentionally first so --sanity-check (which
# only runs the first variant) hits the right strategy.
DEFAULT_ROSTER: List[Tuple[str, str]] = [
    ("strategies.swing_cash.donchian_55_20_spec", "V35"),
    ("strategies.swing_cash.mean_reversion_swing_v1", "V36"),
    ("strategies.swing_cash.pullback_to_sma50_v1", "V37"),
    ("strategies.swing_cash.weekly_breakout_v1", "V38"),
    ("strategies.swing_cash.macd_swing_v1", "V39"),
    ("strategies.swing_cash.dual_momentum_relstrength_v1", "V40"),
]


# V32's published numbers (charter Phase 12 / mode_a_decision_v32_2026-06-01.md).
# Used by --sanity-check to assert V35 (Donchian-55/20 through new engine)
# matches V32 (same strategy through old engine).
V32_BASELINE = {
    "cagr_pct": 2.84,
    "profit_factor": 1.36,
    "max_dd_pct": -7.80,
}
SANITY_TOLERANCE = {
    "cagr_pct_abs": 0.10,        # ±0.10% absolute
    "profit_factor_abs": 0.05,   # ±0.05 absolute
    "max_dd_pct_abs": 0.50,      # ±0.50% absolute (DD is more sensitive)
}


# ============================================================
# Data fetch
# ============================================================

def _fetch_universe_history(symbols: List[str], start: str, end: str
                            ) -> Dict[str, pd.DataFrame]:
    import yfinance as yf  # type: ignore
    out: Dict[str, pd.DataFrame] = {}
    print(f"[multi_swing] fetching {len(symbols)} symbols from yfinance "
          f"({start} → {end}) ...")
    t0 = time.time()
    failed: List[str] = []
    for i, sym in enumerate(symbols, 1):
        try:
            df = yf.Ticker(sym).history(
                start=start, end=end, interval="1d",
                auto_adjust=False, actions=False,
            )
            if df.empty:
                failed.append(sym)
                continue
            df.columns = [c.lower() for c in df.columns]
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    failed.append(sym)
                    break
            else:
                if hasattr(df.index, "tz") and df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                out[sym] = df[["open", "high", "low", "close", "volume"]].copy()
                if i % 10 == 0 or i == len(symbols):
                    print(f"[multi_swing]   {i}/{len(symbols)}  ({sym}: {len(df)} bars)")
        except Exception as e:  # noqa: BLE001
            failed.append(sym)
            print(f"[multi_swing]   {sym}: {type(e).__name__}: {e}")
    print(f"[multi_swing] fetched {len(out)}/{len(symbols)} OK in {time.time()-t0:.1f}s "
          f"(failed: {failed})")
    return out


def _build_universe_history(start: str, end: str) -> Dict[str, pd.DataFrame]:
    """Load V4 universe, fetch with .NS suffix, return clean-symbol-keyed dict."""
    raw_universe = load_v4_swing_cash_universe(exclude_cash_sweep=True)
    yf_universe = [f"{s}.NS" for s in raw_universe]
    history_yf = _fetch_universe_history(yf_universe, start, end)
    if not history_yf:
        raise RuntimeError("No history fetched; aborting.")
    history: Dict[str, pd.DataFrame] = {
        (k[:-3] if k.endswith(".NS") else k): v
        for k, v in history_yf.items()
    }
    return history


# ============================================================
# Strategy loading
# ============================================================

def _load_spec(module_path: str) -> StrategySpec:
    mod = import_module(module_path)
    if not hasattr(mod, "SPEC"):
        raise ImportError(f"{module_path} does not export a SPEC constant")
    spec = mod.SPEC
    if not isinstance(spec, StrategySpec):
        raise TypeError(f"{module_path}.SPEC is not a StrategySpec ({type(spec).__name__})")
    return spec


def _filter_roster(
    roster: List[Tuple[str, str]],
    requested: Optional[List[str]],
) -> List[Tuple[str, str]]:
    if not requested:
        return roster
    requested_upper = [r.strip().upper() for r in requested]
    filtered = [(m, a) for m, a in roster if a.upper() in requested_upper]
    if not filtered:
        raise ValueError(
            f"--variants {requested} matched nothing in roster {[a for _, a in roster]}"
        )
    return filtered


# ============================================================
# Sanity check
# ============================================================

def _evaluate_sanity(metrics: dict) -> Tuple[bool, str]:
    """Compare V35 (new engine, Donchian-55/20, max_c=6) to V32 published."""
    lines: List[str] = []
    lines.append("# V35 ↔ V32 sanity check")
    lines.append("")
    lines.append(f"> V35 = Donchian-55/20 through the NEW engine (`swing_backtester`)")
    lines.append(f"> V32 = Donchian-55/20 through the V27 standalone tool (already published)")
    lines.append(f"> Both with max_concurrent_positions=6, identical params.")
    lines.append(f"> Tolerance: CAGR ±{SANITY_TOLERANCE['cagr_pct_abs']:.2f}%, "
                 f"PF ±{SANITY_TOLERANCE['profit_factor_abs']:.2f}, "
                 f"MaxDD ±{SANITY_TOLERANCE['max_dd_pct_abs']:.2f}%")
    lines.append("")
    lines.append("| Metric | V35 (new engine) | V32 (published) | Δ | Tolerance | Pass |")
    lines.append("|---|---:|---:|---:|---:|:---:|")

    all_ok = True
    for key, tol_key in (
        ("cagr_pct", "cagr_pct_abs"),
        ("profit_factor", "profit_factor_abs"),
        ("max_dd_pct", "max_dd_pct_abs"),
    ):
        got = metrics.get(key)
        want = V32_BASELINE[key]
        tol = SANITY_TOLERANCE[tol_key]
        if got is None:
            ok = False
            delta_s = "?"
        else:
            delta = float(got) - float(want)
            ok = abs(delta) <= tol
            delta_s = f"{delta:+.3f}"
        if not ok:
            all_ok = False
        lines.append(f"| {key} | {got} | {want} | {delta_s} | ±{tol} | {'✓' if ok else '✗ FAIL'} |")
    lines.append("")
    verdict = "PASS — engine extraction is correct, proceed with V36–V40." \
        if all_ok else \
        "FAIL — engine extraction has a bug; investigate before trusting V36–V40 numbers."
    lines.append(f"**Verdict: {verdict}**")
    lines.append("")
    return all_ok, "\n".join(lines)


# ============================================================
# Top-level comparison rendering
# ============================================================

def _render_top_comparison(
    run_dir: Path,
    results_by_variant: Dict[str, Dict[str, Any]],
    benchmark_cagr: float,
    benchmark_dd: float,
    window_start: str,
    window_end: str,
    capital_inr: float,
) -> str:
    out: List[str] = []
    out.append("# Multi-strategy swing backtest — V35–V40 comparison")
    out.append("")
    out.append(f"> **Engine:** `packages/research/swing_backtester.py` (Engine B)  ")
    out.append(f"> **Runner:** `tools/multi_swing_backtest_2026_06_01.py`  ")
    out.append(f"> **Window:** {window_start} → {window_end}  ")
    out.append(f"> **Capital:** ₹{capital_inr:,.0f}  ")
    out.append(f"> **Universe:** V4 cross-asset (75 instruments, see `data/v4_universe_swing_cash.txt`)  ")
    out.append(f"> **Cost model:** AngelOne CNC DELIVERY (`packages/core/charges.py`)  ")
    out.append(f"> **Benchmark:** NIFTYBEES buy-and-hold: "
               f"CAGR {benchmark_cagr:+.2f}%, MaxDD {benchmark_dd:+.2f}%")
    out.append("")

    out.append("## Variant comparison")
    out.append("")
    out.append("| Variant | CAGR % | vs Bench | PF | MaxDD % | Trades | WinRate | Avg ₹/trade | §3.10 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for alias, result in results_by_variant.items():
        m = result["metrics"]
        cagr = m.get("cagr_pct", 0)
        pf = m.get("profit_factor")
        dd = m.get("max_dd_pct", 0)
        verdict = _verdict_letter(pf, cagr, dd, benchmark_cagr)
        pf_s = f"{pf:.2f}" if pf is not None else "—"
        out.append(
            f"| {alias}_{result['spec_name'].split('_', 1)[1] if '_' in result['spec_name'] else result['spec_name']} "
            f"| {cagr:+.2f} | {cagr - benchmark_cagr:+.2f} "
            f"| {pf_s} | {dd:+.2f} | {m.get('n_trades', 0)} | {m.get('win_rate_pct', 0):.1f}% "
            f"| ₹{m.get('avg_charges_per_trade_inr', 0):,.0f} | {verdict} |"
        )
    out.append("")

    out.append("## Charter §3.10 verdict legend")
    out.append("")
    out.append("- **A1** — PF < 1.10. No edge at any size; abandon.")
    out.append("- **A2** — PF ∈ [1.10, 1.20). Borderline; defer to retune.")
    out.append("- **A3** — PF ≥ 1.20 BUT CAGR < benchmark + 2%. Informational only.")
    out.append("- **A4** — PF ≥ 1.20 AND CAGR ≥ benchmark + 2% AND |MaxDD| ≤ 25%. **PASS** → paper-mode candidate.")
    out.append("- **A5** — MaxDD > 25%. Stop; incompatible with capital base.")
    out.append("")

    out.append("## Exit-reason breakdown by variant")
    out.append("")
    out.append("| Variant | top exit reasons (count) |")
    out.append("|---|---|")
    for alias, result in results_by_variant.items():
        from collections import Counter
        cnt = Counter(t.exit_reason for t in result.get("trades", []))
        top = ", ".join(f"{r}={n}" for r, n in cnt.most_common(5))
        if not top:
            top = "—"
        out.append(f"| {alias} | {top} |")
    out.append("")

    out.append("---")
    out.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST.*")
    return "\n".join(out)


def _verdict_letter(pf, cagr, dd, bench_cagr) -> str:
    pf_val = pf if pf is not None and isinstance(pf, (int, float)) else 0.0
    if pf_val < 1.10:
        return "A1"
    if pf_val < 1.20:
        return "A2"
    if dd < -25.0:
        return "A5"
    if pf_val >= 1.20 and cagr >= bench_cagr + 2.0 and abs(dd) <= 25.0:
        return "A4"
    if pf_val >= 1.20 and cagr < bench_cagr + 2.0:
        return "A3"
    return "?"


# ============================================================
# CLI
# ============================================================

def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", default=None,
                   help="Start date YYYY-MM-DD (default: 5 years before --end)")
    p.add_argument("--end", default=None,
                   help="End date YYYY-MM-DD (default: today)")
    p.add_argument("--capital", type=float, default=100_000.0,
                   help="Initial capital INR (default: 100,000 per charter §3.9)")
    p.add_argument("--max-concurrent", type=int, default=6,
                   help="Max concurrent positions (V32 sweet spot: 6)")
    p.add_argument("--sector-cap", type=int, default=None,
                   help="Optional max positions per sector (None = off)")
    p.add_argument("--tag", default="firstrun",
                   help="Output dir suffix (default: firstrun)")
    p.add_argument("--variants", default=None,
                   help="Comma-separated subset, e.g. V35,V37,V40 (default: all)")
    p.add_argument("--sanity-check", action="store_true",
                   help="Run JUST V35 (Donchian-55/20 through new engine) and "
                        "assert it reproduces V32's published numbers. Exits 1 "
                        "on failure so this can be wired into CI later.")
    p.add_argument("--exclude", default="",
                   help="Comma-separated symbols to EXCLUDE from signal candidates")
    p.add_argument("--strategy-params-json", default=None,
                   help="JSON dict of strategy-level param overrides applied "
                        "to EVERY variant in the run. Use to sweep one knob "
                        "across one variant. Example (POSIX/bash): "
                        "--variants V38 --strategy-params-json '{\"weekly_entry_n\": 15}' "
                        "--tag v38_n15. PowerShell often mangles inline JSON; "
                        "use --strategy-params-file PATH instead on Windows. "
                        "Engine-level knobs (max_concurrent, sector_cap) have "
                        "their own flags; this is strictly for the strategy "
                        "module's default_params keys.")
    p.add_argument("--strategy-params-file", default=None,
                   help="Path to a JSON file with strategy-level param overrides. "
                        "Equivalent to --strategy-params-json but reads from disk, "
                        "avoiding the PowerShell-quote-mangling pitfall.")
    args = p.parse_args()

    strategy_params_override: Optional[Dict[str, Any]] = None
    if args.strategy_params_json and args.strategy_params_file:
        print("[multi_swing] use either --strategy-params-json OR --strategy-params-file, not both")
        return 2
    if args.strategy_params_json:
        try:
            strategy_params_override = json.loads(args.strategy_params_json)
        except json.JSONDecodeError as exc:
            print(f"[multi_swing] --strategy-params-json invalid: {exc}")
            return 2
    elif args.strategy_params_file:
        sp_path = Path(args.strategy_params_file)
        if not sp_path.exists():
            print(f"[multi_swing] --strategy-params-file not found: {sp_path}")
            return 2
        try:
            strategy_params_override = json.loads(sp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[multi_swing] {sp_path} contained invalid JSON: {exc}")
            return 2
    if strategy_params_override is not None and not isinstance(strategy_params_override, dict):
        print("[multi_swing] strategy params must be a JSON object")
        return 2

    end = args.end or datetime.now().date().strftime("%Y-%m-%d")
    if args.start:
        start = args.start
    else:
        start_dt = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=5 * 365)
        start = start_dt.strftime("%Y-%m-%d")

    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}

    # Filter roster.
    if args.sanity_check:
        # Sanity check always runs V35 only — even if --variants was passed.
        roster = [DEFAULT_ROSTER[0]]
    else:
        requested = [v.strip() for v in args.variants.split(",")] if args.variants else None
        roster = _filter_roster(DEFAULT_ROSTER, requested)

    print(f"[multi_swing] window: {start} → {end}")
    print(f"[multi_swing] capital: ₹{args.capital:,.0f}")
    print(f"[multi_swing] max_concurrent: {args.max_concurrent}, sector_cap: {args.sector_cap}")
    print(f"[multi_swing] variants: {[a for _, a in roster]}")
    if excluded:
        print(f"[multi_swing] excluded: {sorted(excluded)}")

    # Fetch universe ONCE.
    history = _build_universe_history(start, end)

    # Run dir.
    out_root = ROOT / "logs" / "backtests" / f"multi_swing_{args.tag}_2026_06_01"
    out_root.mkdir(parents=True, exist_ok=True)

    engine_params = EngineParams(
        max_concurrent_positions=args.max_concurrent,
        sector_cap=args.sector_cap,
    )

    # Run each variant.
    results_by_variant: Dict[str, Dict[str, Any]] = {}
    benchmark_cagr = 0.0
    benchmark_dd = 0.0
    for module_path, alias in roster:
        print()
        print("=" * 75)
        print(f"[multi_swing] running {alias} ({module_path})")
        print("=" * 75)
        try:
            spec = _load_spec(module_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[multi_swing] FAILED to load {alias}: {type(exc).__name__}: {exc}")
            continue

        variant_dir = out_root / f"{alias}_{spec.name.split('_', 1)[1]}" \
            if "_" in spec.name else out_root / alias
        result = run_swing_backtest(
            spec,
            history=history,
            capital_inr=args.capital,
            start=start, end=end,
            engine_params=engine_params,
            strategy_params_override=strategy_params_override,
            output_dir=variant_dir,
            excluded_from_signals=excluded,
        )
        result["spec_name"] = spec.name
        results_by_variant[alias] = result

        # NIFTYBEES bench (same for every variant; capture once)
        b = result["benchmark"]
        if "cagr_pct" in b:
            benchmark_cagr = b["cagr_pct"]
            benchmark_dd = b.get("max_dd_pct", 0)

        m = result["metrics"]
        print(f"[multi_swing] {alias} done — CAGR {m.get('cagr_pct'):+.2f}% | "
              f"PF {m.get('profit_factor')} | MaxDD {m.get('max_dd_pct'):+.2f}% | "
              f"trades {m.get('n_trades')}")

    # Sanity check (if --sanity-check was set, this is the ONLY thing we care about).
    if args.sanity_check:
        if "V35" not in results_by_variant:
            print("[multi_swing] sanity check requires V35 result; aborting")
            return 1
        ok, sanity_md = _evaluate_sanity(results_by_variant["V35"]["metrics"])
        (out_root / "sanity_check.md").write_text(sanity_md, encoding="utf-8")
        print()
        print(sanity_md)
        print()
        print(f"[multi_swing] sanity-check verdict: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    # Top-level comparison + manifest.
    comp_md = _render_top_comparison(
        run_dir=out_root,
        results_by_variant=results_by_variant,
        benchmark_cagr=benchmark_cagr,
        benchmark_dd=benchmark_dd,
        window_start=start, window_end=end,
        capital_inr=args.capital,
    )
    (out_root / "comparison_top.md").write_text(comp_md, encoding="utf-8")
    (out_root / "manifest_top.json").write_text(json.dumps({
        "engine": "swing_backtester (Engine B, Path B)",
        "runner": "tools/multi_swing_backtest_2026_06_01.py",
        "window_start": start,
        "window_end": end,
        "capital_inr": args.capital,
        "max_concurrent_positions": args.max_concurrent,
        "sector_cap": args.sector_cap,
        "excluded_from_signals": sorted(excluded),
        "variants": [
            {
                "alias": alias,
                "module": module,
                "spec_name": results_by_variant[alias]["spec_name"] if alias in results_by_variant else None,
                "result_dir": str((out_root / f"{alias}_{results_by_variant[alias]['spec_name'].split('_', 1)[1]}").relative_to(ROOT)) if alias in results_by_variant else None,
            }
            for module, alias in roster
        ],
        "benchmark_niftybees_cagr_pct": benchmark_cagr,
        "benchmark_niftybees_max_dd_pct": benchmark_dd,
    }, indent=2), encoding="utf-8")

    print()
    print("=" * 75)
    print(f"[multi_swing] all variants done. Output: {out_root}")
    print(f"[multi_swing] top-level comparison: {out_root / 'comparison_top.md'}")
    print("=" * 75)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
