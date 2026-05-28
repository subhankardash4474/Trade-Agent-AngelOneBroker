"""
Data Handler Module
Manages market data retrieval from AngelOne SmartAPI and Yahoo Finance.
Supports real-time feeds, OHLCV data, and historical data for backtesting.
"""

import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytz
import requests
from loguru import logger

# B-3 (audit 2026-05-25): SSL verification is now config-driven instead
# of hard-coded off. The previous module-import-time
# `urllib3.disable_warnings(...)` + `self._session.verify = False`
# silently neutered TLS for every Yahoo Finance HTTPS call regardless
# of operator intent. Both are now gated by TRADER_DISABLE_SSL_VERIFY,
# which defaults to "false" (secure) and is set explicitly by
# docker-compose.yml on the cloud VM.
_SSL_BYPASS = os.environ.get("TRADER_DISABLE_SSL_VERIFY", "false").lower() in (
    "1", "true", "yes",
)
if _SSL_BYPASS:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

IST = pytz.timezone("Asia/Kolkata")

# NSE holidays (2025-2026). Update annually or fetch from NSE website.
# B-6 (audit 2026-05-25): the hardcoded calendar covers 2025 + 2026 only.
# Without proactive maintenance, the first request after 2026-12-25 falls
# off the end of the set and `is_trading_day()` returns True for every
# 2027 holiday, causing the daemon to attempt scans/trades on holidays
# (broker rejects with cascading errors). `_holiday_coverage_years` makes
# the supported range explicit; `is_known_holiday_year()` lets callers
# (run_daemon, market_safety) fail-closed when asked about a year we
# haven't curated yet. Update annually.
#
# Bug L (2026-05-28 live miss): the 2026 list was previously curated by
# date-only without festival names and turned out to contain 9 spurious
# entries (02-17, 03-20, 03-30, 05-25, 07-07, 08-15, 08-17, 10-09, 10-21)
# and 7 missing real holidays (01-15, 03-26, 03-31, 05-28, 06-26, 09-14,
# 11-10). The 2026-05-28 (Bakri Id) miss caused the trader daemon to
# scan a closed market for ~7 hours on a holiday. Damage was zero
# (defensive stack -- opening lockout + allow_shorts:false + paper mode
# + xgb-disabled -- absorbed everything), but the next gap is
# 2026-06-26 (Muharram, Friday). The list below was cross-checked
# against Samco / Upstox / Zerodha / ET / Outlook Business -- all 5
# sources agree on the 16 holidays for 2026. See
# docs/findings_log_2026-05-27.md section 12 (Bug L).
#
# CONTRACT for future curators: every entry MUST carry the festival
# name + day-of-week as a comment. A bare ISO date with no comment is
# a code-review red flag; the comment is what makes the calendar
# auditable against any third-party source without re-reading code.
NSE_HOLIDAYS = {
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
    "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
    "2025-10-20", "2025-10-21", "2025-10-22", "2025-11-05", "2025-11-26",
    "2025-12-25",
    # 2026 -- 16 holidays, Bug L corrected list (2026-05-28). Verified
    # against Samco/Upstox/Zerodha/ET/Outlook Business.
    "2026-01-15",  # THU  Municipal Corporation Elections in Maharashtra
    "2026-01-26",  # MON  Republic Day
    "2026-03-03",  # TUE  Holi
    "2026-03-26",  # THU  Shri Ram Navami
    "2026-03-31",  # TUE  Shri Mahavir Jayanti
    "2026-04-03",  # FRI  Good Friday
    "2026-04-14",  # TUE  Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # FRI  Maharashtra Day / Buddha Pournima
    "2026-05-28",  # THU  Bakri Id  (Bug L: added 2026-05-28 after live miss)
    "2026-06-26",  # FRI  Muharram  (Bug L: corrected from wrong 2026-07-07)
    "2026-09-14",  # MON  Ganesh Chaturthi
    "2026-10-02",  # FRI  Mahatma Gandhi Jayanti
    "2026-10-20",  # TUE  Dussehra
    "2026-11-10",  # TUE  Diwali-Balipratipada
    "2026-11-24",  # TUE  Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # FRI  Christmas
    # Holidays falling on Sat/Sun (already non-trading; intentionally NOT
    # listed above): 2026-02-15 Mahashivratri (Sun), 2026-03-21 Eid-Ul-Fitr
    # (Sat), 2026-08-15 Independence Day (Sat), 2026-11-08 Diwali Laxmi
    # Pujan (Sun -- special Muhurat trading session this date, timings
    # announced closer to date; not a normal full-day session, currently
    # not encoded here).
}

# Years for which `NSE_HOLIDAYS` is authoritative. Derived from the set
# above so this stays in sync automatically when the next year is added.
_HOLIDAY_COVERAGE_YEARS = {int(s.split("-", 1)[0]) for s in NSE_HOLIDAYS}


def is_known_holiday_year(year: int) -> bool:
    """True iff the operator has curated the NSE holiday list for this year.

    Callers asking about a year outside coverage should NOT assume "no
    holidays" — they should either skip the day defensively or surface a
    loud warning so the operator updates `NSE_HOLIDAYS`. B-6.
    """
    return int(year) in _HOLIDAY_COVERAGE_YEARS

ANGELONE_INTERVALS = {
    "1min": "ONE_MINUTE",
    "3min": "THREE_MINUTE",
    "5min": "FIVE_MINUTE",
    "10min": "TEN_MINUTE",
    "15min": "FIFTEEN_MINUTE",
    "30min": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
}

YF_INTERVALS = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1h": "1h",
    "1d": "1d",
}


class DataSource(ABC):
    """Abstract base class for data sources."""

    @abstractmethod
    def get_historical_data(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    def get_ltp(self, symbol: str, token: str) -> Optional[float]:
        pass


class AngelOneDataSource(DataSource):
    """Data source backed by AngelOne SmartAPI."""

    def __init__(self, smart_api, config: dict):
        self._api = smart_api
        self._config = config
        self._rate_limiter = RateLimiter(max_calls=3, period=1.0)

    def get_historical_data(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        token = self._resolve_token(symbol)
        if token is None:
            logger.warning(f"Token not found for {symbol}, falling back to empty frame")
            return pd.DataFrame()

        ao_interval = ANGELONE_INTERVALS.get(interval)
        if ao_interval is None:
            raise ValueError(f"Unsupported interval '{interval}' for AngelOne")

        all_data: List[pd.DataFrame] = []
        chunk_start = start_date
        # AngelOne limits to 2000 candles per request; paginate by day chunks
        chunk_delta = timedelta(days=30) if interval in ("1d",) else timedelta(days=5)

        while chunk_start < end_date:
            chunk_end = min(chunk_start + chunk_delta, end_date)
            self._rate_limiter.wait()
            try:
                params = {
                    "exchange": self._config.get("exchange", "NSE"),
                    "symboltoken": token,
                    "interval": ao_interval,
                    "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
                    "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
                }
                response = self._api.getCandleData(params)
                if response and response.get("status"):
                    candles = response.get("data", [])
                    if candles:
                        df = pd.DataFrame(
                            candles,
                            columns=["timestamp", "open", "high", "low", "close", "volume"],
                        )
                        df["timestamp"] = pd.to_datetime(df["timestamp"])
                        df.set_index("timestamp", inplace=True)
                        all_data.append(df)
                else:
                    logger.warning(f"AngelOne API returned no data for {symbol} chunk {chunk_start}")
            except Exception as e:
                logger.error(f"Error fetching AngelOne data for {symbol}: {e}")

            chunk_start = chunk_end

        if not all_data:
            return pd.DataFrame()

        result = pd.concat(all_data).sort_index()
        result = result[~result.index.duplicated(keep="first")]
        for col in ("open", "high", "low", "close", "volume"):
            result[col] = pd.to_numeric(result[col], errors="coerce")
        return result

    def get_ltp(self, symbol: str, token: str) -> Optional[float]:
        self._rate_limiter.wait()
        try:
            data = self._api.ltpData(
                self._config.get("exchange", "NSE"),
                symbol,
                token,
            )
            if data and data.get("status"):
                return float(data["data"]["ltp"])
        except Exception as e:
            logger.error(f"Error fetching LTP for {symbol}: {e}")
        return None

    def get_order_book(self, symbol: str, token: str) -> Optional[dict]:
        self._rate_limiter.wait()
        try:
            data = self._api.getMarketData(
                mode="FULL",
                exchangeTokens={self._config.get("exchange", "NSE"): [token]},
            )
            if data and data.get("status"):
                fetched = data.get("data", {}).get("fetched", [])
                if fetched:
                    return fetched[0]
        except Exception as e:
            logger.error(f"Error fetching order book for {symbol}: {e}")
        return None

    def _resolve_token(self, symbol: str) -> Optional[str]:
        instruments = self._config.get("instruments", [])
        for inst in instruments:
            if inst["symbol"] == symbol:
                return inst["token"]
        return None


class YahooFinanceDataSource(DataSource):
    """
    Data source backed by Yahoo Finance direct chart API.
    Bypasses the yfinance library to avoid crumb/cookie rate limits
    (common on corporate networks with shared IPs).
    """

    NSE_SUFFIX = ".NS"
    _CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    _YF_INTERVAL_MAP = {
        "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
        "1h": "1h", "1d": "1d",
    }

    def __init__(self):
        self._session = requests.Session()
        self._session.verify = not _SSL_BYPASS
        self._session.headers.update(self._HEADERS)

    def _chart_request(self, ticker: str, interval: str = "1d",
                       range_str: str = None, period1: int = None,
                       period2: int = None) -> pd.DataFrame:
        """Low-level Yahoo chart v8 request → DataFrame."""
        url = self._CHART_URL.format(ticker=ticker)
        params = {"interval": interval, "includePrePost": "false"}
        if range_str:
            params["range"] = range_str
        if period1 is not None:
            params["period1"] = period1
        if period2 is not None:
            params["period2"] = period2

        try:
            resp = self._session.get(url, params=params, timeout=10)
            if resp.status_code == 429:
                time.sleep(3)
                resp = self._session.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return pd.DataFrame()

            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                return pd.DataFrame()

            quote = result[0].get("indicators", {}).get("quote", [{}])[0]
            timestamps = result[0].get("timestamp", [])
            if not timestamps or not quote:
                return pd.DataFrame()

            df = pd.DataFrame({
                "open": quote.get("open", []),
                "high": quote.get("high", []),
                "low": quote.get("low", []),
                "close": quote.get("close", []),
                "volume": quote.get("volume", []),
            }, index=pd.to_datetime(timestamps, unit="s"))
            df.index.name = "timestamp"
            return df.dropna(subset=["close"])

        except Exception as e:
            logger.debug(f"Yahoo chart error for {ticker}: {e}")
            return pd.DataFrame()

    def get_historical_data(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        yf_interval = self._YF_INTERVAL_MAP.get(interval, "1d")
        ticker_symbol = f"{symbol}{self.NSE_SUFFIX}"

        period1 = int(start_date.timestamp())
        period2 = int(end_date.timestamp())

        df = self._chart_request(ticker_symbol, interval=yf_interval,
                                 period1=period1, period2=period2)

        if df.empty:
            # For intraday, Yahoo needs range-based requests (max 60 days for 5m)
            if "m" in yf_interval or "h" in yf_interval:
                diff_days = (end_date - start_date).days
                range_str = f"{max(diff_days, 1)}d"
                if diff_days > 60:
                    range_str = "60d"
                df = self._chart_request(ticker_symbol, interval=yf_interval,
                                         range_str=range_str)

        if df.empty:
            logger.warning(f"No Yahoo data for {ticker_symbol} ({yf_interval})")

        return df

    def get_ltp(self, symbol: str, token: str = "") -> Optional[float]:
        """Get latest price using a 1-day range request."""
        ticker_symbol = f"{symbol}{self.NSE_SUFFIX}"
        df = self._chart_request(ticker_symbol, interval="1d", range_str="1d")
        if not df.empty:
            return float(df["close"].iloc[-1])

        # Fallback: try 5-day range
        df = self._chart_request(ticker_symbol, interval="1d", range_str="5d")
        if not df.empty:
            return float(df["close"].iloc[-1])

        return None


class RateLimiter:
    """Token-bucket style rate limiter for API calls."""

    def __init__(self, max_calls: int = 3, period: float = 1.0):
        self._max_calls = max_calls
        self._period = period
        self._timestamps: List[float] = []

    def wait(self):
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < self._period]
        if len(self._timestamps) >= self._max_calls:
            sleep_time = self._period - (now - self._timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.monotonic())


class DataHandler:
    """
    Unified data handler that orchestrates data retrieval from multiple sources.
    Primary: AngelOne (for live trading).
    Fallback: Yahoo Finance (for simulation/backtest).
    """

    # B-9 (audit 2026-05-25): hard cap on the historical-data cache. The
    # previous unbounded dict accumulated every (symbol, interval, start, end)
    # tuple ever requested, and on a long-running daemon (or any battery run
    # that iterates over multiple windows) climbed to GB-scale RSS before
    # being noticed. Cap is a tunable env var; default 256 covers a Nifty 500
    # scan x 1 window x 2 intervals comfortably.
    _CACHE_MAX_ENTRIES = int(os.environ.get("DATA_HANDLER_CACHE_MAX_ENTRIES", "256"))

    def __init__(self, config: dict, smart_api=None):
        self._config = config
        self._market_config = config.get("market", {})
        self._cache: Dict[str, pd.DataFrame] = {}
        # B-9: track insertion order for cheap FIFO eviction without
        # importing an OrderedDict.
        self._cache_order: List[str] = []

        self._yahoo = YahooFinanceDataSource()
        self._angelone: Optional[AngelOneDataSource] = None

        if smart_api is not None:
            self._angelone = AngelOneDataSource(smart_api, self._market_config)

    @property
    def primary_source(self) -> DataSource:
        if self._angelone is not None:
            return self._angelone
        return self._yahoo

    def get_historical_data(
        self,
        symbol: str,
        interval: str = "5min",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Fetch OHLCV historical data, trying primary source then fallback."""
        if end_date is None:
            end_date = datetime.now(IST)
        if start_date is None:
            start_date = end_date - timedelta(days=60)

        # Don't cache intraday data — it needs to refresh each cycle
        is_intraday = interval in ("1min", "5min", "15min", "30min", "1h")
        cache_key = f"{symbol}_{interval}_{start_date.date()}_{end_date.date()}"
        if use_cache and not is_intraday and cache_key in self._cache:
            # F-52: return a defensive copy on cache hit. Previously this
            # returned the cached DataFrame by reference -- any caller
            # that mutated it (added columns, fillna(inplace=True),
            # sliced+reassigned) silently corrupted the cache for every
            # subsequent caller in the same process.
            return self._cache[cache_key].copy()

        df = pd.DataFrame()
        for attempt in range(3):
            try:
                df = self.primary_source.get_historical_data(symbol, interval, start_date, end_date)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                wait = 2 ** attempt
                logger.warning(f"Historical data retry {attempt + 1}/3 for {symbol}: {e}")
                time.sleep(wait)
            except Exception as e:
                logger.error(f"Historical data error for {symbol}: {e}")
                break

        if df.empty and self._angelone is not None:
            logger.info(f"Primary source empty for {symbol}, falling back to Yahoo Finance")
            df = self._yahoo.get_historical_data(symbol, interval, start_date, end_date)

        if not df.empty and use_cache and not is_intraday:
            self._cache_put(cache_key, df)

        return df

    def _cache_put(self, key: str, df: pd.DataFrame) -> None:
        """Insert into the historical cache, evicting oldest if over cap.

        B-9 (audit 2026-05-25): the previous implementation `self._cache[key] = df`
        was unbounded. On long-running daemons / battery sweeps with many
        (symbol, window) tuples this leaked memory until OOM. FIFO is fine
        because the access pattern is "scan a universe, never look back".
        """
        if key in self._cache:
            try:
                self._cache_order.remove(key)
            except ValueError:
                pass
        self._cache[key] = df
        self._cache_order.append(key)
        while len(self._cache_order) > self._CACHE_MAX_ENTRIES:
            victim = self._cache_order.pop(0)
            self._cache.pop(victim, None)

    def get_ltp(self, symbol: str, token: str = "", retries: int = 2) -> Optional[float]:
        """Get last traded price with retry on transient failures."""
        for attempt in range(retries + 1):
            try:
                price = self.primary_source.get_ltp(symbol, token)
                if price is None and self._angelone is not None:
                    price = self._yahoo.get_ltp(symbol)
                return price
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LTP fetch retry {attempt + 1}/{retries} for {symbol} (wait {wait}s): {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"LTP fetch failed after {retries} retries for {symbol}: {e}")
            except Exception as e:
                logger.error(f"LTP fetch error for {symbol}: {e}")
                break
        return None

    def get_multiple_ltp(self, instruments: List[dict]) -> Dict[str, Optional[float]]:
        """Get LTP for multiple instruments with per-symbol error isolation."""
        prices = {}
        for inst in instruments:
            try:
                prices[inst["symbol"]] = self.get_ltp(inst["symbol"], inst.get("token", ""))
            except Exception as e:
                logger.error(f"LTP error for {inst['symbol']}: {e}")
                prices[inst["symbol"]] = None
        return prices

    def get_order_book(self, symbol: str, token: str) -> Optional[dict]:
        """Get order book depth (AngelOne only)."""
        if self._angelone is not None:
            return self._angelone.get_order_book(symbol, token)
        logger.warning("Order book depth not available in Yahoo Finance mode")
        return None

    def is_market_open(self) -> bool:
        """Check if Indian equity market is currently open (weekday + non-holiday + trading hours)."""
        now = datetime.now(IST)
        if now.weekday() >= 5:  # Saturday/Sunday
            return False
        if not is_known_holiday_year(now.year):
            # B-6 (audit 2026-05-25): warn loudly when the holiday calendar
            # has not been updated for the current year. We keep returning
            # True (don't break automation) but the operator will see this
            # in trading_agent_*.log every cycle until they extend
            # NSE_HOLIDAYS — and the warning text spells out the fix.
            logger.warning(
                f"[holiday-calendar] NSE_HOLIDAYS has no entries for {now.year}; "
                f"is_market_open() cannot rule out a holiday today. Update "
                f"packages/core/data_handler.py:NSE_HOLIDAYS with this year's "
                f"NSE schedule."
            )
        if now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
            return False
        trading_hours = self._market_config.get("trading_hours", {})
        market_open = datetime.strptime(trading_hours.get("start", "09:15"), "%H:%M").time()
        market_close = datetime.strptime(trading_hours.get("end", "15:30"), "%H:%M").time()
        return market_open <= now.time() <= market_close

    def clear_cache(self):
        self._cache.clear()
        self._cache_order.clear()

    def download_historical_for_backtest(
        self,
        symbols: List[str],
        interval: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Bulk download historical data for backtesting."""
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else datetime.now(IST) - timedelta(days=365)
        end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now(IST)

        data = {}
        for symbol in symbols:
            logger.info(f"Downloading historical data for {symbol}...")
            df = self._yahoo.get_historical_data(symbol, interval, start, end)
            if not df.empty:
                data[symbol] = df
                logger.info(f"  {symbol}: {len(df)} bars downloaded")
            else:
                logger.warning(f"  {symbol}: no data available")
        return data
