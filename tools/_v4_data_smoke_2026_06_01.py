"""V4 Mode A — data-availability smoke test.

Goal: verify that yfinance can fetch ~5 years of daily bars for every
instrument in `data/v4_universe_swing_cash.txt`. Surface ETFs (commodity,
debt, sector) most likely to have data gaps; fail loud rather than land
silent-zero-bar series into the V27 backtest.

Output (stdout + JSON):
    Per-symbol: bars-fetched, first-bar date, last-bar date, "OK / SHORT / EMPTY".
    Summary: total OK, total SHORT (< 1000 bars in 5y), total EMPTY.

Run:
    python tools/_v4_data_smoke_2026_06_01.py
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Windows cp1252 console can't render Unicode arrows; force UTF-8.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from core.instruments.etf_universe import (  # noqa: E402
    load_v4_swing_cash_universe,
    universe_categories,
)

WINDOW_DAYS = 5 * 365
MIN_BARS_OK = 1000  # ~4 years of trading days; allows for newly-listed ETFs

OUT_JSON = ROOT / "logs" / "v4_data_smoke_2026_06_01.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        print("[smoke] yfinance not importable; aborting.")
        return 2

    universe = load_v4_swing_cash_universe(
        exclude_cash_sweep=False,  # smoke-test LIQUIDBEES too
        yfinance_suffix=True,
    )
    cats = universe_categories()

    print(f"[smoke] universe size: {len(universe)} (incl. cash-sweep)")
    print(f"[smoke] categories: {sorted(cats.keys())}")
    print(f"[smoke] window: {WINDOW_DAYS} days · interval: 1d")
    print(f"[smoke] start: {datetime.now().strftime('%H:%M:%S')}")
    print("-" * 75)

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=WINDOW_DAYS)

    results = []
    t0 = time.time()
    for i, sym in enumerate(universe, 1):
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=False,
                actions=False,
            )
            n_bars = len(df)
            first_bar = str(df.index[0].date()) if n_bars else "—"
            last_bar = str(df.index[-1].date()) if n_bars else "—"

            if n_bars == 0:
                status = "EMPTY"
            elif n_bars < MIN_BARS_OK:
                status = "SHORT"
            else:
                status = "OK"

            results.append({
                "symbol": sym,
                "bars": n_bars,
                "first": first_bar,
                "last": last_bar,
                "status": status,
            })
            tag = {"OK": "OK", "SHORT": "!!", "EMPTY": "XX"}[status]
            print(
                f"[{i:2d}/{len(universe)}] {tag} {sym:18s} "
                f"bars={n_bars:5d}  {first_bar} → {last_bar}  [{status}]"
            )

        except Exception as e:  # noqa: BLE001
            results.append({
                "symbol": sym,
                "bars": 0,
                "first": "—",
                "last": "—",
                "status": f"ERROR: {type(e).__name__}",
            })
            print(f"[{i:2d}/{len(universe)}] XX {sym:18s} ERROR: {type(e).__name__}: {e}")

    elapsed = time.time() - t0
    summary = {
        "universe_size": len(universe),
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "short": sum(1 for r in results if r["status"] == "SHORT"),
        "empty": sum(1 for r in results if r["status"] == "EMPTY"),
        "errors": sum(1 for r in results if r["status"].startswith("ERROR")),
        "elapsed_sec": round(elapsed, 1),
        "window_days": WINDOW_DAYS,
        "min_bars_ok": MIN_BARS_OK,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
    }

    print("-" * 75)
    print(f"[smoke] {summary['ok']}/{summary['universe_size']} OK · "
          f"{summary['short']} SHORT · {summary['empty']} EMPTY · "
          f"{summary['errors']} ERROR")
    print(f"[smoke] elapsed: {summary['elapsed_sec']}s")
    print(f"[smoke] verdict: {'PASS' if summary['empty'] + summary['errors'] == 0 else 'INVESTIGATE'}")

    OUT_JSON.write_text(json.dumps({
        "summary": summary,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"[smoke] wrote: {OUT_JSON.relative_to(ROOT)}")

    return 0 if summary["empty"] + summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
