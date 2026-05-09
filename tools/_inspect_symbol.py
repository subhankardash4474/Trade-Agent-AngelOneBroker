"""One-shot tool to inspect why a single symbol moved against us.

Usage: python tools/_inspect_symbol.py CROMPTON

Pulls daily history (3 months) + last 30 daily closes, plus today's
5-min bars, plus computes simple trend filters (20d SMA, 50d SMA,
percent above SMA, ATR, recent direction). The output is intended
as a quick "should we have shorted this?" check.
"""

from __future__ import annotations

import sys
from datetime import datetime

import pandas as pd
import yfinance as yf


def main(symbol: str) -> None:
    ticker = f"{symbol}.NS"

    daily = yf.download(ticker, period="3mo", interval="1d",
                        progress=False, auto_adjust=False)
    if isinstance(daily.columns, pd.MultiIndex):
        daily.columns = daily.columns.get_level_values(0)
    if daily.empty:
        print(f"No daily data for {ticker}")
        return

    closes = daily["Close"]
    sma20 = closes.rolling(20).mean()
    sma50 = closes.rolling(50).mean()

    last_close = float(closes.iloc[-1])
    sma20_now = float(sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else None
    sma50_now = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None

    pct_30d = (closes.iloc[-1] / closes.iloc[-30] - 1) * 100 if len(closes) >= 30 else None
    pct_60d = (closes.iloc[-1] / closes.iloc[-60] - 1) * 100 if len(closes) >= 60 else None

    high_30d = float(closes.tail(30).max())
    low_30d = float(closes.tail(30).min())
    high_60d = float(closes.tail(60).max()) if len(closes) >= 60 else None

    print(f"=== {symbol} trend snapshot @ {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    print(f"Last close            : {last_close:.2f}")
    if sma20_now:
        delta20 = (last_close / sma20_now - 1) * 100
        print(f"20-day SMA            : {sma20_now:.2f}  ({'+' if delta20 >= 0 else ''}{delta20:.1f}% vs close)")
    if sma50_now:
        delta50 = (last_close / sma50_now - 1) * 100
        print(f"50-day SMA            : {sma50_now:.2f}  ({'+' if delta50 >= 0 else ''}{delta50:.1f}% vs close)")
    if pct_30d is not None:
        print(f"30-day return         : {pct_30d:+.1f}%")
    if pct_60d is not None:
        print(f"60-day return         : {pct_60d:+.1f}%")
    print(f"30-day high / low     : {high_30d:.2f} / {low_30d:.2f}")
    if high_60d:
        print(f"60-day high           : {high_60d:.2f}")

    high = daily["High"]
    low = daily["Low"]
    close = daily["Close"]
    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]
    print(f"ATR(14, daily)        : {atr14:.2f}")

    last5 = closes.tail(5)
    direction = "UP" if last5.iloc[-1] > last5.iloc[0] else "DOWN"
    move_5d = (last5.iloc[-1] / last5.iloc[0] - 1) * 100
    print(f"Last 5 daily closes   : {direction} ({move_5d:+.1f}%)")

    print()
    print("Last 10 daily closes:")
    last10 = daily.tail(10)
    for d, row in last10.iterrows():
        rng = row["High"] - row["Low"]
        print(f"  {d.date()}  O={row['Open']:>7.2f}  H={row['High']:>7.2f}  "
              f"L={row['Low']:>7.2f}  C={row['Close']:>7.2f}  Range={rng:>5.2f}  "
              f"Vol={int(row['Volume']):>11,}")

    print()
    print("=== VERDICT ===")
    if sma50_now and last_close > sma50_now * 1.02:
        print(f"[BULLISH TREND] Price is {(last_close/sma50_now-1)*100:+.1f}% above 50-day SMA.")
        print("  -> Mean-reversion SHORT against this trend has LOW probability of success.")
        print("  -> Strategy probably should have been blocked by a trend filter.")
    elif sma50_now and last_close < sma50_now * 0.98:
        print(f"[BEARISH TREND] Price is {(last_close/sma50_now-1)*100:+.1f}% below 50-day SMA.")
        print("  -> Mean-reversion SHORT aligned with trend; loss may be timing-related.")
    else:
        print("[NEUTRAL] Price near 50-day SMA. Range-bound — MR signals are valid.")


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "CROMPTON"
    main(sym)
