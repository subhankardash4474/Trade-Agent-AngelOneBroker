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

import threading
import time
from typing import Optional

import pandas as pd
from loguru import logger

THRESHOLD_PCT = 5.0  # symbol must be within +/- 5% of 50d SMA to trade with trend
CACHE_TTL_SEC = 6 * 3600
_cache: dict[str, dict] = {}
_lock = threading.Lock()


def _fetch_daily(symbol: str) -> Optional[dict]:
    """Pull 3 months of daily bars from yfinance, compute SMAs."""
    try:
        import yfinance as yf
        df = yf.download(f"{symbol}.NS", period="3mo", interval="1d",
                         progress=False, auto_adjust=False)
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
