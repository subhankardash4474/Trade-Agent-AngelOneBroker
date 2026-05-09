"""Post-mortem: did we leave money on the table on MEESHO today?

Pulls actual 5-min bars and walks through what each trailing-stop variant
would have captured, vs what we actually got (+147.92).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENTRY_PRICE = 206.56
ENTRY_TIME = "2026-05-07 09:15"
EXIT_TIME_ACTUAL = "2026-05-07 10:21"
EXIT_PRICE_ACTUAL = 202.45
QTY = 38
SL = 209.72
TP = 200.42
COMMISSION_EST = 8.50


def main() -> None:
    df = yf.download("MEESHO.NS", period="2d", interval="5m", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        print("No 5min data")
        return

    df.index = df.index.tz_convert("Asia/Kolkata") if df.index.tz else df.index
    today = df[df.index.date == pd.Timestamp("2026-05-07").date()]
    if today.empty:
        print("No bars for 2026-05-07 yet — using last available trading day")
        today = df.tail(80)

    print(f"=== MEESHO 5-min bars (today, up to ~10:30) ===\n")
    upto = today[today.index <= pd.Timestamp("2026-05-07 10:30").tz_localize("Asia/Kolkata")] if today.index.tz else today
    if upto.empty:
        upto = today.head(20)

    print(f"  {'Time':<10} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'SHORT-pnl-on-Low':>20}")
    bar_lows = []
    for ts, row in upto.iterrows():
        time_str = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
        pnl_low = (ENTRY_PRICE - row["Low"]) * QTY  # max favourable for SHORT
        marker = ""
        if abs(row["Low"] - row["Low"]) < 0.01 and row["Low"] < ENTRY_PRICE:
            marker = ""
        print(f"  {time_str:<10} {row['Open']:>8.2f} {row['High']:>8.2f} {row['Low']:>8.2f} "
              f"{row['Close']:>8.2f}  {pnl_low:>+15.2f}")
        bar_lows.append((ts, row["Low"], row["Close"], row["High"]))

    print()
    if bar_lows:
        peak_bar = min(bar_lows, key=lambda x: x[1])
        peak_pnl = (ENTRY_PRICE - peak_bar[1]) * QTY - COMMISSION_EST
        print(f"BEST BAR LOW: {peak_bar[1]:.2f} @ {peak_bar[0].strftime('%H:%M')} "
              f"-> would have netted Rs +{peak_pnl:.2f}")

    actual_net = (ENTRY_PRICE - EXIT_PRICE_ACTUAL) * QTY - COMMISSION_EST
    print(f"\nACTUAL EXIT  : 202.45 @ 10:21  -> +{actual_net:.2f} net (gross +{(ENTRY_PRICE - EXIT_PRICE_ACTUAL) * QTY:.2f})")

    print()
    print("=== TRAILING STOP SIMULATIONS ===\n")
    print(f"Setup: entry 206.56 SHORT, 38 qty, SL=209.72, TP=200.42")
    print(f"Trail kicks in once unrealized > 0.5R (R = entry - SL_distance = ~3.16, so 0.5R = Rs 1.58/share = +60)")
    print()

    activated = False
    best_low = ENTRY_PRICE  # for SHORT, "best" = lowest low touched
    for trail_pct in [0.20, 0.30, 0.40, 0.50]:
        activated = False
        best_low = ENTRY_PRICE
        exit_price = None
        exit_time = None
        for ts, low, close, high in bar_lows:
            if low < best_low:
                best_low = low
            unrealized = (ENTRY_PRICE - low) * QTY
            if unrealized > 60:
                activated = True
            if not activated:
                continue
            atr_proxy = (high - low)
            trail_distance = max(trail_pct * atr_proxy, 0.30)
            trail_stop = best_low + trail_distance
            if high >= trail_stop:
                exit_price = trail_stop
                exit_time = ts
                break

        if exit_price is None:
            net = actual_net
            label = "no trail trigger - unchanged"
        else:
            gross = (ENTRY_PRICE - exit_price) * QTY
            net = gross - COMMISSION_EST
            label = f"exited at {exit_price:.2f} @ {exit_time.strftime('%H:%M')}"
        delta = net - actual_net
        print(f"  Trail = {trail_pct:.0%} of bar-range:  {label:<45}  net Rs {net:+8.2f}  ({delta:+.2f} vs actual)")

    print()
    print("=== TIGHTER TP SIMULATIONS ===\n")
    for tp_test in [201.00, 201.50, 202.00, 202.50]:
        hit = False
        for ts, low, close, high in bar_lows:
            if low <= tp_test:
                gross = (ENTRY_PRICE - tp_test) * QTY
                net = gross - COMMISSION_EST
                delta = net - actual_net
                print(f"  TP @ {tp_test:.2f}: HIT @ {ts.strftime('%H:%M')}  -> net Rs {net:+.2f}  ({delta:+.2f} vs actual)")
                hit = True
                break
        if not hit:
            print(f"  TP @ {tp_test:.2f}: not hit before our exit")


if __name__ == "__main__":
    main()
