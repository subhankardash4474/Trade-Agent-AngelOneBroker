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
import pickle
import sys
import time
import traceback
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--variants", nargs="+", default=None,
                    help="Subset of variant names to run (default: all)")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--resume", default=None,
                    help="Resume an existing run by run_id (YYYYMMDDTHHMMSS), "
                         "or pass 'auto' to pick the most recent incomplete run.")
    args = ap.parse_args()

    # ── Resolve run_id (fresh vs resume) ──
    resuming = False
    if args.resume:
        if args.resume == "auto":
            found = _find_latest_incomplete_run()
            if found:
                run_id = found
                resuming = True
                print(f"[BATTERY] auto-resume: continuing run {run_id}")
            else:
                run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
                print(f"[BATTERY] auto-resume: no incomplete run found, "
                      f"starting fresh as {run_id}")
        else:
            run_id = args.resume
            resuming = True

    else:
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S")

    out_root = ROOT / "logs" / "backtests" / run_id
    if resuming and not out_root.exists():
        print(f"[ERROR] cannot resume — directory not found: {out_root}")
        return 2
    (out_root / "configs").mkdir(parents=True, exist_ok=True)
    (out_root / "results").mkdir(parents=True, exist_ok=True)

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

    for name, overrides in pending:
        logger.info(f"\n{'=' * 70}\n[BATTERY] running variant: {name}\n{'=' * 70}")
        try:
            cfg = _build_variant_config(base_cfg, overrides)

            # Freeze config for reproducibility
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
            # Continue to the next variant; don't kill the whole battery.

        # Rewrite comparison.md after every variant (success or failure) so
        # the file is always usable mid-run.
        _write_comparison(
            sorted(rows, key=lambda r: r["variant"]),
            out_root / "comparison.md",
            _meta(datetime.now().isoformat(timespec="seconds")),
            complete=False, failed=failed,
        )

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
