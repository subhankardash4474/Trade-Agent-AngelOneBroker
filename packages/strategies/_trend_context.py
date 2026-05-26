"""Module-level daily-trend cache shared across strategies.

The intraday 5-min DataFrames passed to `Strategy.generate_signal()` only
hold ~1-2 sessions of context — not enough to know whether a stock is in
a multi-month uptrend. This module fetches daily bars on demand, caches a
50-day SMA per symbol, and exposes a single helper:

    is_against_trend(symbol, side) -> bool

A SHORT entry is "against trend" when the last close is more than
`THRESHOLD_PCT` above the 50-day SMA. A LONG entry is "against trend"
when the close is more than `THRESHOLD_PCT` below the 50-day SMA.

Cache TTL is 6 hours, so each symbol is fetched at most twice per
trading session (once at warmup, once mid-session). Fetch failures are
treated as "trend unknown" -> filter does NOT block the trade (fail-open
to avoid silently disabling the strategy on data outages).

Why module-level state and not a class? Strategies are instantiated once
each by trading_agent and don't have access to a shared service registry.
A module-level cache is the simplest cross-strategy sharing mechanism.

Calibration of THRESHOLD_PCT:
- Today's data showed SHORTs at +8% (POLICYBZR) all the way up to +26%
  (MEESHO) above 50d SMA, all of which were trend-mismatched.
- Setting threshold at 5% blocks all four. Setting at 10% would let
  POLICYBZR through. We use 5% as a conservative starting point;
  Phase 2 backtest will refine.

Why the 50-day SMA specifically?
- Daily 50-SMA is a well-known proxy for medium-term trend.
- It updates slowly enough not to flip on weekly noise but fast enough
  to react to regime change inside a quarter.
"""

from __future__ import annotations

import concurrent.futures as _cf
import os
import threading
import time
from typing import Optional

import pandas as pd
from loguru import logger

THRESHOLD_PCT = 5.0  # symbol must be within +/- 5% of 50d SMA to trade with trend
CACHE_TTL_SEC = 6 * 3600
# 2026-05-25 Bug G-3: hard timeout on the yfinance HTTP call. Without
# this, a stalled connection (slow Yahoo response, broken DNS, hung
# socket) hangs the worker thread indefinitely. The battery harness'
# 30-min progress watchdog would eventually kill the worker, but that
# triggers a ProcessPoolExecutor cascade-kill of all sibling variants
# (see Bug F / Bug G-2). 30s is generous for a single ~3-month daily
# bar pull.
_YF_TIMEOUT_SEC = float(os.environ.get("TREND_FETCH_TIMEOUT_SEC", "30"))
_cache: dict[str, dict] = {}
_lock = threading.Lock()


def _yf_download_with_timeout(symbol: str, timeout: float) -> "pd.DataFrame | None":
    """Run `yfinance.download` in a worker thread with a hard timeout.

    Why a thread (not a signal alarm or `yf.download(..., timeout=N)`)?
    - Signals only work on the main thread of the main interpreter and
      we're called from worker subprocesses where signal-based
      preemption is fragile.
    - `yfinance.download` does not consistently honour an outer
      `timeout=` kwarg across versions; relying on it would silently
      regress on a yfinance upgrade.
    - A `ThreadPoolExecutor` with `result(timeout=N)` is portable and
      version-stable. If the underlying call is genuinely hung, the
      future leaks the thread until process exit, but
      `max_tasks_per_child=1` (Bug F fix) guarantees process exit
      after this single variant — so the leak is bounded to one
      worker lifetime.

    2026-05-26 Bug G-3 audit fix: this function used to use
    ``with _cf.ThreadPoolExecutor(...) as ex:`` for cleanup. That is
    WRONG -- `Executor.__exit__` calls ``shutdown(wait=True)``, which
    blocks until every running task completes. When the inner
    ``yf.download`` is genuinely hung (the exact case this whole
    helper exists to defend against), the timeout would fire
    correctly, but the with-block's __exit__ would then block
    FOREVER waiting for the hung thread to finish. The function
    never returns, the worker hangs, the watchdog eventually fires,
    and the ProcessPoolExecutor cascades -- exactly the failure mode
    G-3 was supposed to prevent. The fix below uses an explicit
    try/finally with ``shutdown(wait=False, cancel_futures=True)`` so
    the function returns within ``timeout`` seconds even when the
    fetch thread is stuck. The hung thread leaks, but max_tasks_per_child=1
    guarantees process exit (and thread reaping) after the variant.
    """
    def _do_fetch() -> "pd.DataFrame | None":
        import yfinance as yf
        return yf.download(
            f"{symbol}.NS", period="3mo", interval="1d",
            progress=False, auto_adjust=False,
        )
    ex = _cf.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="trend-fetch",
    )
    try:
        return ex.submit(_do_fetch).result(timeout=timeout)
    except _cf.TimeoutError:
        logger.warning(
            f"[trend_context] yfinance timed out after {timeout}s for "
            f"{symbol}; treating as 'trend unknown' (fail-open)."
        )
        return None
    finally:
        # wait=False is critical: a hung yf.download must not block
        # this caller. cancel_futures=True is best-effort -- it only
        # cancels tasks that haven't started yet, but since we
        # submitted exactly one task and result() already started it,
        # this is a no-op in practice. Kept for forward-compat with
        # any future refactor that submits more than one task.
        ex.shutdown(wait=False, cancel_futures=True)


def _fetch_daily(symbol: str) -> Optional[dict]:
    """Pull 3 months of daily bars from yfinance, compute SMAs.

    Hard-timeouted via `_yf_download_with_timeout` (Bug G-3).
    Fetch failures (timeout, network error, empty result, parse error)
    return None and the caller treats the trend as unknown (fail-open
    so a data-source outage doesn't silently disable the strategy).
    """
    try:
        df = _yf_download_with_timeout(symbol, _YF_TIMEOUT_SEC)
        if df is None:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 50:
            return None
        closes = df["Close"]
        sma50 = float(closes.rolling(50).mean().iloc[-1])
        sma20 = float(closes.rolling(20).mean().iloc[-1])
        last = float(closes.iloc[-1])
        return {
            "sma50": sma50,
            "sma20": sma20,
            "last_close": last,
            "pct_vs_sma50": (last / sma50 - 1) * 100 if sma50 > 0 else 0.0,
        }
    except Exception as e:
        logger.debug(f"[trend_context] fetch failed for {symbol}: {e}")
        return None


def get_trend(symbol: str, *, force_refresh: bool = False) -> Optional[dict]:
    """Return cached trend dict for symbol, fetching if stale.

    Returns None on fetch failure -> callers should treat as "unknown,
    let the trade through" rather than blocking on missing data.
    """
    now = time.time()
    with _lock:
        cached = _cache.get(symbol)
        if not force_refresh and cached and (now - cached["fetched_at"]) < CACHE_TTL_SEC:
            return cached["data"]
    data = _fetch_daily(symbol)
    with _lock:
        _cache[symbol] = {"fetched_at": now, "data": data}
    return data


def is_against_trend(symbol: str, side: str, *, threshold_pct: float = THRESHOLD_PCT) -> bool:
    """Return True if a `side` entry on `symbol` fights the daily trend.

    SHORT against +X% above 50d SMA -> blocked.
    LONG against -X% below 50d SMA  -> blocked.

    Fail-open: if we can't fetch trend data, returns False (don't block).
    """
    trend = get_trend(symbol)
    if trend is None or trend.get("pct_vs_sma50") is None:
        return False
    pct = trend["pct_vs_sma50"]
    if side.upper() == "SELL":
        return pct > threshold_pct
    if side.upper() == "BUY":
        return pct < -threshold_pct
    return False


def clear_cache() -> None:
    """Clear the cache (used by tests)."""
    with _lock:
        _cache.clear()
