"""Overnight backtest battery.

Runs a curated set of config variants against the SAME pre-downloaded
historical data. See VARIANTS list below for the open questions tested.

Each variant runs against the same market_data, so:
  - Comparison is apples-to-apples (no yfinance jitter).
  - We hit the network once, not 15x (avoids rate limits).

Outputs:
  logs/backtests/<run_id>/
      configs/*.yaml                — frozen configs per variant
      results/<variant>.json        — full backtest payload per variant
      results/<variant>.failure.txt — traceback if variant crashed
      market_data.pkl               — cached pre-downloaded bars (for --resume)
      comparison.md                 — markdown comparison (rewritten after each
                                      successful variant; safe to read mid-run)
      log.txt                       — runner stdout

Usage:
  # Fresh run:
  python tools/overnight_backtest_battery.py
  python tools/overnight_backtest_battery.py --days 14 --symbols RELIANCE TCS

  # Resume a run that crashed/was interrupted (skips completed variants,
  # reuses cached market_data — no yfinance refetch):
  python tools/overnight_backtest_battery.py --resume 20260508T173000

  # Auto-resume the most recent incomplete run, or start fresh if none:
  python tools/overnight_backtest_battery.py --resume auto
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pickle
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
from loguru import logger

# Phase 1 layout: this file lives at packages/research/battery.py
#   parents[1] = packages/      (sys.path bootstrap)
#   parents[2] = project root   (where logs/backtests/, config.yaml live)
PKG_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PKG_ROOT))
# `ROOT` previously meant "project root" pre-Phase-1. The downstream code
# below uses ROOT for `logs/backtests/` and `config.yaml`, both of which
# live at the project root, so keep the alias pointing there.
ROOT = PROJECT_ROOT

from research.backtest_ensemble import BacktestConfig, EnsembleBacktester, export_result  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from core.features import FeatureEngine  # noqa: E402


DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "TATAMOTORS", "HINDALCO", "JSWSTEEL", "BHARTIARTL", "SBIN",
]


def _deep_set(cfg: dict, dotted: str, value):
    """Set nested key by dotted path: 'strategies.mean_reversion.tp_reversion_pct'."""
    parts = dotted.split(".")
    d = cfg
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


# ── Variant definitions ──
# Each variant is (name, [(dotted_path, value), ...])
# An empty override list means "use base config as-is".

# 2026-05-08: refreshed slate after deploying trend filter to all 6 strategies.
# Goal: validate the May-8 expansion (4 new filters + the 2 existing ones),
# sweep the threshold, and audit the structural knobs that compounded today's
# losses.
#
# Naming: V1..V15 to differentiate from yesterday's C1..C9 results (the
# learnings from C* are already baked into the current config, which is V1).

# Threshold-sweep helper: set the same trend_filter_pct on all 6 strategies.
def _trend_all(pct):
    keys = [
        "mean_reversion", "xgboost_classifier", "supertrend_follow",
        "rsi_momentum", "vwap_bounce", "opening_range_breakout",
    ]
    return [(f"strategies.{k}.trend_filter_pct", pct) for k in keys]


VARIANTS = [
    # ── Tier 1: validate the May-8 trend-filter expansion ──
    # V1 is the *current shipped config*; V2 turns ALL filters off (the
    # apples-to-apples "no protection" baseline); V3 reproduces yesterday's
    # config (only XGB+MR filtered) so we can isolate what the 4 new filters
    # added.
    ("V1_baseline_current_shipped", []),
    ("V2_all_filters_off", _trend_all(None)),
    ("V3_only_xgb_mr_filtered_yday", [
        ("strategies.supertrend_follow.trend_filter_pct", None),
        ("strategies.rsi_momentum.trend_filter_pct", None),
        ("strategies.vwap_bounce.trend_filter_pct", None),
        ("strategies.opening_range_breakout.trend_filter_pct", None),
    ]),

    # ── Tier 2: threshold sweep (uniform across all 6 strategies) ──
    ("V4_threshold_3pct",  _trend_all(3.0)),
    ("V5_threshold_7pct",  _trend_all(7.0)),
    ("V6_threshold_10pct", _trend_all(10.0)),

    # ── Tier 3: per-strategy isolation — which strategies actually NEED it? ──
    ("V7_filter_supertrend_only", [
        ("strategies.mean_reversion.trend_filter_pct", None),
        ("strategies.xgboost_classifier.trend_filter_pct", None),
        ("strategies.rsi_momentum.trend_filter_pct", None),
        ("strategies.vwap_bounce.trend_filter_pct", None),
        ("strategies.opening_range_breakout.trend_filter_pct", None),
    ]),
    ("V8_filter_rsi_only", [
        ("strategies.mean_reversion.trend_filter_pct", None),
        ("strategies.xgboost_classifier.trend_filter_pct", None),
        ("strategies.supertrend_follow.trend_filter_pct", None),
        ("strategies.vwap_bounce.trend_filter_pct", None),
        ("strategies.opening_range_breakout.trend_filter_pct", None),
    ]),
    ("V9_filter_vwap_orb_off", [
        ("strategies.vwap_bounce.trend_filter_pct", None),
        ("strategies.opening_range_breakout.trend_filter_pct", None),
    ]),

    # ── Tier 4: structural knobs (audit other defensive layers) ──
    ("V10_confidence_060", [
        ("ensemble.confidence_threshold", 0.60),
    ]),
    ("V11_confidence_050", [
        ("ensemble.confidence_threshold", 0.50),
    ]),
    ("V12_peak_giveback_off", [
        ("risk.peak_giveback_enabled", False),
    ]),
    ("V13_window_cap_8", [
        ("risk.max_opens_per_window", 8),
    ]),
    ("V14_opening_lockout_off", [
        ("risk.opening_lockout_minutes", 0),
    ]),

    # ── Tier 5: nuclear option — strategy whitelist ──
    # Today we lost on supertrend (CAMS) + rsi (ATHERENERG). What if we ran
    # ONLY mean_reversion + xgboost (yesterday's winners)?
    ("V15_mr_xgb_only", [
        ("strategies.active", ["mean_reversion", "xgboost_classifier"]),
    ]),
]


def _load_base_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build_variant_config(base: dict, overrides: list) -> dict:
    cfg = copy.deepcopy(base)
    for dotted, value in overrides:
        _deep_set(cfg, dotted, value)
    return cfg


def _bt_config(cfg: dict) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=cfg.get("backtest", {}).get("initial_capital", 25000.0),
        commission_pct=cfg.get("backtest", {}).get("commission_pct", 0.03),
        slippage_pct=cfg.get("backtest", {}).get("slippage_pct", 0.05),
        confidence_threshold=cfg.get("ensemble", {}).get("confidence_threshold", 0.55),
        min_entry_atr_pct=cfg.get("robustness", {}).get("min_entry_atr_pct", 0.8),
        min_profit_to_charges_ratio=cfg.get("risk", {}).get("min_profit_to_charges_ratio", 2.5),
        min_absolute_reward_rs=cfg.get("risk", {}).get("min_absolute_reward_rs", 20.0),
        max_positions=cfg.get("risk", {}).get("max_positions", 3),
        max_losses_per_stock=cfg.get("robustness", {}).get("max_losses_per_stock_per_day", 2),
        product_type=cfg.get("execution", {}).get("product_type", "INTRADAY"),
    )


def _summary_row(name: str, result) -> dict:
    return {
        "variant": name,
        "trades": result.total_trades,
        "wins": result.wins,
        "losses": result.losses,
        "win_rate": round(result.win_rate, 1),
        "pnl": round(result.total_pnl, 2),
        "profit_factor": round(result.profit_factor, 2),
        "rr": round(result.rr_ratio, 2),
        "expectancy": round(result.expectancy, 2),
        "sharpe": round(result.sharpe, 2),
        "max_dd_pct": round(result.max_drawdown_pct, 2),
        "return_pct": round(result.return_pct, 2),
        "charges": round(result.total_charges, 2),
    }


def _save_market_data_cache(out_root: Path, market_data: dict) -> None:
    """Pickle the pre-downloaded + feature-enriched market_data for resume."""
    cache_path = out_root / "market_data.pkl"
    try:
        with cache_path.open("wb") as f:
            pickle.dump(market_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        logger.info(f"[BATTERY] market_data cached ({size_mb:.1f} MB) -> {cache_path}")
    except Exception as e:
        logger.warning(f"[BATTERY] failed to cache market_data: {e}")


def _load_market_data_cache(out_root: Path) -> dict | None:
    """Return cached market_data dict if present and valid, else None."""
    cache_path = out_root / "market_data.pkl"
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("rb") as f:
            md = pickle.load(f)
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"[BATTERY] reusing cached market_data ({size_mb:.1f} MB, "
            f"{len(md)} symbols) — skipping yfinance fetch"
        )
        return md
    except Exception as e:
        logger.warning(f"[BATTERY] cache load failed ({e}); will refetch")
        return None


def _run_variant_in_subprocess(
    name: str,
    overrides: list,
    base_cfg: dict,
    symbols: list,
    interval: str,
    days: int,
    out_root_str: str,
) -> tuple[str, dict]:
    """Worker entry point for parallel battery execution.

    Runs ONE variant inside a fresh ProcessPoolExecutor subprocess and
    returns its result payload. Must be a top-level (importable) function
    because Windows `spawn` pickles workers by qualified name.

    Why workers reload market_data from disk instead of receiving it via
    IPC: the pickled dict is ~300 MB at 200 stocks × 90 days. Sending it
    through ProcessPoolExecutor's argument-pickle would pay that cost
    once per task (18 tasks × 300 MB = 5.4 GB of IPC) on top of the
    once-per-worker memory cost. Reading from disk is faster and the
    market_data.pkl already exists for the resume mechanism, so no new
    artifact is needed.

    Per-task disk writes (configs/<name>.yaml, results/<name>.json) are
    handled here in the worker so each task is fully self-contained — the
    parent only needs to update comparison.md from the returned payload.

    Per-worker log sink (workers/<name>.log) is installed so progress is
    visible mid-run. Without this, ProcessPoolExecutor workers run in a
    multi-hour log blackout: their inherited stderr is unreliable on
    Windows (disconnects when the launching shell terminates) and the
    parent's logger.add(log.txt) sink only exists in the parent process.
    Caused user-facing "looks like the battery has failed" alarms during
    the v2 run on 2026-05-10.
    """
    out_root = Path(out_root_str)

    # Install a per-variant log sink BEFORE doing any heavy work so that
    # market_data.pkl unpickling, feature reload, model loads, and the
    # backtest's per-symbol strategy emissions (e.g. "[vwap_bounce] SELL
    # RELIANCE @ ...") all become visible while the variant is running.
    # enqueue=True because numpy/pandas may emit from threads under the
    # hood; the queue prevents log-line interleaving from racing.
    workers_dir = out_root / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    worker_log = workers_dir / f"{name}.log"
    logger.add(
        str(worker_log),
        level="INFO",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:7} | {name}:{function}:{line} - {message}",
    )
    logger.info(f"[WORKER] starting variant {name}")

    market_data = _load_market_data_cache(out_root)
    if market_data is None:
        raise RuntimeError(f"market_data.pkl missing in {out_root}")
    logger.info(f"[WORKER] {name}: market_data loaded ({len(market_data)} symbols)")

    cfg = _build_variant_config(base_cfg, overrides)
    (out_root / "configs" / f"{name}.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
    )

    bt_cfg = _bt_config(cfg)
    bt = EnsembleBacktester(cfg, bt_cfg)
    strategies = cfg.get("strategies", {}).get("active")
    logger.info(f"[WORKER] {name}: backtester initialized, starting bt.run()")

    t0 = time.time()
    result = bt.run(
        symbols=symbols, interval=interval, days=days,
        strategies=strategies, market_data=market_data,
    )
    elapsed = time.time() - t0
    logger.info(
        f"[WORKER] {name}: bt.run() complete in {elapsed:.1f}s | "
        f"trades={result.total_trades} pnl=Rs {result.total_pnl:+.2f} "
        f"WR={result.win_rate:.1f}% PF={result.profit_factor:.2f}"
    )

    payload = {
        "variant": name,
        "overrides": overrides,
        "elapsed_sec": round(elapsed, 1),
        "summary": _summary_row(name, result),
        "gate_stats": result.gate_stats.as_dict(),
        "strategy_pnl": result.strategy_pnl,
        "regime_pnl": result.regime_pnl,
        "trades": result.trades,
    }
    # Persist per-variant result here so a worker crash mid-run still
    # leaves a complete record (parent's comparison.md write is the only
    # thing that becomes inconsistent, and that's a single-writer file).
    (out_root / "results" / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return name, payload


def _find_latest_incomplete_run() -> str | None:
    """Return the run_id of the most recent run that hasn't completed.

    A run is "complete" when comparison.md contains the COMPLETE marker.
    """
    bt_dir = ROOT / "logs" / "backtests"
    if not bt_dir.exists():
        return None
    candidates = sorted(
        [p for p in bt_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    for cand in candidates:
        comp = cand / "comparison.md"
        if comp.exists() and "[COMPLETE]" in comp.read_text(encoding="utf-8"):
            continue
        # Has at least one variant result -> resumable
        results_dir = cand / "results"
        if results_dir.exists() and any(results_dir.glob("*.json")):
            return cand.name
    return None


def _completed_variant_names(out_root: Path) -> set[str]:
    """Return the set of variant names that have a result JSON on disk."""
    results_dir = out_root / "results"
    if not results_dir.exists():
        return set()
    return {p.stem for p in results_dir.glob("*.json")}


def _write_comparison(rows: list, out_path: Path, meta: dict,
                      *, complete: bool = False, failed: list | None = None):
    """Write/overwrite comparison.md.

    Called after every variant (rows grows incrementally), so the file is
    safe to read mid-run. The 'complete' flag adds a marker the resume
    detector keys off of.
    """
    failed = failed or []
    status = "[COMPLETE]" if complete else "[IN-PROGRESS]"
    lines = []
    lines.append(f"# Overnight Backtest Battery -- Comparison {status}\n")
    lines.append(f"- Run ID: `{meta['run_id']}`")
    lines.append(f"- Started: {meta['started']}")
    lines.append(f"- Last update: {meta['finished']}")
    lines.append(f"- Symbols: {', '.join(meta['symbols'])}")
    lines.append(f"- Days: {meta['days']}  |  Interval: {meta['interval']}")
    lines.append(f"- Initial capital: Rs {meta['capital']:,.0f}")
    lines.append(f"- Variants done: {len(rows)} / {meta.get('total_variants', '?')}"
                 f"   |  failed: {len(failed)}\n")
    lines.append("## Results\n")
    headers = ["Variant", "Trades", "WR%", "PnL", "PF", "R:R", "Exp", "Sharpe", "MaxDD%", "Ret%"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---:" if h != "Variant" else "---" for h in headers]) + "|")
    for r in rows:
        lines.append("| {variant} | {trades} | {win_rate} | Rs {pnl:+.0f} | {profit_factor} | 1:{rr} | "
                     "Rs {expectancy:+.1f} | {sharpe} | {max_dd_pct} | {return_pct:+.2f}% |".format(**r))

    if failed:
        lines.append("\n## Failed variants\n")
        for name, err in failed:
            lines.append(f"- `{name}` — {err}")

    lines.append("\n## Notes\n")
    lines.append("- Same market_data used across all variants -- comparable.")
    lines.append("- PnL is gross of taxes/STT; Sharpe is annualized from per-bar returns.")
    lines.append("- Expectancy = total_pnl / trades (Rs/trade).")
    if not complete:
        lines.append(
            "- This run is **still in progress**. To resume after a crash:\n"
            f"    `python tools/overnight_backtest_battery.py --resume {meta['run_id']}`"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")


_BROKER_CRED_ENV_PREFIXES = ("ANGELONE_", "SMARTAPI_", "BROKER_", "KITE_")


def _assert_backtester_isolation() -> None:
    """Refuse to start the battery on a backtester-role host if any broker
    credentials are present in the environment.

    Activated by setting `BACKTESTER_MODE=1` (typically wired by the
    backtester VM's systemd unit or `launch_battery.sh`). On the live
    trader VM this var is absent, so this is a no-op there.

    Rationale: the backtester VM has no broker IP whitelist by design and
    must never touch a live broker socket. If we accidentally rsync a
    populated .env file (or a developer pastes one), we want a loud
    crash *before* the harness opens any data sources, not a silent path
    where the wrong creds reach the wrong host.
    """
    if os.environ.get("BACKTESTER_MODE", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return
    leaked = [
        k for k in os.environ
        if any(k.startswith(p) for p in _BROKER_CRED_ENV_PREFIXES)
    ]
    if leaked:
        # Print to stderr (and not a logger) so the message is visible even
        # if logging hasn't been initialised yet.
        print(
            "[BATTERY][FATAL] BACKTESTER_MODE=1 but the following broker "
            "credential env vars are present: "
            + ", ".join(sorted(leaked))
            + ". A backtester host MUST NOT carry broker creds (no IP "
            "whitelist, no live order surface). Aborting before any data "
            "source is opened.",
            file=sys.stderr,
        )
        raise SystemExit(9)


def main() -> int:
    _assert_backtester_isolation()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--universe-file", default=None,
                    help="Path to a JSON file with shape {\"universe\": [\"RELIANCE\", ...]} "
                         "(see tools/_freeze_battery_v2_universe.py). When provided, this "
                         "overrides --symbols. Use for battery-v2 runs against a stable "
                         "200-stock list — passing 200 symbols on the command line hits the "
                         "shell argument-buffer limit on Windows.")
    ap.add_argument("--variants", nargs="+", default=None,
                    help="Subset of variant names to run (default: all)")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--resume", default=None,
                    help="Resume an existing run by run_id (YYYYMMDDTHHMMSS), "
                         "or pass 'auto' to pick the most recent incomplete run.")
    ap.add_argument("--run-id", default=None,
                    help="Pin a deterministic run_id instead of the "
                         "auto-generated YYYYMMDDTHHMMSS timestamp. Useful for "
                         "reproducible smoke tests, CI runs, and cross-machine "
                         "comparison (so two machines running the same flags "
                         "land in the same logs/backtests/<run_id>/ folder). "
                         "Mutually exclusive with --resume.")
    ap.add_argument("--train-window-days", type=int, default=None,
                    help="Walk-forward TRAIN slice: keep only the FIRST N days "
                         "of market_data for variant runs. Mutually exclusive "
                         "with --holdout-window-days. Use case: select best "
                         "variant on the train slice, then re-run with "
                         "--holdout-window-days to validate on UNSEEN bars. "
                         "Without train/holdout flags, the whole window is "
                         "used (which trains and tests on the same data -- "
                         "fine for relative comparisons, NOT for honest 'is "
                         "this overfit?' validation).")
    ap.add_argument("--holdout-window-days", type=int, default=None,
                    help="Walk-forward HOLDOUT slice: keep only the LAST N "
                         "days of market_data. Mutually exclusive with "
                         "--train-window-days. If a variant wins on the train "
                         "slice AND survives the holdout slice, it has real "
                         "edge; if it crumbles on holdout, the train win was "
                         "p-hacked.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of parallel worker processes for variant "
                         "execution (default: 1 = serial, preserves legacy "
                         "behavior for CI/tests/debugging). Variants are "
                         "embarrassingly parallel; battery-v2 (18 variants) "
                         "wall-time at --workers 4 is ~3x faster than serial. "
                         "Cap to (cpu_count - 1) and budget ~1.5 GB RAM/worker "
                         "for a 200-stock universe.")
    args = ap.parse_args()

    # Sanity-clamp workers: 0 or negative is nonsense; >cpu_count just wastes
    # memory on context-switching. Still allow oversubscription if the user
    # explicitly requests it (some I/O wait can hide behind extra processes),
    # but warn so the caller knows it's intentional.
    cpu = os.cpu_count() or 1
    if args.workers < 1:
        args.workers = 1
    elif args.workers > cpu:
        print(f"[BATTERY] WARNING: --workers={args.workers} exceeds cpu_count={cpu}; "
              f"oversubscription is rarely a win for CPU-bound backtests.")

    # ── Mutex checks for the new flags ──
    if args.resume and args.run_id:
        print("[ERROR] --resume and --run-id are mutually exclusive "
              "(--resume already pins the run_id to the existing folder).")
        return 6
    if args.train_window_days and args.holdout_window_days:
        print("[ERROR] --train-window-days and --holdout-window-days are "
              "mutually exclusive. Run battery twice (once per slice) to get "
              "both train and holdout numbers.")
        return 7

    # ── Resolve run_id (fresh vs resume vs pinned) ──
    resuming = False
    if args.resume:
        if args.resume == "auto":
            found = _find_latest_incomplete_run()
            if found:
                run_id = found
                resuming = True
                print(f"[BATTERY] auto-resume: continuing run {run_id}")
            else:
                run_id = args.run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
                print(f"[BATTERY] auto-resume: no incomplete run found, "
                      f"starting fresh as {run_id}")
        else:
            run_id = args.resume
            resuming = True

    elif args.run_id:
        # Pinned run_id: deterministic for reproducibility, but still create
        # a fresh folder (won't accidentally overwrite an existing run unless
        # the user explicitly reuses an ID, which is then their choice).
        run_id = args.run_id
        print(f"[BATTERY] using pinned run_id={run_id}")

    else:
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S")

    out_root = ROOT / "logs" / "backtests" / run_id
    if resuming and not out_root.exists():
        print(f"[ERROR] cannot resume — directory not found: {out_root}")
        return 2
    (out_root / "configs").mkdir(parents=True, exist_ok=True)
    (out_root / "results").mkdir(parents=True, exist_ok=True)

    # Universe-file override: load a frozen universe JSON if specified.
    # This must happen AFTER args parsing but BEFORE any code that reads
    # `args.symbols` (currently the data-fetch and metadata sections below).
    if args.universe_file:
        uf_path = Path(args.universe_file)
        if not uf_path.is_absolute():
            uf_path = ROOT / uf_path
        try:
            payload = json.loads(uf_path.read_text(encoding="utf-8"))
            args.symbols = list(payload["universe"])
            print(f"[BATTERY] Loaded {len(args.symbols)} symbols from "
                  f"{uf_path.relative_to(ROOT) if uf_path.is_relative_to(ROOT) else uf_path}")
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
            print(f"[ERROR] failed to load --universe-file {uf_path}: {e}")
            return 3

    # Mirror loguru into a per-run log file (append on resume)
    logger.add(out_root / "log.txt", level="INFO")

    base_cfg = _load_base_config(ROOT / args.config)
    if args.capital is not None:
        _deep_set(base_cfg, "backtest.initial_capital", args.capital)

    selected = [v for v in VARIANTS if (args.variants is None or v[0] in args.variants)]
    completed = _completed_variant_names(out_root) if resuming else set()
    pending = [v for v in selected if v[0] not in completed]

    logger.info(
        f"[BATTERY] run_id={run_id} resume={resuming} | "
        f"variants total={len(selected)} completed={len(completed)} "
        f"pending={len(pending)}"
    )
    if completed:
        logger.info(f"[BATTERY] skipping already-done variants: {sorted(completed)}")
    if not pending:
        logger.info("[BATTERY] all selected variants already complete — "
                    "regenerating comparison.md only")

    # ── Step 1: market_data — reuse cache on resume, else download ──
    market_data = _load_market_data_cache(out_root) if resuming else None
    if market_data is None:
        logger.info(f"[BATTERY] downloading {args.interval} bars: "
                    f"{args.symbols} for {args.days}d")
        dh = DataHandler(base_cfg)
        fe = FeatureEngine()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=args.days)
        raw = dh.download_historical_for_backtest(
            symbols=[s[:-3] if s.upper().endswith(".NS") else s for s in args.symbols],
            interval=args.interval if args.interval not in ("5m", "15m", "30m", "1m")
                     else args.interval.replace("m", "min"),
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
        )
        market_data = {s: df for s, df in raw.items() if not df.empty}
        if not market_data:
            logger.error("[BATTERY] no market data downloaded — aborting")
            return 1

        logger.info(f"[BATTERY] enriching features for {len(market_data)} symbols")
        for s in list(market_data.keys()):
            market_data[s] = fe.compute_all(market_data[s])

        _save_market_data_cache(out_root, market_data)

    total_bars = sum(len(df) for df in market_data.values())
    logger.info(f"[BATTERY] data ready: {len(market_data)} symbols, {total_bars} bars total")

    # ── Walk-forward slice (optional) ──
    # Keep this AFTER market_data is fully loaded/cached so that the cache
    # always contains the FULL window. Subsequent --resume invocations can
    # then re-slice differently without re-downloading from yfinance.
    # Slicing by calendar days (not bar count) so weekends/holidays are
    # handled correctly: 30 calendar days = ~22 trading days = ~1500 bars
    # at 5m intervals in a normal NSE month.
    if args.train_window_days or args.holdout_window_days:
        n = args.train_window_days or args.holdout_window_days
        keep = "first" if args.train_window_days else "last"
        sliced_count = 0
        for sym in list(market_data.keys()):
            df = market_data[sym]
            if df.empty:
                continue
            try:
                if keep == "first":
                    cutoff = df.index.min() + pd.Timedelta(days=n)
                    market_data[sym] = df[df.index < cutoff]
                else:
                    cutoff = df.index.max() - pd.Timedelta(days=n)
                    market_data[sym] = df[df.index >= cutoff]
                sliced_count += 1
            except (TypeError, AttributeError) as e:
                # df.index isn't datetime-like -- can't time-slice. Skip but
                # warn so the user knows this symbol's data is suspect.
                logger.warning(f"[BATTERY] {sym}: cannot apply walk-forward "
                               f"slice (non-datetime index): {e}")
        sliced_total = sum(len(df) for df in market_data.values())
        ratio = sliced_total / total_bars if total_bars else 0
        logger.info(
            f"[BATTERY] walk-forward slice ({keep} {n}d, applied to "
            f"{sliced_count}/{len(market_data)} symbols): "
            f"{sliced_total} bars (was {total_bars}, ratio {ratio:.1%})"
        )
        # Reload total_bars for downstream metadata so the slice is reflected.
        total_bars = sliced_total

    # ── Step 2: run each variant ──
    # Hydrate `rows` from already-completed variants so comparison.md is
    # accurate on resume (and after every successful new variant).
    rows: list = []
    failed: list = []
    started = datetime.now().isoformat(timespec="seconds")

    for name in completed:
        try:
            payload = json.loads((out_root / "results" / f"{name}.json").read_text(encoding="utf-8"))
            rows.append(payload["summary"])
        except Exception as e:
            logger.warning(f"[BATTERY] could not rehydrate {name}: {e}")

    def _meta(now_iso: str) -> dict:
        return {
            "run_id": run_id,
            "started": started,
            "finished": now_iso,
            "symbols": args.symbols,
            "days": args.days,
            "interval": args.interval,
            "capital": _bt_config(base_cfg).initial_capital,
            "total_variants": len(selected),
        }

    # Initial comparison.md (in-progress) — even if no new variant runs.
    _write_comparison(
        sorted(rows, key=lambda r: r["variant"]),
        out_root / "comparison.md",
        _meta(datetime.now().isoformat(timespec="seconds")),
        complete=False, failed=failed,
    )

    if args.workers == 1:
        # Serial path — unchanged (preserves CI/test behavior, easy debugging,
        # and KeyboardInterrupt friendliness during interactive smoke runs).
        for name, overrides in pending:
            logger.info(f"\n{'=' * 70}\n[BATTERY] running variant: {name}\n{'=' * 70}")
            try:
                cfg = _build_variant_config(base_cfg, overrides)

                (out_root / "configs" / f"{name}.yaml").write_text(
                    yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
                )

                bt_cfg = _bt_config(cfg)
                bt = EnsembleBacktester(cfg, bt_cfg)
                strategies = cfg.get("strategies", {}).get("active")
                t0 = time.time()
                result = bt.run(
                    symbols=args.symbols,
                    interval=args.interval,
                    days=args.days,
                    strategies=strategies,
                    market_data=market_data,
                )
                elapsed = time.time() - t0
                logger.info(
                    f"[BATTERY] {name} done in {elapsed:.1f}s | "
                    f"trades={result.total_trades}  pnl=Rs {result.total_pnl:+.2f}  "
                    f"WR={result.win_rate:.1f}%  PF={result.profit_factor:.2f}"
                )

                payload = {
                    "variant": name,
                    "overrides": overrides,
                    "elapsed_sec": round(elapsed, 1),
                    "summary": _summary_row(name, result),
                    "gate_stats": result.gate_stats.as_dict(),
                    "strategy_pnl": result.strategy_pnl,
                    "regime_pnl": result.regime_pnl,
                    "trades": result.trades,
                }
                (out_root / "results" / f"{name}.json").write_text(
                    json.dumps(payload, indent=2, default=str), encoding="utf-8"
                )
                rows.append(_summary_row(name, result))

            except KeyboardInterrupt:
                logger.warning(f"[BATTERY] interrupted during {name} — partial results saved. "
                               f"Resume with: --resume {run_id}")
                _write_comparison(
                    sorted(rows, key=lambda r: r["variant"]),
                    out_root / "comparison.md",
                    _meta(datetime.now().isoformat(timespec="seconds")),
                    complete=False, failed=failed,
                )
                return 130
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"[BATTERY] {name} CRASHED: {e}\n{tb}")
                (out_root / "results" / f"{name}.failure.txt").write_text(
                    f"{datetime.now().isoformat()}\n{e}\n\n{tb}", encoding="utf-8"
                )
                failed.append((name, str(e).splitlines()[0] if str(e) else type(e).__name__))

            _write_comparison(
                sorted(rows, key=lambda r: r["variant"]),
                out_root / "comparison.md",
                _meta(datetime.now().isoformat(timespec="seconds")),
                complete=False, failed=failed,
            )
    else:
        # Parallel path — spawn ProcessPoolExecutor and dispatch each pending
        # variant as an independent task. Workers are subprocesses (not
        # threads): the EnsembleBacktester is CPU-bound under the GIL, so
        # only true parallelism gives speedup. Throughput target for v2:
        # ~3x at --workers 4 vs. serial (the residual is process-startup
        # cost + the shared market_data load each worker pays once).
        logger.info(f"[BATTERY] PARALLEL mode: workers={args.workers}, tasks={len(pending)}")
        if not (out_root / "market_data.pkl").exists():
            # Workers reload market_data from this file. If we got here
            # without saving it (e.g., resume path elided the save), we'd
            # be sending bad data. Fail loudly rather than silently giving
            # each worker a None.
            logger.error("[BATTERY] market_data.pkl missing — cannot run parallel workers. "
                         "(This shouldn't happen on a fresh run; if you're resuming an "
                         "older run, re-run without --resume to regenerate the cache.)")
            return 4

        try:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(
                        _run_variant_in_subprocess,
                        name, overrides, base_cfg,
                        args.symbols, args.interval, args.days,
                        str(out_root),
                    ): name
                    for name, overrides in pending
                }
                logger.info(f"[BATTERY] dispatched {len(futures)} variants to worker pool")

                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        _, payload = fut.result()
                        summary = payload["summary"]
                        rows.append(summary)
                        logger.info(
                            f"[BATTERY] {name} done in {payload['elapsed_sec']}s | "
                            f"trades={summary['trades']}  pnl=Rs {summary['pnl']:+.2f}  "
                            f"WR={summary['win_rate']:.1f}%  PF={summary['profit_factor']:.2f}"
                        )
                    except Exception as e:
                        # Capture per-variant failure — DO NOT kill the pool.
                        # Other workers continue; we just record this one as
                        # failed and still write the partial comparison.md.
                        tb = traceback.format_exc()
                        logger.error(f"[BATTERY] {name} CRASHED in worker: {e}\n{tb}")
                        (out_root / "results" / f"{name}.failure.txt").write_text(
                            f"{datetime.now().isoformat()}\n{e}\n\n{tb}", encoding="utf-8"
                        )
                        failed.append((name, str(e).splitlines()[0] if str(e) else type(e).__name__))

                    # Single-writer comparison.md update from the parent.
                    # Workers never touch this file, so no lock needed.
                    _write_comparison(
                        sorted(rows, key=lambda r: r["variant"]),
                        out_root / "comparison.md",
                        _meta(datetime.now().isoformat(timespec="seconds")),
                        complete=False, failed=failed,
                    )
        except KeyboardInterrupt:
            # Shutdown cleanly: with-block will cancel pending futures.
            logger.warning(f"[BATTERY] interrupted — partial results saved. "
                           f"Resume with: --resume {run_id}")
            _write_comparison(
                sorted(rows, key=lambda r: r["variant"]),
                out_root / "comparison.md",
                _meta(datetime.now().isoformat(timespec="seconds")),
                complete=False, failed=failed,
            )
            return 130

    # ── Step 3: final comparison report ──
    finished = datetime.now().isoformat(timespec="seconds")
    _write_comparison(
        sorted(rows, key=lambda r: r["variant"]),
        out_root / "comparison.md",
        _meta(finished),
        complete=(len(failed) == 0 and len(rows) == len(selected)),
        failed=failed,
    )

    logger.info(f"\n[BATTERY] Done. Output: {out_root}")
    if failed:
        print(f"\n[PARTIAL] {len(rows)}/{len(selected)} variants OK, "
              f"{len(failed)} failed. See {out_root}/comparison.md")
        print(f"          Resume failed ones with: --resume {run_id}")
    else:
        print(f"\n[OK] Battery complete: {out_root}/comparison.md")
    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())
