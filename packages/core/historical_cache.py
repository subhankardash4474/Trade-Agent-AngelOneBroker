"""Parquet-backed cache for historical OHLCV data.

Why this module exists
======================
The default ``DataSource.get_historical_data`` (yfinance or AngelOne) goes
to the network on every call. For battery backtests that touch the same
(symbol, interval, window) hundreds of times across variants, this is
both slow and rate-limit-risky. ``HistoricalCache`` wraps any DataSource
with a transparent disk cache:

    from core.data_handler import AngelOneDataSource
    from core.historical_cache import HistoricalCache

    inner = AngelOneDataSource(smart_api, broker_cfg)
    cache = HistoricalCache(inner, cache_dir="data/cache/angelone")

    df = cache.get_historical_data("RELIANCE", "5m",
                                   start_date, end_date)
    # First call: fetches via AngelOne, writes parquet.
    # Second call: reads parquet, no network.

Cache layout
------------
``<cache_dir>/<symbol>/<interval>/<start_iso>_<end_iso>.parquet``

Example: ``data/cache/angelone/RELIANCE/5m/20260201_20260301.parquet``

Cache key is the EXACT (symbol, interval, start, end) tuple. Smart
window-matching ("I asked for [Feb-Mar], you have [Jan-Apr]") is
deliberately deferred to a follow-up; exact-key is the 80%-correct
solution and avoids the slicing-correctness bugs that smart matching
introduces.

Deferred to weekend/full impl
-----------------------------
- Smart window subset matching (saves another ~30% of fetches).
- Fall-back to yfinance for windows AngelOne can't serve (>1y old, etc.).
- Concurrent-write safety (locking). Currently safe only for
  single-process callers.
- Cache invalidation on stale data (e.g., when a symbol's listing
  changes, splits, or corporate actions mid-window).
- Per-symbol token cache so we don't rediscover the AngelOne instrument
  token on every miss.

The scaffold is intentionally additive: existing code paths that hit
DataSource directly are unaffected. Opt-in only.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol

import pandas as pd
import pytz
from loguru import logger

# Regression #8 (2026-05-18): TZ-discipline parity with the rest of the
# codebase. Every other "is this today?" check uses IST; the freshness
# guard below used a naive datetime.now() which would silently shift the
# day boundary if the daemon ever ran on a non-IST host (e.g. a CI box
# in UTC, the new OCI VM region default before TZ is set, or a developer
# laptop in another timezone). Standardise on IST here too.
IST = pytz.timezone("Asia/Kolkata")


class _DataSourceLike(Protocol):
    """Minimal protocol that a wrapped data source must satisfy.

    Matches the existing ``DataSource`` ABC in data_handler.py (so any
    concrete subclass -- AngelOneDataSource, YFinanceDataSource -- works
    transparently as the backing store).
    """

    def get_historical_data(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame: ...


class HistoricalCache:
    """Disk-backed cache layer over any DataSource-like object.

    Public surface deliberately mirrors ``DataSource.get_historical_data``
    so callers can swap a raw source for a cached one without touching
    call sites.
    """

    # P2 tick-path cluster (2026-05-17): how stale a cache file may be
    # before we refetch when the request's end_date is recent. History
    # doesn't change for end_dates older than yesterday, so we only
    # apply the TTL when end_date is "today-ish".
    _FRESH_TTL_SECONDS = 3600.0  # 1 hour

    def __init__(self, source: _DataSourceLike, cache_dir: str | Path):
        self._source = source
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────

    def get_historical_data(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        """Return OHLCV bars for [start_date, end_date]. Cache-aware."""
        path = self._key_path(symbol, interval, start_date, end_date)

        if path.exists():
            # P2 tick-path cluster (2026-05-17): freshness check. The OLD
            # code treated the cache as immutable: if the file existed it
            # was returned verbatim. That worked for historical end_dates
            # (yesterday, last week, ...) but broke for end_date=today --
            # the morning's bars were cached, the afternoon's bars were
            # never refetched even after the partial-day parquet was
            # superseded by the broker. Forces refetch when end_date is
            # "today or after" AND the cache file is older than the TTL.
            try:
                # 2026-05-18 (Regression #8): use IST-aware now() so the
                # day boundary matches the rest of the codebase. The
                # mtime comparison stays in epoch seconds (host-clock,
                # which is correct regardless of TZ), but the
                # "is end_date today?" check needs IST-relative dates.
                #
                # 2026-05-18 (Audit Issue #3): localize the NAIVE branch
                # too. The previous version used ``end_dt.date()`` for a
                # naive ``end_date``, which silently picked up the host
                # timezone -- on a UTC host an IST-intended end_date like
                # ``2026-05-18 02:30`` (naive) would compare as
                # ``2026-05-18`` while ``today`` was already ``2026-05-19``
                # IST, flipping the freshness check the wrong way around.
                # We assume naive datetimes from callers are IST-intended
                # (matches the rest of the codebase's convention) and
                # localize before extracting the date.
                today = datetime.now(IST).date()
                end_dt = end_date
                if end_dt.tzinfo is not None:
                    end_date_local = end_dt.astimezone(IST).date()
                else:
                    end_date_local = IST.localize(end_dt).date()
                end_today_or_later = end_date_local >= today
                stale = (
                    end_today_or_later
                    and (datetime.now().timestamp() - path.stat().st_mtime)
                        > self._FRESH_TTL_SECONDS
                )
            except OSError:
                stale = False
            if stale:
                logger.info(
                    f"[HistoricalCache] STALE {symbol} {interval} "
                    f"{start_date.date()}->{end_date.date()} "
                    f"(cache age > {self._FRESH_TTL_SECONDS:.0f}s and "
                    f"end_date is today). Refetching."
                )
            else:
                try:
                    df = pd.read_parquet(path)
                    logger.debug(
                        f"[HistoricalCache] HIT {symbol} {interval} "
                        f"{start_date.date()}->{end_date.date()} "
                        f"({len(df)} bars from {path.name})"
                    )
                    return df
                except Exception as e:
                    # Corrupt / partial parquet -- delete and refetch.
                    logger.warning(
                        f"[HistoricalCache] cache file unreadable, refetching: "
                        f"{path} ({e})"
                    )
                    try:
                        path.unlink()
                    except OSError:
                        pass

        df = self._source.get_historical_data(
            symbol=symbol,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
        )
        if df is None or df.empty:
            logger.warning(
                f"[HistoricalCache] backend returned empty for {symbol} "
                f"{interval} {start_date.date()}->{end_date.date()} -- "
                f"NOT caching the empty frame (avoid poisoning the cache)"
            )
            return pd.DataFrame() if df is None else df

        try:
            df.to_parquet(path, compression="snappy")
            logger.debug(
                f"[HistoricalCache] STORE {symbol} {interval} "
                f"{start_date.date()}->{end_date.date()} "
                f"({len(df)} bars -> {path.name})"
            )
        except Exception as e:
            # Cache write failures must NOT fail the caller. Worst case
            # we just refetch next time.
            logger.warning(
                f"[HistoricalCache] failed to write parquet (continuing "
                f"without cache): {path} ({e})"
            )

        return df

    def invalidate(
        self,
        symbol: Optional[str] = None,
        interval: Optional[str] = None,
    ) -> int:
        """Remove cached parquet files. Returns count deleted.

        - No args: nukes the entire cache.
        - Just ``symbol``: all intervals/windows for that symbol.
        - ``symbol`` + ``interval``: that subtree only.
        """
        if symbol is None:
            target = self._cache_dir
        elif interval is None:
            target = self._cache_dir / symbol
        else:
            target = self._cache_dir / symbol / interval

        if not target.exists():
            return 0

        deleted = 0
        for p in target.rglob("*.parquet"):
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                logger.warning(f"[HistoricalCache] could not delete {p}: {e}")
        logger.info(
            f"[HistoricalCache] invalidated {deleted} parquet files "
            f"under {target}"
        )
        return deleted

    def stats(self) -> dict:
        """Lightweight introspection for ops/debug."""
        files = list(self._cache_dir.rglob("*.parquet"))
        total_bytes = sum(p.stat().st_size for p in files)
        symbols = {p.parts[len(self._cache_dir.parts)]
                   for p in files
                   if len(p.parts) > len(self._cache_dir.parts)}
        return {
            "cache_dir": str(self._cache_dir),
            "files": len(files),
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "symbols": len(symbols),
        }

    # ── Internal helpers ───────────────────────────────────────────────────

    def _key_path(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> Path:
        # Date-only granularity in the filename keeps things human-readable
        # and avoids silly cache-misses from sub-second timestamp differences.
        # Callers wanting intra-day caching (rare for backtests) should
        # extend this to include time.
        start_iso = start_date.strftime("%Y%m%d")
        end_iso = end_date.strftime("%Y%m%d")
        d = self._cache_dir / symbol / interval
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{start_iso}_{end_iso}.parquet"
