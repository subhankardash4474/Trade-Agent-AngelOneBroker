"""Freeze a stock universe to JSON for reproducible battery runs.

Battery-v2 needs a stable list of ~200 NSE symbols. Pulling them live from
the scanner each run risks differences (de-listings, regime-driven volume
changes, NSE feed hiccups) — bad for apples-to-apples variant comparison.

This tool snapshots the scanner's current universe (the live Nifty 500
fetch with the hardcoded NSE_UNIVERSE fallback) into
`tests/fixtures/battery_v2_universe.json`. Once committed, every battery
run that points at this file gets identical input.

Usage:
    python tools/_freeze_battery_v2_universe.py
    python tools/_freeze_battery_v2_universe.py --max 100 --out custom.json

Output JSON shape:
    {
        "universe": ["RELIANCE", "TCS", ...],
        "frozen_at": "2026-05-09T23:30:00+05:30",
        "source": "stock_scanner.NSE_UNIVERSE (hardcoded fallback)",
        "count": 200
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from core.stock_scanner import NSE_UNIVERSE  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "battery_v2_universe.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max", type=int, default=None,
                    help="Cap the universe to first N symbols (default: all).")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT.relative_to(ROOT)}).")
    args = ap.parse_args()

    universe = list(NSE_UNIVERSE)
    if args.max is not None:
        universe = universe[: args.max]

    payload = {
        "universe": universe,
        "frozen_at": datetime.now(IST).isoformat(),
        "source": "core.stock_scanner.NSE_UNIVERSE (hardcoded fallback)",
        "count": len(universe),
    }

    # Resolve so .relative_to(ROOT) works whether user passed a relative or
    # absolute path. Without resolve(), a relative input crashes the print
    # below even though the file write itself succeeded.
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    try:
        display = out_path.relative_to(ROOT)
    except ValueError:
        display = out_path
    print(f"[OK] Universe frozen: {len(universe)} symbols -> {display}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
