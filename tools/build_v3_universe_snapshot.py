"""Refresh the v3.0 swing universe snapshot (top 30 by 60-day ADTV).

Per `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §3, the v3.0 swing
trading hypothesis universe is **top 30 from Nifty 50 by 60-day average
traded value**. This tool computes that from yfinance daily-bar data
and refreshes `data/v3_universe_top30.json`.

The initial commit of `data/v3_universe_top30.json` (2026-05-30) used a
market-cap-ordered first-30 from `tests/fixtures/nifty50_universe.json`
as a proxy for ADTV (correlation > 0.95 for Nifty 50). Once this tool
is run with live data on the backtester VM, the snapshot becomes
ADTV-accurate. The two outputs typically differ by 2-3 names.

Usage:
    python tools/build_v3_universe_snapshot.py
    python tools/build_v3_universe_snapshot.py --window-days 60 --top-n 30
    python tools/build_v3_universe_snapshot.py --as-of 2025-12-01
    python tools/build_v3_universe_snapshot.py --dry-run

Run on the backtester VM where yfinance is reachable. Output is
deterministic given fixed (window-days, top-n, as-of) inputs.

Refresh cadence: quarterly per charter §3, OR ad-hoc when:
* Nifty 50 composition changes (NSE announces an index review).
* A liquidity-shock event (e.g. corporate action) bumps a name out
  of the top-30 by a wide margin.
* Phase B / Phase C transition wants a fresh universe.

Output JSON shape (matches battery `--universe-file` contract):

    {
        "_meta": { ... v3 charter cross-references, snapshot date,
                   methodology, survivorship-bias note ... },
        "universe": ["RELIANCE", "TCS", ...]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import pandas as pd
import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_OUT = ROOT / "data" / "v3_universe_top30.json"
NIFTY50_FIXTURE = ROOT / "tests" / "fixtures" / "nifty50_universe.json"


def _load_nifty50() -> List[str]:
    """Read the Nifty 50 universe fixture (the candidate pool from
    which we pick the top-30 by ADTV).
    """
    payload = json.loads(NIFTY50_FIXTURE.read_text(encoding="utf-8"))
    return list(payload["universe"])


def _compute_adtv(
    symbols: List[str],
    *,
    window_days: int,
    as_of: datetime,
) -> "pd.Series":
    """Compute average daily traded value (ADTV = avg(close * volume)) over
    the trailing ``window_days`` ending at ``as_of`` for each symbol.

    Returns a pandas Series indexed by symbol with ADTV in INR.
    Symbols whose data is missing or partial fall to the bottom (NaN
    sorts last under ascending), so they won't be picked.
    """
    from core.data_handler import DataHandler  # noqa: WPS433
    import yaml  # noqa: WPS433

    cfg_path = ROOT / "config.yaml"
    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    dh = DataHandler(config)

    end_date = as_of.strftime("%Y-%m-%d")
    start_date = (as_of - timedelta(days=window_days * 2)).strftime("%Y-%m-%d")
    # Pull 2x window-days so partial trading-day overlap with weekends/
    # holidays still leaves enough trading bars for the trailing window.

    data = dh.download_historical_for_backtest(
        symbols=symbols,
        interval="1d",
        start_date=start_date,
        end_date=end_date,
    )

    rows = {}
    cutoff = as_of - timedelta(days=window_days)
    for sym, df in data.items():
        if df is None or df.empty:
            rows[sym] = float("nan")
            continue
        df = df.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)
        windowed = df[df.index >= cutoff.astimezone(IST)]
        if windowed.empty:
            rows[sym] = float("nan")
            continue
        adtv = float((windowed["close"] * windowed["volume"]).mean())
        rows[sym] = adtv

    return pd.Series(rows, name="adtv_inr").sort_values(ascending=False)


def _build_payload(
    top_symbols: List[str],
    *,
    snapshot_date: datetime,
    window_days: int,
    top_n: int,
) -> dict:
    """Assemble the JSON payload with full provenance metadata."""
    return {
        "_meta": {
            "name": "v3.0 Swing Universe — Top 30 Nifty 50 by Liquidity",
            "description": (
                f"Top {top_n} most-liquid Nifty 50 names by trailing "
                f"{window_days}-day average daily traded value (ADTV = "
                f"mean of close * volume). Computed by "
                f"tools/build_v3_universe_snapshot.py from yfinance "
                f"daily bars. Per freeze_v3.0_charter_2026-05-30.md §3."
            ),
            "snapshot_date": snapshot_date.strftime("%Y-%m-%d"),
            "snapshot_method": (
                f"yfinance_daily_close_x_volume_trailing_{window_days}d"
            ),
            "refresh_cadence": "quarterly",
            "refresh_tool": "tools/build_v3_universe_snapshot.py",
            "v3_charter_ref": "docs/freeze/freeze_v3.0_charter_2026-05-30.md §3",
            "survivorship_bias_note": (
                "This snapshot represents top-N AS OF the snapshot date. "
                "A 180-day backtest starting BEFORE the snapshot ideally "
                "uses the top-N AS OF the backtest start. For Nifty 50 "
                "the index turns over ~3-4 names/year so the bias on a "
                "180d window is bounded but non-zero. v3.1+ may add "
                "per-day valid_from/valid_to schema; deferred per A1 "
                "gap analysis §6 / §11 R2."
            ),
            "frozen_at": datetime.now(IST).isoformat(),
            "frozen_by": "tools/build_v3_universe_snapshot.py",
        },
        "universe": top_symbols,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window-days", type=int, default=60,
                    help="Trailing window for ADTV (default: 60).")
    ap.add_argument("--top-n", type=int, default=30,
                    help="Number of top-liquid symbols to keep (default: 30).")
    ap.add_argument(
        "--as-of", default=None,
        help=("Snapshot date in YYYY-MM-DD; default = today (IST). Use a "
              "past date to back-fill a snapshot at the start of a "
              "specific backtest window — reduces survivorship bias."),
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT.relative_to(ROOT)}).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the proposed top-N to stdout without overwriting.")
    args = ap.parse_args()

    if args.as_of:
        as_of_dt = IST.localize(datetime.strptime(args.as_of, "%Y-%m-%d"))
    else:
        as_of_dt = datetime.now(IST)

    candidates = _load_nifty50()
    print(f"[v3-universe] computing ADTV over {args.window_days}d "
          f"as-of {as_of_dt.date()} for {len(candidates)} candidates...")

    adtv = _compute_adtv(candidates, window_days=args.window_days, as_of=as_of_dt)
    top = adtv.head(args.top_n)
    print(f"[v3-universe] top {args.top_n} by ADTV:")
    for sym, val in top.items():
        if pd.isna(val):
            print(f"    {sym:<14}  (no data)")
        else:
            print(f"    {sym:<14}  ADTV ~= INR {val/1e9:.2f}B")

    top_symbols = top.index.tolist()
    payload = _build_payload(
        top_symbols,
        snapshot_date=as_of_dt,
        window_days=args.window_days,
        top_n=args.top_n,
    )

    if args.dry_run:
        print("\n[v3-universe] --dry-run: payload would be:")
        print(json.dumps(payload, indent=2))
        return 0

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        display = out_path.relative_to(ROOT)
    except ValueError:
        display = out_path
    print(f"\n[OK] v3 universe snapshot written: {len(top_symbols)} symbols "
          f"-> {display}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
