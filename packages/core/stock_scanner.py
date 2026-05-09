"""
Stock Scanner Module
Automatically discovers, filters, and ranks NSE stocks for trading.
Eliminates the need for manual stock selection — the agent decides
what to trade based on liquidity, price, momentum, and volatility.
"""

import os
import ssl
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytz
import requests as req
from loguru import logger

# Suppress SSL warnings on corporate networks
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


def _yahoo_chart(symbol: str, session: req.Session, days: int = 25) -> pd.DataFrame:
    """
    Fetch OHLCV data directly from Yahoo Finance chart API,
    bypassing the yfinance library (avoids crumb/cookie rate limits).
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": f"{days}d", "interval": "1d", "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = session.get(url, params=params, headers=headers, timeout=10, verify=False)
        if resp.status_code == 429:
            time.sleep(5)
            resp = session.get(url, params=params, headers=headers, timeout=10, verify=False)
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

        return df.dropna(subset=["close"])

    except Exception:
        return pd.DataFrame()

IST = pytz.timezone("Asia/Kolkata")


def _fetch_nse_index_symbols(session: req.Session, index_name: str = "NIFTY 500") -> List[str]:
    """
    Fetch live index constituents from the NSE website.
    Falls back to the hardcoded list if NSE is unreachable.
    """
    try:
        url = "https://www.nseindia.com/api/equity-stockIndices"
        params = {"index": index_name}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }
        # NSE needs a session cookie first
        s = req.Session()
        s.verify = False
        s.headers.update(headers)
        s.get("https://www.nseindia.com", timeout=5)
        resp = s.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            symbols = [item["symbol"] for item in data.get("data", [])
                       if item.get("symbol") and item["symbol"] != index_name]
            if len(symbols) > 50:
                logger.info(f"Fetched {len(symbols)} stocks from NSE {index_name} index")
                return symbols
    except Exception as e:
        logger.debug(f"NSE index fetch failed ({e}), using hardcoded universe")
    return []


# Hardcoded fallback: Nifty 50 + Nifty Next 50 + popular mid/small caps
# Total ~300 liquid stocks covering most of the tradeable NSE universe.
NSE_UNIVERSE = [
    # ── Nifty 50 ──
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NTPC",
    "WIPRO", "POWERGRID", "NESTLEIND", "TATAMOTORS", "M&M",
    "JSWSTEEL", "ADANIENT", "ADANIPORTS", "ONGC", "COALINDIA",
    "TATASTEEL", "BAJAJFINSV", "TECHM", "INDUSINDBK", "HINDALCO",
    "BPCL", "GRASIM", "DRREDDY", "DIVISLAB", "CIPLA",
    "EICHERMOT", "BRITANNIA", "APOLLOHOSP", "TATACONSUM", "HEROMOTOCO",
    "SBILIFE", "BAJAJ-AUTO", "HDFCLIFE", "LTIM", "UPL",
    # ── Nifty Next 50 ──
    "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "AUROPHARMA", "BAJAJHLDNG",
    "BANDHANBNK", "BERGEPAINT", "BIOCON", "BOSCHLTD", "CHOLAFIN",
    "COLPAL", "DABUR", "DLF", "GLAND", "GODREJCP",
    "HAVELLS", "ICICIGI", "ICICIPRULI", "IDFCFIRSTB", "INDHOTEL",
    "IOC", "JINDALSTEL", "JSWENERGY", "LICI", "LUPIN",
    "MARICO", "MAXHEALTH", "MOTHERSON", "MUTHOOTFIN", "NAUKRI",
    "NHPC", "OBEROIRLTY", "OFSS", "PAGEIND", "PIDILITIND",
    "PNB", "POLYCAB", "SBICARD", "SHREECEM", "SIEMENS",
    "SRF", "TATAELXSI", "TATAPOWER", "TORNTPHARM", "TRENT",
    "VEDL", "ZOMATO", "ZYDUSLIFE",
    # ── Popular mid-caps / small-caps (liquid, affordable) ──
    "IDEA", "YESBANK", "IRCTC", "PAYTM", "IRFC",
    "BANKBARODA", "SAIL", "NATIONALUM", "NMDC", "GAIL",
    "RECLTD", "PFC", "BHEL", "CANBK", "UNIONBANK",
    "FEDERALBNK", "ABCAPITAL", "MANAPPURAM", "GMRINFRA", "TTML",
    "SUZLON", "JPPOWER", "RPOWER", "NBCC", "HUDCO",
    "RVNL", "BEL", "HAL", "CDSL", "INDIANB",
    "CENTRALBK", "BANKINDIA", "FACT", "COCHINSHIP",
    # ── Additional high-volume mid/small caps ──
    "ASHOKLEY", "AFFLE", "AAVAS", "APLAPOLLO", "ASTRAL",
    "BALRAMCHIN", "BDL", "CANFINHOME", "CASTROLIND", "CESC",
    "CHAMBLFERT", "COFORGE", "CONCOR", "CROMPTON", "CUMMINSIND",
    "DEEPAKNTR", "DELTACORP", "DIXON", "EIDPARRY", "ELGIEQUIP",
    "ESCORTS", "EXIDEIND", "FINCABLES", "FORTIS", "GLENMARK",
    "GNFC", "GRANULES", "GSPL", "GUJGASLTD", "HFCL",
    "HINDCOPPER", "HINDPETRO", "HONAUT", "IBULHSGFIN", "IEX",
    "INDIACEM", "INDIAMART", "INDUSTOWER", "INTELLECT", "IOB",
    "IPCALAB", "IRB", "ISEC", "JKCEMENT", "JKLAKSHMI",
    "JSL", "JUBLFOOD", "KAJARIACER", "KEI", "KEC",
    "KPITTECH", "LALPATHLAB", "LAURUSLABS", "LICHSGFIN", "LTTS",
    "MFSL", "MGL", "MPHASIS", "MRPL", "NAM-INDIA",
    "NAVINFLUOR", "NIACL", "NLCINDIA", "OLECTRA", "PGHH",
    "PHOENIXLTD", "PIIND", "PRESTIGE", "PVRINOX", "RAIN",
    "RAJESHEXPO", "RBLBANK", "RECLTD", "RELCHEMQ", "RENUKA",
    "ROUTE", "SANOFI", "SAPPHIRE", "SCHAEFFLER", "SJVN",
    "STARHEALTH", "SUNTV", "SUPREMEIND", "SYNGENE", "TATACHEM",
    "TATACOMM", "TATAINVEST", "TATATECH", "THERMAX", "TIINDIA",
    "TORNTPOWER", "TRIDENT", "TRITURBINE", "TV18BRDCST", "TVSMOTOR",
    "UBL", "VOLTAS", "WELCORP", "WHIRLPOOL", "ZEEL",
]


class StockScanner:
    """
    Autonomous stock scanner that replaces manual instrument selection.

    Every scan cycle:
      1. Fetches live data for the NSE universe.
      2. Filters by: price range (affordable), minimum volume, minimum volatility.
      3. Computes a composite score (momentum + volume + volatility).
      4. Ranks and returns the top N candidates.

    The agent calls scan() at startup and periodically re-scans to rotate
    into the best opportunities.
    """

    def __init__(self, config: dict):
        scanner_cfg = config.get("scanner", {})
        capital = config.get("capital", {}).get("initial_balance", 10000.0)

        # Price filter: stock must be affordable (can buy >= min_shares shares)
        self.max_price: float = scanner_cfg.get("max_price", capital * 0.20)  # max 20% of capital per stock
        self.min_price: float = scanner_cfg.get("min_price", 5.0)  # avoid penny stocks under ₹5

        # Volume filter: minimum average daily volume
        self.min_avg_volume: int = scanner_cfg.get("min_avg_volume", 500_000)

        # Volatility: minimum ATR% for enough intraday movement
        self.min_atr_pct: float = scanner_cfg.get("min_atr_pct", 1.0)

        # How many stocks to select
        self.top_n: int = scanner_cfg.get("top_n", 10)

        # Universe: try live NSE index first, then hardcoded fallback
        custom_universe = scanner_cfg.get("universe", [])
        if custom_universe:
            self.universe: List[str] = custom_universe
        else:
            session = req.Session()
            session.verify = False
            live = _fetch_nse_index_symbols(session, "NIFTY 500")
            self.universe: List[str] = live if live else NSE_UNIVERSE
            # Deduplicate
            seen = set()
            self.universe = [s for s in self.universe if not (s in seen or seen.add(s))]

        # Rescan interval
        self.rescan_interval_minutes: int = scanner_cfg.get("rescan_interval_minutes", 60)

        # Cache
        self._last_scan_time: Optional[datetime] = None
        self._cached_results: List[dict] = []

        logger.info(
            f"Scanner initialized | Universe: {len(self.universe)} stocks | "
            f"Price: ₹{self.min_price}-₹{self.max_price:.0f} | "
            f"Min volume: {self.min_avg_volume:,} | Top: {self.top_n}"
        )

    def scan(self, force: bool = False) -> List[dict]:
        """
        Run a full scan and return top N instruments.

        Returns:
            List of dicts: [{"symbol": "SBIN", "token": "", "score": 0.85, ...}, ...]
        """
        if not force and self._cached_results and self._last_scan_time:
            elapsed = (datetime.now(IST) - self._last_scan_time).total_seconds() / 60
            if elapsed < self.rescan_interval_minutes:
                logger.debug(f"Using cached scan results ({elapsed:.0f}m old)")
                return self._cached_results

        logger.info(f"Scanning {len(self.universe)} NSE stocks...")
        start_time = time.monotonic()

        candidates = self._fetch_universe_data()
        filtered = self._apply_filters(candidates)
        ranked = self._rank_and_select(filtered)

        elapsed = time.monotonic() - start_time
        self._cached_results = ranked
        self._last_scan_time = datetime.now(IST)

        symbols = [r["symbol"] for r in ranked]
        logger.info(
            f"Scan complete in {elapsed:.1f}s | "
            f"{len(candidates)} fetched → {len(filtered)} filtered → {len(ranked)} selected"
        )
        logger.info(f"Selected: {symbols}")

        return ranked

    def needs_rescan(self) -> bool:
        """Check if it's time for a rescan."""
        if self._last_scan_time is None:
            return True
        elapsed = (datetime.now(IST) - self._last_scan_time).total_seconds() / 60
        return elapsed >= self.rescan_interval_minutes

    def _fetch_universe_data(self) -> List[dict]:
        """Fetch current price, volume, and short history for all stocks in the universe."""
        results = []
        session = req.Session()
        session.verify = False
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        fetched = 0
        for idx, symbol in enumerate(self.universe):
            try:
                ticker_yf = f"{symbol}.NS"
                df = _yahoo_chart(ticker_yf, session, days=25)

                if df.empty:
                    continue

                fetched += 1

                if "close" not in df.columns or len(df) < 5:
                    continue

                df = df.dropna(subset=["close"])
                if df.empty:
                    continue

                current_price = float(df["close"].iloc[-1])
                avg_volume = float(df["volume"].mean()) if "volume" in df.columns else 0

                # ATR percentage (volatility measure)
                if len(df) >= 14 and all(c in df.columns for c in ("high", "low", "close")):
                    tr = pd.concat([
                        df["high"] - df["low"],
                        (df["high"] - df["close"].shift()).abs(),
                        (df["low"] - df["close"].shift()).abs(),
                    ], axis=1).max(axis=1)
                    atr = float(tr.rolling(14).mean().iloc[-1])
                    atr_pct = (atr / current_price) * 100 if current_price > 0 else 0
                else:
                    atr = 0.0
                    atr_pct = 0.0

                # Momentum: 5-day return
                if len(df) >= 6:
                    momentum_5d = (current_price - float(df["close"].iloc[-6])) / float(df["close"].iloc[-6]) * 100
                else:
                    momentum_5d = 0.0

                # Recent volume spike: today vs 20-day avg
                vol_today = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0
                vol_ratio = vol_today / avg_volume if avg_volume > 0 else 0

                # RSI (14)
                if len(df) >= 15:
                    delta = df["close"].diff()
                    gain = delta.where(delta > 0, 0.0).ewm(com=13, min_periods=14).mean()
                    loss = (-delta.where(delta < 0, 0.0)).ewm(com=13, min_periods=14).mean()
                    rs = gain / loss.replace(0, np.nan)
                    rsi_val = float((100 - 100 / (1 + rs)).iloc[-1])
                else:
                    rsi_val = 50.0

                results.append({
                    "symbol": symbol,
                    "token": "",  # resolved at broker level
                    "price": round(current_price, 2),
                    "avg_volume": int(avg_volume),
                    "vol_ratio": round(vol_ratio, 2),
                    "atr": round(atr, 2),
                    "atr_pct": round(atr_pct, 2),
                    "momentum_5d": round(momentum_5d, 2),
                    "rsi": round(rsi_val, 1),
                })

            except Exception as e:
                logger.debug(f"Skip {symbol}: {e}")
                continue

            # Small delay every 5 tickers to avoid rate limits
            if (idx + 1) % 5 == 0 and idx + 1 < len(self.universe):
                time.sleep(1)

        logger.info(f"Downloaded data for {fetched}/{len(self.universe)} stocks")
        return results

    def _apply_filters(self, candidates: List[dict]) -> List[dict]:
        """Filter stocks by price, volume, and volatility."""
        filtered = []
        for c in candidates:
            # Price filter
            if c["price"] < self.min_price or c["price"] > self.max_price:
                continue

            # Volume filter
            if c["avg_volume"] < self.min_avg_volume:
                continue

            # Volatility filter: need enough movement for intraday
            if c["atr_pct"] < self.min_atr_pct:
                continue

            # Skip extreme RSI (probably already moved too much)
            if c["rsi"] > 85 or c["rsi"] < 15:
                continue

            filtered.append(c)

        return filtered

    def _rank_and_select(self, filtered: List[dict]) -> List[dict]:
        """
        Rank filtered stocks by a composite score and pick top N.

        Score = weighted combination of:
          - Volume ratio (higher = more interest today)
          - ATR% (higher = more intraday range)
          - Absolute momentum (closer to 0 = mean reversion opportunity,
            OR strong = trend opportunity; we score both sides)
        """
        if not filtered:
            logger.warning("No stocks passed filters. Relaxing criteria...")
            return []

        for c in filtered:
            # Normalize components to [0, 1] range
            max_vol_ratio = max(x["vol_ratio"] for x in filtered) or 1
            max_atr_pct = max(x["atr_pct"] for x in filtered) or 1
            max_momentum = max(abs(x["momentum_5d"]) for x in filtered) or 1

            vol_score = c["vol_ratio"] / max_vol_ratio
            atr_score = c["atr_pct"] / max_atr_pct
            # Reward both trend (strong momentum) and mean reversion (weak momentum near mean)
            momentum_score = abs(c["momentum_5d"]) / max_momentum

            # RSI-based opportunity score: extremes are more interesting
            rsi_score = abs(c["rsi"] - 50) / 50  # 0 at RSI=50, 1 at RSI=0 or 100

            c["score"] = round(
                0.30 * vol_score +
                0.25 * atr_score +
                0.25 * momentum_score +
                0.20 * rsi_score,
                4,
            )

        # Sort by score descending
        ranked = sorted(filtered, key=lambda x: x["score"], reverse=True)
        return ranked[:self.top_n]

    def get_scan_summary(self) -> str:
        """Human-readable summary of latest scan."""
        if not self._cached_results:
            return "No scan results available."

        lines = ["Stock Scanner Results:", "=" * 65]
        lines.append(f"{'Symbol':<12} {'Price':>8} {'AvgVol':>10} {'ATR%':>6} {'Mom5d':>7} {'RSI':>5} {'Score':>6}")
        lines.append("-" * 65)
        for r in self._cached_results:
            lines.append(
                f"{r['symbol']:<12} {r['price']:>8.2f} {r['avg_volume']:>10,} "
                f"{r['atr_pct']:>5.1f}% {r['momentum_5d']:>+6.1f}% {r['rsi']:>5.1f} {r['score']:>6.4f}"
            )
        lines.append(f"\nSelected {len(self._cached_results)} instruments for trading")
        return "\n".join(lines)
