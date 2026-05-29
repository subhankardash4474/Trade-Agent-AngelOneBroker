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
# F-48 (audit 2026-05-27): on a fetch failure we previously cached the
# None result for the FULL 6h positive-TTL, so a single transient
# yfinance hiccup at market open silently disabled the trend filter
# for the entire session (combined with the default fail-open path,
# that admits every counter-trend entry until market close). Use a
# short negative TTL so we retry within minutes of the outage clearing
# while still bounding yfinance pressure on a persistent failure.
_NEGATIVE_CACHE_TTL_SEC = float(os.environ.get("TREND_NEG_TTL_SEC", "300"))
# 2026-05-25 Bug G-3: hard timeout on the yfinance HTTP call. Without
# this, a stalled connection (slow Yahoo response, broken DNS, hung
# socket) hangs the worker thread indefinitely. The battery harness'
# 30-min progress watchdog would eventually kill the worker, but that
# triggers a ProcessPoolExecutor cascade-kill of all sibling variants
# (see Bug F / Bug G-2). 30s is generous for a single ~3-month daily
# bar pull.
_YF_TIMEOUT_SEC = float(os.environ.get("TREND_FETCH_TIMEOUT_SEC", "30"))
# C-14 (audit 2026-05-26): hard cap on cache size to bound memory in
# long-running battery workers (200+ symbols × dozens of variants without
# this leaked into the gigabytes; same failure class as B-9 DataHandler
# cache). LRU-ish eviction by oldest fetched_at; eviction triggers only
# when we exceed the cap so the steady state is still O(symbols_in_use).
_CACHE_MAX_ENTRIES = int(os.environ.get("TREND_CACHE_MAX_ENTRIES", "2000"))
# C-13 (audit 2026-05-26): fail-closed mode. When set to "1"/"true", a
# fetch failure (timeout, empty response, network error) is treated as
# "trend unknown -> block the trade" instead of the fail-open default.
# Operators running live should consider flipping this on (combined with
# a healthy yfinance / NSE archive fallback) so a data outage doesn't
# silently disable the trend filter and admit counter-trend entries.
_FAIL_CLOSED = os.environ.get("TREND_FILTER_FAIL_CLOSED", "false").lower() in (
    "1", "true", "yes", "on",
)
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


def _fetch_daily(symbol: str, as_of_date=None) -> Optional[dict]:
    """Pull 3 months of daily bars from yfinance, compute SMAs.

    Hard-timeouted via `_yf_download_with_timeout` (Bug G-3).
    Fetch failures (timeout, network error, empty result, parse error)
    return None and the caller treats the trend as unknown (fail-open
    so a data-source outage doesn't silently disable the strategy).

    NUM-05 / NUM-15 (audit 2026-05-28): pre-fix this used
    ``closes.iloc[-1]`` unconditionally. During an intraday call the
    LAST bar is TODAY's HALF-FORMED daily candle -- the 50d SMA
    therefore included a forming bar (live lookahead). The trend
    filter would happily route an entry against a forward-looking
    50d SMA, breaking backtest/live parity for every strategy that
    calls ``is_against_trend`` (every one of them with
    ``trend_filter_pct`` set).

    Fix: when the last bar's date matches ``as_of_date`` (defaults to
    today IST) we drop it -- the prior completed session is the
    cutoff. Backtest callers pass the simulation cutoff date so the
    historical re-fetch returns deterministic, leak-free trend.
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

        # NUM-05 / NUM-15: drop the forming-today bar if it sneaks
        # into the daily series. yfinance's intraday daily request
        # often returns a bar dated today with the price as of "now"
        # -- which gives the rolling SMA visibility into a future-
        # incomplete value.
        try:
            from datetime import datetime as _dt
            import pytz as _pytz
            cutoff = as_of_date
            if cutoff is None:
                cutoff = _dt.now(_pytz.timezone("Asia/Kolkata")).date()
            elif hasattr(cutoff, "date"):
                cutoff = cutoff.date()
            last_idx = closes.index[-1]
            last_date = last_idx.date() if hasattr(last_idx, "date") else None
            if last_date is not None and last_date >= cutoff:
                closes = closes.iloc[:-1]
                if len(closes) < 50:
                    return None
        except Exception:
            # Defensive: if the date comparison fails (timezone-naive
            # index, exotic Timestamp impl), fall back to legacy
            # behaviour rather than block trading.
            pass

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


def _evict_oldest_locked() -> None:
    """Evict the oldest cache entries while holding `_lock`.

    C-14 (audit 2026-05-26): keeps `len(_cache) <= _CACHE_MAX_ENTRIES` by
    dropping the lowest `fetched_at` entries first. Cheap because we only
    pay the sort when we actually cross the cap.
    """
    if len(_cache) <= _CACHE_MAX_ENTRIES:
        return
    overflow = len(_cache) - _CACHE_MAX_ENTRIES
    victims = sorted(_cache.items(), key=lambda kv: kv[1].get("fetched_at", 0.0))[:overflow]
    for key, _ in victims:
        _cache.pop(key, None)


def get_trend(symbol: str, *, force_refresh: bool = False, as_of_date=None) -> Optional[dict]:
    """Return cached trend dict for symbol, fetching if stale.

    Returns None on fetch failure -> callers should treat as "unknown,
    let the trade through" rather than blocking on missing data.

    NUM-05 / NUM-15 (audit 2026-05-28): ``as_of_date`` is forwarded to
    ``_fetch_daily`` so backtest callers (which set the simulation
    cutoff) get leak-free trend. Live callers can omit it -- the
    helper defaults to today (IST).
    """
    now = time.time()
    # NUM-05 / NUM-15 (audit 2026-05-28): incorporate the as-of date
    # into the cache key so a backtest sweep across multiple
    # simulation dates does not pollute live daemon's cache. The
    # legacy live path passes None -> empty suffix -> identical key
    # behaviour for backward compatibility.
    cache_key = symbol if as_of_date is None else f"{symbol}@{as_of_date!s}"
    with _lock:
        cached = _cache.get(cache_key)
        if not force_refresh and cached is not None:
            age = now - cached["fetched_at"]
            ttl = (
                CACHE_TTL_SEC
                if cached.get("data") is not None
                else _NEGATIVE_CACHE_TTL_SEC
            )
            if age < ttl:
                return cached["data"]
    data = _fetch_daily(symbol, as_of_date=as_of_date)
    with _lock:
        # F-48: still cache the negative result so we don't hammer
        # yfinance during a sustained outage, but the negative TTL
        # is much shorter than the positive one (see constants).
        _cache[cache_key] = {"fetched_at": now, "data": data}
        _evict_oldest_locked()
    return data


def is_against_trend(symbol: str, side: str, *, threshold_pct: float = THRESHOLD_PCT,
                     as_of_date=None) -> bool:
    """Return True if a `side` entry on `symbol` fights the daily trend.

    SHORT against +X% above 50d SMA -> blocked.
    LONG against -X% below 50d SMA  -> blocked.

    By default fail-open: if we can't fetch trend data, returns False so a
    yfinance outage doesn't silently disable the strategy. Set
    `TREND_FILTER_FAIL_CLOSED=true` to invert (block the trade when trend
    is unknown). C-13 (audit 2026-05-26).
    """
    trend = get_trend(symbol, as_of_date=as_of_date)
    if trend is None or trend.get("pct_vs_sma50") is None:
        if _FAIL_CLOSED:
            logger.warning(
                f"[trend_context] trend unknown for {symbol}; fail-closed "
                f"mode active -> blocking {side}"
            )
            return True
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
