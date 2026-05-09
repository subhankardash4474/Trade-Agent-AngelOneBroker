"""Lightweight health probe for the trading daemon.

Reads `logs/health.json` (written by the running daemon every heartbeat)
and prints a short summary. Exits non-zero if:
  - File missing (daemon never started, or never reached first heartbeat)
  - Heartbeat is stale (ts_unix older than --max-age-seconds)
  - Daily PnL crosses --pnl-floor (catastrophic loss alert)

Designed to be called from a watchdog (cron / Windows Task Scheduler /
shell loop) so a hung daemon is detected even if its OS process is alive.

Usage:
    python tools/health_check.py
    python tools/health_check.py --max-age-seconds 600 --pnl-floor -2500
    python tools/health_check.py --quiet                # just exit code

Exit codes:
    0  healthy
    1  file missing
    2  heartbeat stale
    3  pnl below floor
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "logs" / "health.json"


def _check(path: Path, max_age: int, pnl_floor: float | None, quiet: bool) -> int:
    if not path.exists():
        if not quiet:
            print(f"[FAIL] no health file at {path}")
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        if not quiet:
            print(f"[FAIL] corrupt health file: {e}")
        return 1

    ts_unix = int(data.get("ts_unix", 0))
    age = int(time.time()) - ts_unix
    if age > max_age:
        if not quiet:
            print(f"[STALE] heartbeat is {age}s old (max={max_age}s) — daemon may be hung")
            print(json.dumps(data, indent=2))
        return 2

    if pnl_floor is not None:
        pnl = float(data.get("daily_pnl", 0.0))
        if pnl < pnl_floor:
            if not quiet:
                print(f"[PNL-FLOOR] daily_pnl={pnl} < floor={pnl_floor}")
                print(json.dumps(data, indent=2))
            return 3

    if not quiet:
        print(
            f"[OK] daemon up: pid={data.get('pid')}  "
            f"age={age}s  "
            f"cycle={data.get('cycle_count')}  "
            f"positions={data.get('open_position_count')}  "
            f"cash=Rs {data.get('cash', 0):,.0f}  "
            f"pnl=Rs {data.get('daily_pnl', 0):+,.0f}  "
            f"trades={data.get('daily_trades')}  "
            f"mode={data.get('mode')}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--max-age-seconds", type=int, default=600,
                    help="alert if heartbeat is older than this (default 10 min)")
    ap.add_argument("--pnl-floor", type=float, default=None,
                    help="alert if daily PnL drops below this (Rs)")
    ap.add_argument("--quiet", action="store_true",
                    help="exit silently with status code only")
    args = ap.parse_args()
    return _check(args.path, args.max_age_seconds, args.pnl_floor, args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
