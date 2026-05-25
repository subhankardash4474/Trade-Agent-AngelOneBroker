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
import re
import sys
import threading
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


# ── Battery worker log filter (perf knob) ──────────────────────────────
# The strategy and ensemble modules emit one INFO line per generated
# signal (e.g. "[vwap_bounce] SELL RELIANCE @ 2842.50"). On a
# 220-symbol × 90-day × 5-min battery that's millions of bars per
# variant — the per-worker log file balloons to 6+ MB / variant and
# the disk I/O measurably starves the cores on a 2-vCPU VM.
#
# This filter drops INFO from those modules but keeps:
#   * harness messages (loguru name='__main__' / 'research.battery')
#   * WARNING+ from anything (rejection cascades, exceptions)
#   * the per-variant audit CSV (written outside loguru, unaffected)
#
# Set BATTERY_VERBOSE=1 to disable the filter (useful when debugging
# a single variant locally and you want to see every signal).
_BATTERY_QUIET_PREFIXES: tuple[str, ...] = (
    "strategies.",      # strategies.vwap_bounce, .rsi_momentum, .supertrend_follow,
                        # .ensemble, .opening_range_breakout, .mean_reversion,
                        # .xgboost_classifier — all the per-bar signal emitters.
    "core.portfolio",   # close_position / open_position spam (one INFO per fill).
)


def _battery_verbose_enabled() -> bool:
    """True when BATTERY_VERBOSE=1 / true / yes (any case)."""
    return os.environ.get("BATTERY_VERBOSE", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _resolve_workers(raw_workers, cpu_count: int) -> tuple[int, str | None]:
    """Resolve the --workers CLI value to a concrete integer.

    Returns (resolved_int, info_message). info_message is non-None when
    the caller should surface what just happened (e.g. "auto resolved to 7").
    Raises ValueError on bogus input -- caller decides how to surface the
    error (the CLI prints to stderr and returns exit code 8).
    """
    raw_str = str(raw_workers).strip().lower()
    if raw_str == "auto":
        resolved = max(1, cpu_count - 1)
        msg = (f"[BATTERY] --workers=auto resolved to {resolved} "
               f"(cpu_count={cpu_count})")
        return resolved, msg
    try:
        return int(raw_str), None
    except ValueError as exc:
        raise ValueError(
            f"--workers must be 'auto' or an integer; got {raw_workers!r}."
        ) from exc


def _battery_log_filter(record) -> bool:
    """Loguru filter: keep WARNING+ always, drop INFO from per-bar emitters.

    Returns True (= keep) when the record should be written to the sink,
    False (= drop) otherwise. Pure function over the record dict — safe
    to call from worker subprocesses.
    """
    if _battery_verbose_enabled():
        return True
    name = record.get("name") or ""
    if not any(name.startswith(p) for p in _BATTERY_QUIET_PREFIXES):
        return True  # not a noisy module — keep at all levels
    # Noisy module: only keep WARNING+ (loguru WARNING.no == 30).
    return record["level"].no >= 30


# ── Worker watchdog (deadlock detector) ────────────────────────────────
# Defends against the case where a worker subprocess goes silent — GIL
# deadlock, OOM thrashing, third-party library blocking on a kernel
# resource — while the parent's ProcessPoolExecutor sees it as "still
# running" and waits indefinitely. A 33h queue-blocking worker (real
# incident: battery_freeze_v21_20260518T181337) is exactly the failure
# mode this guards.
#
# The watchdog reads the worker's own log file's mtime: with the
# tightened progress emission (every PROGRESS_LOG_INTERVAL_SECONDS),
# mtime advances at least every ~60s during a healthy run. If mtime
# stalls beyond max_silence_sec, the worker is presumed hung and
# self-terminates with os._exit(124) so the parent sees a clean
# CalledProcessError-equivalent and the queue moves on.
#
# Configurable via BATTERY_WATCHDOG_SILENCE_MIN (default 30). Setting
# to 0 disables the watchdog entirely (useful for debugging).
_WATCHDOG_DEFAULT_SILENCE_MIN = 30
_WATCHDOG_POLL_SEC = 60


def _watchdog_silence_sec() -> int:
    """Resolve the configured silence threshold in seconds (0 = disabled)."""
    raw = os.environ.get("BATTERY_WATCHDOG_SILENCE_MIN", "").strip()
    if not raw:
        return _WATCHDOG_DEFAULT_SILENCE_MIN * 60
    try:
        minutes = int(raw)
    except ValueError:
        return _WATCHDOG_DEFAULT_SILENCE_MIN * 60
    return max(minutes, 0) * 60


def _spawn_progress_watchdog(
    worker_log: Path,
    variant_name: str,
    max_silence_sec: int,
) -> threading.Thread | None:
    """Start a daemon thread that suicides this worker if its log goes silent.

    Returns the thread (so callers can join in tests) or None when the
    watchdog is disabled (max_silence_sec <= 0). The thread is daemonic
    so it never prevents normal worker exit.
    """
    if max_silence_sec <= 0:
        return None

    def _watch() -> None:
        # Keep importing inside the thread so test patches of os._exit etc.
        # take effect even when this module is imported once at startup.
        import os as _os
        import sys as _sys
        import time as _time

        # Allow a generous startup grace: market_data unpickle (300 MB) +
        # FeatureEngine warmup can take 60-120s on the 2-vCPU VM before
        # the first progress line emits. Without this, the watchdog can
        # fire during legitimate startup.
        startup_grace = max(max_silence_sec // 2, 120)
        _time.sleep(min(startup_grace, max_silence_sec))

        while True:
            _time.sleep(_WATCHDOG_POLL_SEC)
            try:
                mtime = worker_log.stat().st_mtime
            except (FileNotFoundError, OSError):
                # Log not created yet, or filesystem hiccup — try again.
                continue
            age = _time.time() - mtime
            if age > max_silence_sec:
                msg = (
                    f"[WATCHDOG] {variant_name}: no log activity for "
                    f"{age:.0f}s (limit {max_silence_sec}s). Worker presumed "
                    f"hung; suiciding with exit 124 so the queue can advance."
                )
                # Write to stderr (not loguru) — if loguru is the thing
                # that's deadlocked, we'd never get the message out
                # through the same sinks.
                try:
                    _sys.stderr.write(msg + "\n")
                    _sys.stderr.flush()
                except Exception:
                    pass
                _os._exit(124)

    t = threading.Thread(
        target=_watch,
        name=f"battery-watchdog-{variant_name}",
        daemon=True,
    )
    t.start()
    return t


# Compiled regex used by the parent's live-status reader to find the
# most recent progress marker in a worker log file. Matches the line
# format emitted by research.backtest_ensemble.run().
_PROGRESS_LINE_RE = re.compile(
    r"\[BATTERY-PROGRESS\]\s+"
    r"(?P<done>[\d,]+)\s*/\s*(?P<total>[\d,]+)\s+"
    r"\(\s*(?P<pct>[\d.]+)%\s*\)\s*\|\s*"
    r"sim_date=(?P<sim_date>\S+)\s*\|\s*"
    r"rate=(?P<rate>[\d,]+)\s*ev/s\s*\|\s*"
    r"elapsed=(?P<elapsed>\S+)\s*\|\s*"
    r"ETA=(?P<eta>\S+)"
)


def _parse_last_progress(log_path: Path) -> dict | None:
    """Return the most recent [BATTERY-PROGRESS] payload or None.

    Uses a tail-friendly read (last 32KB) so this stays cheap even on
    multi-MB worker logs. The progress line format is fixed-width-ish
    (~150 chars), so 32KB easily covers the last ~200 emissions and
    guarantees we'll find one if any were emitted in the last
    PROGRESS_LOG_INTERVAL_SECONDS window.
    """
    if not log_path.exists():
        return None
    try:
        with log_path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                read_from = max(0, size - 32 * 1024)
                f.seek(read_from)
                tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                # Tiny file — just read it whole.
                f.seek(0)
                tail = f.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError):
        return None

    last_match = None
    for m in _PROGRESS_LINE_RE.finditer(tail):
        last_match = m
    if last_match is None:
        return None
    g = last_match.groupdict()
    try:
        done = int(g["done"].replace(",", ""))
        total = int(g["total"].replace(",", ""))
        pct = float(g["pct"])
        rate = int(g["rate"].replace(",", ""))
    except ValueError:
        return None
    return {
        "done": done,
        "total": total,
        "pct": pct,
        "sim_date": g["sim_date"],
        "rate_ev_s": rate,
        "elapsed": g["elapsed"],
        "eta": g["eta"],
    }


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

    # ── Tier 6: completely-naked diagnostic (2026-05-21) ──
    # What does the backtester look like when EVERY gate the harness models
    # is turned off? V2 only neutralises the per-strategy trend filter; V16
    # additionally drops every numerical floor to zero, lifts the
    # concurrent-position cap, lets a stock keep firing after losses, and
    # disables the dead-hour + expected-profit gates entirely. Diagnostic
    # value: upper-bound on opportunity that all our safety nets are
    # gating away.
    #   * V16 PnL >> V1  -> gates are over-aggressive, loosen them
    #   * V16 blows up   -> gates are doing real protective work
    #   * V16 ~= V2      -> dead_hour + profit_gate aren't load-bearing;
    #                       trend filter was the only meaningful gate
    #   * V16 ~= V1      -> all 9 modelled gates combined do little;
    #                       signal quality is the real bottleneck
    #
    # IMPORTANT: the backtest harness only models 9 of the live agent's
    # ~40 gates (no cooldowns, opening lockout, concurrency caps, drawdown
    # halt, event blackout, etc.). V16 disables the modelled subset, not
    # the live agent's full safety perimeter.
    ("V16_completely_naked", [
        *_trend_all(None),                                          # per-strategy trend filter off
        ("ensemble.confidence_threshold", 0.0),                     # accept any vote
        ("robustness.min_entry_atr_pct", 0.0),                      # no vol floor
        ("risk.min_profit_to_charges_ratio", 0.0),                  # no RR/charges gate
        ("risk.min_absolute_reward_rs", 0.0),                       # no absolute-reward floor
        ("risk.max_positions", 99),                                 # no concurrent cap
        ("robustness.max_losses_per_stock_per_day", 99),            # no per-stock blacklist
        ("backtest_gates.apply_dead_hour", False),                  # no dead-hour block
        ("backtest_gates.apply_expected_profit_gate", False),       # no profit-gate logic
    ]),

    # ── Tier 7: long-only candidates (2026-05-25) ──
    # The 2026-05-18 90d × 228-stock pre-speed-patch battery showed the
    # short side losing on 339+ trades regardless of trend-filter setting
    # (V1 shorts -Rs 379 / V2 shorts -Rs 398). The cheapest possible
    # frozen-engine fix is `risk.allow_shorts: false`. These variants
    # test whether the long-side edge is independently profitable:
    #
    #   * V17 ~= V1-longs-only  -> shipped config, shorts off
    #   * V18 ~= V4-longs-only  -> 3% trend filter, shorts off
    #   * V19 ~= V2-longs-only  -> filters off, shorts off
    #
    # Compare V17 PnL to V1's long-only-slice (computed offline from
    # V1's trades.json) to confirm the gate behaves exactly as
    # "drop SELL signals before order placement" -- a sanity check
    # on the new code path.
    ("V17_long_only_shipped", [
        ("risk.allow_shorts", False),
    ]),
    ("V18_long_only_threshold_3pct", [
        ("risk.allow_shorts", False),
        *_trend_all(3.0),
    ]),
    ("V19_long_only_filters_off", [
        ("risk.allow_shorts", False),
        *_trend_all(None),
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
    # 2026-05-21: also propagate the two boolean toggles -- apply_dead_hour
    # and apply_expected_profit_gate -- from a dedicated `backtest_gates`
    # config section so variants can disable them. Defaults preserve
    # existing behaviour (both ON) for every variant that doesn't opt in.
    bt_gates = cfg.get("backtest_gates", {}) or {}
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
        apply_dead_hour=bool(bt_gates.get("apply_dead_hour", True)),
        apply_expected_profit_gate=bool(bt_gates.get("apply_expected_profit_gate", True)),
        product_type=cfg.get("execution", {}).get("product_type", "INTRADAY"),
        # 2026-05-25 short-veto flag from `risk.allow_shorts`. Default True
        # so every variant that doesn't explicitly opt in retains the
        # current behaviour (longs + shorts both allowed in backtest).
        allow_shorts=bool(cfg.get("risk", {}).get("allow_shorts", True)),
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

    # 2026-05-25 Bug F diagnostic: enable C-level fault handler so segfaults
    # / aborts / bus errors in native code (numpy, pandas, yfinance,
    # xgboost, etc.) dump a Python traceback before the worker dies. Without
    # this, the parent's ProcessPoolExecutor sees only the generic
    # BrokenProcessPool and we can't tell whether it was a kernel kill, an
    # uncaught native segfault, or an external SIGTERM. Per-variant fault
    # log lives next to the worker log so it survives the worker's death.
    #
    # The file handle MUST stay open for the worker's lifetime (faulthandler
    # writes directly via fd, not through Python's file object). Stashing it
    # on the function is acceptable because the worker is single-threaded
    # at this scope and only one variant runs per call.
    workers_dir = out_root / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    try:
        import faulthandler as _fh
        _fault_fp = open(str(workers_dir / f"{name}.fault.log"), "w")
        _fh.enable(file=_fault_fp, all_threads=True)
    except Exception:
        # faulthandler is best-effort — never fail the worker because we
        # couldn't open the fault log.
        pass

    # Install a per-variant log sink BEFORE doing any heavy work so that
    # market_data.pkl unpickling, feature reload, model loads, and the
    # backtest's per-symbol strategy emissions (e.g. "[vwap_bounce] SELL
    # RELIANCE @ ...") all become visible while the variant is running.
    # enqueue=True because numpy/pandas may emit from threads under the
    # hood; the queue prevents log-line interleaving from racing.
    #
    # 2026-05-25 throughput-degradation fix (loguru sink leak):
    # ProcessPoolExecutor used to REUSE worker subprocesses across tasks.
    # The next variant scheduled to this worker calls _run_variant_in_subprocess
    # again -- and loguru.add() is ADDITIVE. Without removing the previous
    # variant's sink on completion, every subsequent variant's log lines
    # were duplicated into ALL previously-opened sinks. By the 5th
    # variant per worker the per-log-line disk I/O was 5x, dropping
    # throughput from ~21 ev/s to ~7 ev/s during the
    # battery_nifty50_60d_20260522T085929 run. The arithmetic-progression
    # log file sizes were the smoking gun:
    #     V1.log=772K, V3.log=583K, V5.log=394K, V7.log=206K, V9.log=18K
    #     (diff = 188K per cumulative variant, matching V9's emit rate)
    # Fix: capture the sink id, wrap the work in try/finally, remove
    # the sink unconditionally when the variant exits.
    #
    # 2026-05-25 follow-up: parent now also passes max_tasks_per_child=1
    # to ProcessPoolExecutor (Bug F fix), so workers are no longer reused
    # at all -- this finally/remove dance is now belt-and-suspenders rather
    # than the load-bearing fix it once was. We keep it because (a) it's
    # cheap, (b) it remains correct if max_tasks_per_child is ever bumped,
    # and (c) it documents a real prior bug worth not regressing.
    worker_log = workers_dir / f"{name}.log"
    worker_sink_id = logger.add(
        str(worker_log),
        level="INFO",
        enqueue=True,
        # Drop per-bar signal chatter so the worker log stays scannable
        # AND I/O doesn't bottleneck the 2-vCPU backtester VM. See the
        # _battery_log_filter docstring for what's kept vs dropped.
        filter=_battery_log_filter,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:7} | {name}:{function}:{line} - {message}",
        # Defence in depth: even with the quiet filter, a runaway
        # WARNING storm could fill the disk on a multi-day battery.
        # 50 MB rotation + 3-file retention bounds worst-case worker
        # log usage to 200 MB regardless of run length.
        rotation="50 MB",
        retention=3,
    )
    try:
        logger.info(f"[WORKER] starting variant {name}")

        # Watchdog must be installed AFTER the log sink (it reads the log's
        # mtime as the heartbeat) and BEFORE the heavy work (so it covers
        # market_data unpickle, feature load, model load, and the bt.run
        # loop). Disabled when BATTERY_WATCHDOG_SILENCE_MIN=0.
        _spawn_progress_watchdog(
            worker_log=worker_log,
            variant_name=name,
            max_silence_sec=_watchdog_silence_sec(),
        )

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
    finally:
        # ALWAYS remove the per-variant sink before the worker process
        # is reused for the next variant. Best-effort: a remove() failure
        # here would be a loguru internal bug -- swallow it so the next
        # variant's add() still happens. The cumulative-sink leak this
        # prevents is the bug described in the header comment above.
        try:
            logger.remove(worker_sink_id)
        except Exception:
            pass


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
                      *, complete: bool = False, failed: list | None = None,
                      active_workers: list | None = None):
    """Write/overwrite comparison.md.

    Called after every variant (rows grows incrementally), so the file is
    safe to read mid-run. The 'complete' flag adds a marker the resume
    detector keys off of.

    `active_workers` (when provided) is a list of dicts with keys
    {variant, pct, sim_date, rate_ev_s, elapsed, eta, log_age_sec}. The
    live-update thread populates this so the operator can see ongoing
    variants without SSHing in. The block is suppressed when `complete`
    so the final comparison.md doesn't carry a stale "currently running"
    section.
    """
    failed = failed or []
    active_workers = active_workers or []
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

    # Currently-running block (live updates from the live-md thread). Only
    # included when there's actual progress to show AND the run isn't done.
    if active_workers and not complete:
        lines.append("## Currently running\n")
        lines.append("| Variant | %    | sim_date   | rate (ev/s) | elapsed | ETA  | last log |")
        lines.append("|---|---:|---|---:|---|---|---:|")
        for w in active_workers:
            age = int(w.get("log_age_sec", 0))
            age_str = f"{age}s" if age < 120 else f"{age // 60}m"
            lines.append(
                "| {variant} | {pct:5.1f} | {sim_date} | {rate_ev_s:,} | "
                "{elapsed} | {eta} | {age_str} |".format(age_str=age_str, **w)
            )
        lines.append("")

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


def _read_active_workers(workers_dir: Path, completed_names: set,
                         max_age_sec: int = 300) -> list[dict]:
    """Scan workers/*.log for variants currently in progress.

    A worker is "active" iff:
      * its log file exists in workers/ (created by _run_variant_in_subprocess)
      * the variant doesn't already have a results JSON (not completed)
      * the log file's mtime is within max_age_sec (default 5 min) -- this
        filters out logs from finished/crashed prior workers whose logs are
        still on disk but no longer being written to.
      * the log contains at least one [BATTERY-PROGRESS] line.

    Returned dicts carry the parsed progress payload plus the variant name
    and log_age_sec so the renderer can flag stale workers.
    """
    if not workers_dir.exists():
        return []
    now = time.time()
    active: list[dict] = []
    for log_path in workers_dir.glob("*.log"):
        variant = log_path.stem
        if variant in completed_names:
            continue
        try:
            mtime = log_path.stat().st_mtime
        except OSError:
            continue
        log_age = now - mtime
        if log_age > max_age_sec:
            continue
        progress = _parse_last_progress(log_path)
        if progress is None:
            continue
        progress["variant"] = variant
        progress["log_age_sec"] = int(log_age)
        active.append(progress)
    # Most-progressed-first reads naturally for the operator.
    active.sort(key=lambda w: w["pct"], reverse=True)
    return active


def _live_md_loop(
    out_root: Path,
    state_provider,
    stop_event: threading.Event,
    *,
    interval_sec: float = 60.0,
) -> None:
    """Background thread body: rewrite comparison.md every interval_sec.

    `state_provider` is a thread-safe callable returning the tuple
    (rows, failed, completed_names, meta) so the live thread sees a
    consistent snapshot even while the main thread mutates rows on
    each future completion.

    Stops promptly when stop_event is set (single ~interval_sec lag).
    """
    workers_dir = out_root / "workers"
    comp_path = out_root / "comparison.md"
    while not stop_event.wait(interval_sec):
        try:
            rows, failed, completed_names, meta = state_provider()
            active = _read_active_workers(workers_dir, completed_names)
            _write_comparison(
                sorted(rows, key=lambda r: r["variant"]),
                comp_path,
                meta,
                complete=False,
                failed=failed,
                active_workers=active,
            )
        except Exception as exc:  # pragma: no cover -- defensive
            # Live updates are best-effort; a transient FS error shouldn't
            # take the parent down. Log at WARNING so it surfaces but
            # the run continues.
            logger.warning(f"[BATTERY] live-md update failed: {exc!r}")


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
    ap.add_argument("--workers", default="1",
                    help="Number of parallel worker processes for variant "
                         "execution. Default: 1 (serial; preserves legacy "
                         "behavior for CI/tests/debugging). Pass an integer "
                         "or 'auto' to resolve to max(1, cpu_count - 1). "
                         "Variants are embarrassingly parallel; battery-v2 "
                         "(18 variants) wall-time at --workers 4 is ~3x "
                         "faster than serial. Budget ~1.5 GB RAM/worker for "
                         "a 200-stock universe; oversubscription beyond "
                         "cpu_count rarely helps.")
    args = ap.parse_args()

    # Resolve --workers (supports 'auto' + plain integer). 'auto' picks
    # cpu_count - 1 so the OS / docker scheduler / live-md thread always
    # have one core to work on without contention. We surface the resolved
    # value in a log line so the operator can confirm intent in log.txt.
    cpu = os.cpu_count() or 1
    try:
        args.workers, resolution_msg = _resolve_workers(args.workers, cpu)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 8
    if resolution_msg:
        print(resolution_msg)

    # Sanity-clamp workers: 0 or negative is nonsense; >cpu_count just wastes
    # memory on context-switching. Still allow oversubscription if the user
    # explicitly requests it (some I/O wait can hide behind extra processes),
    # but warn so the caller knows it's intentional.
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

    # Mirror loguru into a per-run log file (append on resume).
    # Same noise filter as the worker logs — keeps log.txt scannable.
    # Rotation/retention matches the worker sinks: a multi-day queue can
    # otherwise produce a single multi-GB log.txt.
    logger.add(
        out_root / "log.txt",
        level="INFO",
        filter=_battery_log_filter,
        rotation="50 MB",
        retention=3,
    )

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

        # Coordination primitives for the live-md updater thread:
        #   * comp_lock serialises writes to comparison.md so the main
        #     thread (per-future-completion writes) and the live thread
        #     (every-60s writes) can't interleave a half-rendered file.
        #   * stop_event signals the live thread to exit cleanly when
        #     the with-block finishes.
        comp_lock = threading.Lock()
        stop_event = threading.Event()

        def _state_snapshot():
            # Take a defensive copy of the mutable state the main thread
            # owns so the live thread can iterate without seeing a
            # mid-update race.
            with comp_lock:
                return (
                    list(rows),
                    list(failed),
                    {r["variant"] for r in rows},
                    _meta(datetime.now().isoformat(timespec="seconds")),
                )

        def _locked_write(rows_arg, failed_arg, *, active_workers=None,
                          complete=False):
            with comp_lock:
                _write_comparison(
                    sorted(rows_arg, key=lambda r: r["variant"]),
                    out_root / "comparison.md",
                    _meta(datetime.now().isoformat(timespec="seconds")),
                    complete=complete, failed=failed_arg,
                    active_workers=active_workers,
                )

        live_thread = threading.Thread(
            target=_live_md_loop,
            args=(out_root, _state_snapshot, stop_event),
            kwargs={"interval_sec": 60.0},
            name="battery-live-md",
            daemon=True,
        )
        live_thread.start()
        logger.info("[BATTERY] live comparison.md updater started "
                    "(refresh every 60s while variants run)")

        try:
            # 2026-05-25 Bug F (mass cascade-fail): nifty50_60d V3 died at
            # ~30 min in a re-used pool worker; ProcessPoolExecutor saw the
            # crash and raised BrokenProcessPool for ALL 17 pending variants
            # (V4-V19), even though only V3 had actually crashed. None of
            # the others ever ran.
            #
            # Root cause hypothesis: cross-variant state pollution in re-used
            # workers. Module-globals that survive a variant include
            # trend_context._cache (yfinance daily bars, TTL 6h), yfinance's
            # internal connection pool, xgboost native handles (model load
            # failed in V1 — left lib in possibly bad state), and any
            # numpy/pandas internal caches. V1+V2 ran in fresh workers and
            # passed; V3+V4 ran in re-used workers (worker A's 2nd task = V3,
            # worker B's 2nd task = V4) and died at the same elapsed time.
            #
            # Fix: max_tasks_per_child=1 forces a brand-new subprocess for
            # each variant. Pays a ~15s startup tax per variant (imports +
            # 90 MB market_data unpickle) for full state isolation. With
            # 19 variants × workers=2 that's ~150s total = ~3 min added to
            # a ~40h queue. Negligible.
            #
            # max_tasks_per_child requires Python 3.11+; container ships 3.11.
            with ProcessPoolExecutor(
                max_workers=args.workers,
                max_tasks_per_child=1,
            ) as pool:
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
                        with comp_lock:
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
                        with comp_lock:
                            failed.append(
                                (name, str(e).splitlines()[0] if str(e) else type(e).__name__)
                            )

                    # Per-completion comparison.md write — same lock as
                    # the live thread so we never tear a render.
                    _locked_write(rows, failed)
        except KeyboardInterrupt:
            # Shutdown cleanly: with-block will cancel pending futures.
            logger.warning(f"[BATTERY] interrupted — partial results saved. "
                           f"Resume with: --resume {run_id}")
            stop_event.set()
            live_thread.join(timeout=5)
            _locked_write(rows, failed)
            return 130
        finally:
            # Whatever exit path we take (normal completion, exception,
            # KeyboardInterrupt-already-handled), tear down the live
            # thread before the parent main() returns. join with a timeout
            # so a stuck thread can't hang the process.
            stop_event.set()
            live_thread.join(timeout=5)

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
