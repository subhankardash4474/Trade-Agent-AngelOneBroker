"""Engine B cloud runner — multi-strategy swing backtest under the
backtester VM's docker harness.

Mirrors `packages/research/battery.py` (Engine A / EnsembleBacktester /
V1-V26) for the Engine B / swing_backtester / V35-V40 family. The
backtester VM's queue scheduler (`tools/run_battery_queue.py`) treats
both engines uniformly: same docker bind-mounts, same `BACKTESTER_MODE=1`
isolation guard, same `<run_id>` folder convention, same
`--resume`/`--run-id` semantics.

Where battery.py runs EnsembleBacktester (intraday ensemble + v3 swing
CNC), this runs swing_backtester (V35-V40 multi-strategy specs from
`packages/strategies/swing_cash/`).

Output layout (under `logs/backtests/<run_id>/`):
    market_data.pkl                   — cached yfinance history (for --resume)
    market_data.pkl.sha256            — integrity sidecar
    manifest_top.json                 — run-level metadata
    comparison_top.md                 — cross-variant ranking
    <alias>_<spec_suffix>/            — per-variant subdir
        manifest.json
        results.json
        equity_curve.csv
        trades.csv
        comparison.md
    <alias>.failure.txt               — traceback if a variant crashed

CLI (called by run_battery_queue.py via docker run):
    python tools/run_swing_battery.py \\
        --variants V35 V38 V40 \\
        --start 2011-06-01 --end 2026-06-01 \\
        --capital 100000 \\
        --max-concurrent 6 \\
        --universe-file data/v4_universe_swing_cash.txt \\
        --strategy-params-file data/sweep_params/v38_n25_m12_2026-06-01.json \\
        --run-id swing_15year_v35_v40_20260601T140000

Resume semantics (mirrors battery.py):
    --resume <run_id>     : re-load market_data.pkl + skip variants
                            whose results.json already exists + no
                            failure.txt sibling. Same `_clean_completed_*`
                            invariants as the ensemble path.
    --run-id <run_id>     : pin the folder name but start fresh; no skip.
    Mutually exclusive (queue scheduler enforces; we re-check defensively).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import pickle
import sys
import time
import traceback
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import hashlib

# Force UTF-8 on Windows hosts (laptop test runs); inside docker stdout
# is already UTF-8 so the wrap is a no-op there.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Path bootstrap (mirrors battery.py's prelude) ────────────────────
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "packages") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages"))

import pandas as pd  # noqa: E402

from core.instruments.etf_universe import load_v4_swing_cash_universe  # noqa: E402
from research.swing_backtester import EngineParams, run_swing_backtest  # noqa: E402
from research.battery import _assert_backtester_isolation  # noqa: E402  (shared guard)


# ── Roster (V35-V40 default; subset via --variants) ──────────────────
# Same identifiers and order as tools/multi_swing_backtest_2026_06_01.py
# so operator muscle memory transfers. Module paths are import strings,
# NOT filesystem paths.
DEFAULT_ROSTER: List[Tuple[str, str]] = [
    ("strategies.swing_cash.donchian_55_20_spec", "V35"),
    ("strategies.swing_cash.mean_reversion_swing_v1", "V36"),
    ("strategies.swing_cash.pullback_to_sma50_v1", "V37"),
    ("strategies.swing_cash.weekly_breakout_v1", "V38"),
    ("strategies.swing_cash.macd_swing_v1", "V39"),
    ("strategies.swing_cash.dual_momentum_relstrength_v1", "V40"),
]


# ── Logging helpers (battery.py uses loguru; we keep stdout-only
# because the queue scheduler captures it via docker logs) ────────────
def _log(msg: str) -> None:
    """Time-prefixed stdout. The queue scheduler tails docker logs for
    these lines; keeping the prefix lets the scheduler grep by '[swing]'."""
    print(f"[swing] {msg}", flush=True)


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ============================================================
# Market data cache (parity with battery.py:_save/_load_market_data_cache)
# ============================================================
def _cache_path(out_root: Path) -> Path:
    return out_root / "market_data.pkl"


def _save_market_data_cache(market_data: Dict[str, pd.DataFrame], out_root: Path) -> None:
    """Pickle + sidecar SHA256. Mirrors battery.py:PERF-13 invariant so
    operators see the same file shape under both engines' run dirs."""
    cache = _cache_path(out_root)
    try:
        with cache.open("wb") as f:
            pickle.dump(market_data, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN failed to write market_data.pkl: {exc!r}")
        return

    try:
        h = hashlib.sha256()
        with cache.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        cache.with_suffix(".pkl.sha256").write_text(h.hexdigest(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN failed to write sha256 sidecar: {exc!r}")


def _load_market_data_cache(out_root: Path) -> Optional[Dict[str, pd.DataFrame]]:
    cache = _cache_path(out_root)
    if not cache.exists():
        return None
    try:
        with cache.open("rb") as f:
            data = pickle.load(f)
        if not isinstance(data, dict) or not data:
            _log(f"WARN market_data.pkl at {cache} is empty / wrong shape; ignoring")
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN failed to load market_data.pkl: {exc!r}; will refetch")
        return None


# ============================================================
# Universe + data fetch
# ============================================================
def _load_universe(universe_file: Optional[Path]) -> List[str]:
    """Load symbol list. Default = V4 swing-cash universe (75 instruments,
    74 signal candidates after LIQUIDBEES exclusion). Custom file uses
    the same loader format (one symbol per line, # comments)."""
    if universe_file is None:
        return load_v4_swing_cash_universe(exclude_cash_sweep=True)

    # Custom universe-file: parse same shape as the canonical file but
    # without the size-enforcement check (operator-supplied universes
    # may legitimately be 100+ for midcap experiments).
    if not universe_file.exists():
        raise SystemExit(f"[swing][FATAL] universe-file not found: {universe_file}")
    symbols: List[str] = []
    seen = set()
    for line in universe_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s in seen:
            continue
        symbols.append(s)
        seen.add(s)
    return symbols


def _fetch_universe_history(symbols: List[str], start: str, end: str
                            ) -> Dict[str, pd.DataFrame]:
    """Sequential yfinance fetch. Matches the laptop runner's behaviour;
    keeps the implementation simple (no asyncio, no thread pool) so
    failure modes are obvious in `docker logs`."""
    import yfinance as yf  # local import — only needed at fetch time

    out: Dict[str, pd.DataFrame] = {}
    _log(f"fetching {len(symbols)} symbols from yfinance ({start} → {end}) ...")
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
            missing = [c for c in ("open", "high", "low", "close", "volume")
                       if c not in df.columns]
            if missing:
                failed.append(sym)
                continue
            if hasattr(df.index, "tz") and df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            out[sym] = df[["open", "high", "low", "close", "volume"]].copy()
            if i % 25 == 0 or i == len(symbols):
                _log(f"  {i}/{len(symbols)} ({sym}: {len(df)} bars)")
        except Exception as exc:  # noqa: BLE001
            failed.append(sym)
            _log(f"  {sym}: {type(exc).__name__}: {exc}")
    _log(f"fetched {len(out)}/{len(symbols)} OK in {time.time()-t0:.1f}s "
         f"(failed: {len(failed)})")
    return out


def _build_history(symbols: List[str], start: str, end: str
                   ) -> Dict[str, pd.DataFrame]:
    """Fetch with .NS suffix, strip to clean keys."""
    yf_symbols = [f"{s}.NS" for s in symbols]
    history_yf = _fetch_universe_history(yf_symbols, start, end)
    if not history_yf:
        raise RuntimeError("No history fetched; aborting.")
    return {
        (k[:-3] if k.endswith(".NS") else k): v
        for k, v in history_yf.items()
    }


# ============================================================
# Strategy spec loading + per-variant runner
# ============================================================
def _load_spec(module_path: str):
    """Import a strategy module and return its SPEC constant."""
    mod = import_module(module_path)
    if not hasattr(mod, "SPEC"):
        raise ImportError(f"{module_path} does not export a SPEC constant")
    return mod.SPEC


def _filter_roster(roster: List[Tuple[str, str]],
                   requested: Optional[List[str]]) -> List[Tuple[str, str]]:
    if not requested:
        return roster
    req = {r.strip().upper() for r in requested}
    filtered = [(m, a) for m, a in roster if a.upper() in req]
    if not filtered:
        raise SystemExit(
            f"[swing][FATAL] --variants {requested} matched nothing in "
            f"roster {[a for _, a in roster]}"
        )
    return filtered


def _variant_dir(out_root: Path, alias: str, spec_name: str) -> Path:
    """Per-variant subdirectory. Mirrors multi_swing's
    '<alias>_<spec_suffix>' layout."""
    suffix = spec_name.split("_", 1)[1] if "_" in spec_name else spec_name
    return out_root / f"{alias}_{suffix}"


def _variant_completed(variant_dir: Path) -> bool:
    """Resume gate: a variant is COMPLETE iff results.json exists AND
    no failure.txt sibling exists. Mirrors battery.py's
    `_completed_variant_names()` invariant. A corrupt JSON or stale
    failure.txt invalidates the resume skip — the runner re-runs.
    """
    results = variant_dir / "results.json"
    failure = variant_dir.parent / f"{variant_dir.name}.failure.txt"
    if failure.exists():
        return False
    if not results.exists():
        return False
    try:
        json.loads(results.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return True


# ============================================================
# Top-level comparison
# ============================================================
def _render_top_comparison(
    out_root: Path,
    results_by_variant: Dict[str, Dict[str, Any]],
    benchmark_cagr: float,
    benchmark_dd: float,
    window_start: str,
    window_end: str,
    capital_inr: float,
    universe_size: int,
    strategy_params_override: Optional[Dict[str, Any]],
) -> str:
    """Markdown comparison.md at the run-root. Same shape as
    multi_swing_backtest_2026_06_01.py's _render_top_comparison so
    operators see consistent output across local + cloud."""
    out: List[str] = []
    out.append("# Multi-strategy swing backtest — cloud run")
    out.append("")
    out.append(f"> **Engine:** `packages/research/swing_backtester.py` (Engine B)  ")
    out.append(f"> **Runner:** `tools/run_swing_battery.py` (cloud / queue-driven)  ")
    out.append(f"> **Window:** {window_start} → {window_end}  ")
    out.append(f"> **Capital:** ₹{capital_inr:,.0f}  ")
    out.append(f"> **Universe:** {universe_size} instruments  ")
    out.append(f"> **Cost model:** AngelOne CNC DELIVERY (`packages/core/charges.py`)  ")
    out.append(f"> **Benchmark:** NIFTYBEES buy-and-hold: "
               f"CAGR {benchmark_cagr:+.2f}%, MaxDD {benchmark_dd:+.2f}%  ")
    if strategy_params_override:
        out.append(f"> **Strategy params override:** "
                   f"`{json.dumps(strategy_params_override)}`  ")
    out.append("")

    out.append("## Variant comparison")
    out.append("")
    out.append("| Variant | CAGR % | vs Bench | PF | MaxDD % | Trades | WinRate | Avg ₹/trade | §3.10 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for alias, result in results_by_variant.items():
        m = result["metrics"]
        cagr = m.get("cagr_pct", 0) or 0
        pf = m.get("profit_factor")
        dd = m.get("max_dd_pct", 0) or 0
        pf_s = f"{pf:.2f}" if pf is not None else "—"
        verdict = _verdict_letter(pf, cagr, dd, benchmark_cagr)
        out.append(
            f"| {alias} | {cagr:+.2f} | {cagr - benchmark_cagr:+.2f} | {pf_s} "
            f"| {dd:+.2f} | {m.get('n_trades', 0)} "
            f"| {m.get('win_rate_pct', 0):.1f}% "
            f"| ₹{m.get('avg_charges_per_trade_inr', 0):,.0f} | {verdict} |"
        )
    out.append("")

    out.append("## Verdict legend (charter §3.10)")
    out.append("")
    out.append("- **A1** PF<1.10 — no edge; abandon.")
    out.append("- **A2** PF∈[1.10,1.20) — borderline; retune.")
    out.append("- **A3** PF≥1.20 BUT CAGR<bench+2% — informational only.")
    out.append("- **A4** PF≥1.20 AND CAGR≥bench+2% AND |MaxDD|≤25% — **PASS**.")
    out.append("- **A5** MaxDD>25% — stop; incompatible with capital base.")
    out.append("")

    out.append(f"---")
    out.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST "
               f"by `tools/run_swing_battery.py`.*")
    return "\n".join(out)


def _verdict_letter(pf, cagr, dd, bench_cagr) -> str:
    pf_val = pf if isinstance(pf, (int, float)) and pf is not None else 0.0
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
# Main entrypoint
# ============================================================
def main(argv: Optional[List[str]] = None) -> int:
    """Mirror of `packages/research/battery.py:main()`'s structure:
    isolation guard → argparse → resolve run_id → fetch/cache market data
    → per-variant loop with resume skip → top-level comparison + manifest.

    Exit codes (same convention as battery.py):
        0  all variants OK
        2  CLI / arg error
        3  some variants failed (partial)
        4  fatal — cache or fetch fundamentally broken
        6  --resume and --run-id both supplied
        9  BACKTESTER_MODE=1 but broker creds present (isolation guard)
    """
    _assert_backtester_isolation()

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    # Universe + window
    ap.add_argument("--universe-file", default=None,
                    help="Path to universe file (one symbol/line, # comments). "
                         "Default: data/v4_universe_swing_cash.txt (75 instruments).")
    ap.add_argument("--start", default=None,
                    help="ISO start date YYYY-MM-DD. Mutually exclusive with --days.")
    ap.add_argument("--end", default=None,
                    help="ISO end date YYYY-MM-DD. Defaults to today (UTC).")
    ap.add_argument("--days", type=int, default=None,
                    help="Alternative to --start: window is the last N calendar days "
                         "ending at --end (or today). Mutually exclusive with --start.")

    # Strategy roster + params
    ap.add_argument("--variants", nargs="+", default=None,
                    help="Subset of variants (V35..V40). Default: all 6.")
    ap.add_argument("--strategy-params-file", default=None,
                    help="JSON file of strategy-level param overrides (e.g. "
                         "{'weekly_entry_n': 25, 'weekly_exit_m': 12}). "
                         "Applied to EVERY variant in the run.")

    # Engine knobs
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--max-concurrent", type=int, default=6)
    ap.add_argument("--sector-cap", type=int, default=None)
    ap.add_argument("--exclude", default="",
                    help="Comma-separated symbols to EXCLUDE from signal "
                         "candidates (kept in history for benchmark reference).")

    # Output / resume (matches battery.py)
    ap.add_argument("--run-id", default=None,
                    help="Pin a deterministic run_id. Default: "
                         "swing_<auto>_<utc_ts>. Mutually exclusive with --resume.")
    ap.add_argument("--resume", default=None,
                    help="Resume an existing run_id (skip completed variants, "
                         "reuse market_data.pkl). Mutually exclusive with --run-id.")

    args = ap.parse_args(argv)

    # ── Mutex checks ──
    if args.resume and args.run_id:
        _log("[FATAL] --resume and --run-id are mutually exclusive")
        return 6
    if args.start and args.days:
        _log("[FATAL] --start and --days are mutually exclusive")
        return 2

    # ── Resolve window ──
    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.start:
        start = args.start
    elif args.days:
        start_dt = datetime.strptime(end, "%Y-%m-%d") - pd.Timedelta(days=args.days)
        start = start_dt.strftime("%Y-%m-%d")
    else:
        # Default = 5 years (parity with multi_swing CLI default)
        start_dt = datetime.strptime(end, "%Y-%m-%d") - pd.Timedelta(days=5 * 365)
        start = start_dt.strftime("%Y-%m-%d")

    # ── Resolve run_id + out_root ──
    if args.resume:
        run_id = args.resume
    elif args.run_id:
        run_id = args.run_id
    else:
        run_id = f"swing_auto_{_utc_ts()}"

    out_root = ROOT / "logs" / "backtests" / run_id
    out_root.mkdir(parents=True, exist_ok=True)

    # ── Resolve roster ──
    roster = _filter_roster(DEFAULT_ROSTER, args.variants)

    # ── Resolve strategy-params override ──
    strategy_params_override: Optional[Dict[str, Any]] = None
    if args.strategy_params_file:
        spf = Path(args.strategy_params_file)
        if not spf.is_absolute():
            spf = ROOT / spf
        if not spf.exists():
            _log(f"[FATAL] --strategy-params-file not found: {spf}")
            return 2
        try:
            strategy_params_override = json.loads(spf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _log(f"[FATAL] {spf} invalid JSON: {exc}")
            return 2

    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}

    # ── Resolve universe ──
    universe_path = None
    if args.universe_file:
        universe_path = Path(args.universe_file)
        if not universe_path.is_absolute():
            universe_path = ROOT / universe_path
    symbols = _load_universe(universe_path)

    # ── Banner ──
    _log("=" * 70)
    _log(f"run_id          : {run_id}")
    _log(f"out_root        : {out_root.relative_to(ROOT)}")
    _log(f"window          : {start} → {end}")
    _log(f"capital         : ₹{args.capital:,.0f}")
    _log(f"max_concurrent  : {args.max_concurrent}  sector_cap: {args.sector_cap}")
    _log(f"universe        : {len(symbols)} symbols "
         f"(source: {'default' if universe_path is None else universe_path.name})")
    _log(f"variants        : {[a for _, a in roster]}")
    if strategy_params_override:
        _log(f"params override : {strategy_params_override}")
    if excluded:
        _log(f"excluded signals: {sorted(excluded)}")
    _log(f"resume          : {bool(args.resume)}")
    _log("=" * 70)

    # ── Market data: load cache or fetch ──
    history: Optional[Dict[str, pd.DataFrame]] = None
    if args.resume:
        history = _load_market_data_cache(out_root)
        if history is not None:
            _log(f"resume: loaded market_data.pkl ({len(history)} symbols, "
                 f"cache from earlier invocation)")
    if history is None:
        history = _build_history(symbols, start, end)
        _save_market_data_cache(history, out_root)
        _log(f"saved market_data.pkl ({len(history)} symbols, "
             f"{sum(len(df) for df in history.values()):,} bars total)")

    # ── Engine config ──
    engine_params = EngineParams(
        max_concurrent_positions=args.max_concurrent,
        sector_cap=args.sector_cap,
    )

    # ── Per-variant loop ──
    results_by_variant: Dict[str, Dict[str, Any]] = {}
    benchmark_cagr = 0.0
    benchmark_dd = 0.0
    failures: List[str] = []
    skipped: List[str] = []

    for module_path, alias in roster:
        try:
            spec = _load_spec(module_path)
        except Exception as exc:  # noqa: BLE001
            _log(f"FAIL load {alias} ({module_path}): {type(exc).__name__}: {exc}")
            failures.append(alias)
            (out_root / f"{alias}.failure.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
                encoding="utf-8",
            )
            continue

        vdir = _variant_dir(out_root, alias, spec.name)

        # Resume skip
        if args.resume and _variant_completed(vdir):
            _log(f"SKIP {alias} (already completed in {vdir.name})")
            skipped.append(alias)
            # Re-load result for top-level comparison
            try:
                meta = json.loads((vdir / "results.json").read_text(encoding="utf-8"))
                # Minimum shape for _render_top_comparison
                results_by_variant[alias] = {
                    "metrics": meta.get("metrics", {}),
                    "benchmark": meta.get("benchmark", {}),
                    "spec_name": spec.name,
                }
                b = meta.get("benchmark", {})
                if b and "cagr_pct" in b:
                    benchmark_cagr = b["cagr_pct"]
                    benchmark_dd = b.get("max_dd_pct", 0)
            except Exception:  # noqa: BLE001
                pass
            continue

        _log("")
        _log("─" * 70)
        _log(f"running {alias} ({module_path})")
        _log("─" * 70)
        try:
            result = run_swing_backtest(
                spec,
                history=history,
                capital_inr=args.capital,
                start=start, end=end,
                engine_params=engine_params,
                strategy_params_override=strategy_params_override,
                output_dir=vdir,
                excluded_from_signals=excluded,
                verbose=True,
            )
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            _log(f"FAIL {alias} during run: {type(exc).__name__}: {exc}")
            (out_root / f"{alias}.failure.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{tb}",
                encoding="utf-8",
            )
            failures.append(alias)
            continue

        result["spec_name"] = spec.name
        results_by_variant[alias] = result
        b = result.get("benchmark", {})
        if b and "cagr_pct" in b:
            benchmark_cagr = b["cagr_pct"]
            benchmark_dd = b.get("max_dd_pct", 0)
        m = result["metrics"]
        _log(f"DONE {alias} — CAGR {m.get('cagr_pct'):+.2f}% | "
             f"PF {m.get('profit_factor')} | MaxDD {m.get('max_dd_pct'):+.2f}% | "
             f"trades {m.get('n_trades')}")

    # ── Top-level comparison + manifest ──
    if results_by_variant:
        comp_md = _render_top_comparison(
            out_root=out_root,
            results_by_variant=results_by_variant,
            benchmark_cagr=benchmark_cagr,
            benchmark_dd=benchmark_dd,
            window_start=start, window_end=end,
            capital_inr=args.capital,
            universe_size=len(symbols),
            strategy_params_override=strategy_params_override,
        )
        (out_root / "comparison_top.md").write_text(comp_md, encoding="utf-8")

    manifest_top = {
        "engine": "swing_backtester (Engine B, cloud)",
        "runner": "tools/run_swing_battery.py",
        "run_id": run_id,
        "window_start": start,
        "window_end": end,
        "capital_inr": args.capital,
        "max_concurrent_positions": args.max_concurrent,
        "sector_cap": args.sector_cap,
        "universe_size": len(symbols),
        "universe_source": str(universe_path.relative_to(ROOT))
                            if universe_path is not None else "default_v4_swing_cash",
        "excluded_from_signals": sorted(excluded),
        "strategy_params_override": strategy_params_override,
        "variants_attempted": [a for _, a in roster],
        "variants_completed": [a for a in results_by_variant if a not in skipped],
        "variants_skipped_resume": skipped,
        "variants_failed": failures,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_root / "manifest_top.json").write_text(
        json.dumps(manifest_top, indent=2, default=str), encoding="utf-8"
    )

    _log("")
    _log("=" * 70)
    _log(f"complete. out: {out_root.relative_to(ROOT)}")
    _log(f"  ok={len(results_by_variant) - len(skipped)} "
         f"skipped={len(skipped)} failed={len(failures)}")
    _log(f"  comparison_top.md: {(out_root / 'comparison_top.md').relative_to(ROOT)}")
    _log("=" * 70)

    if failures:
        _log(f"PARTIAL: {len(failures)} variant(s) failed: {failures}. "
             f"Resume with: --resume {run_id}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
