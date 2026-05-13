"""Reconcile our SQLite ``trades`` table against AngelOne's broker-side
``tradeBook`` for any given trading date.

Why this exists
---------------
Our daemon writes a row to ``trades`` only when a position is **closed**
(see ``Database.store_trade`` called from ``TradingAgent._on_trade_closed``).
The broker's ``tradeBook`` records every **fill**, both entry and exit.
For Stage 3 (live 5-stock basket) and beyond, we need a daily sanity check
that:

  * every fill the broker recorded made it into our DB, and
  * every DB row corresponds to fills that actually happened at the broker.

Any mismatch is a P0 issue: it means our daemon and the broker disagree
about how much capital is at risk, which positions exist, or what
realised P&L is.

Reconciliation strategy
-----------------------
We aggregate both sources to ``(symbol, side)`` -> (count, sum_qty) where:

  * From DB: each closed-trade row expands into TWO legs --
        entry leg: (symbol, db.side,     qty)
        exit leg:  (symbol, opposite,    qty)
    For a long trade (db.side='BUY'): entry=BUY, exit=SELL
    For a short trade (db.side='SELL'): entry=SELL, exit=BUY

  * From broker: each tradeBook row is already one leg with its own side.

We then diff: any (symbol, side) bucket where DB count or qty does not
exactly equal broker count or qty is reported.

Open positions
--------------
If the daemon still has an open position when this tool runs, the entry
leg lives in our `positions` table, not `trades`. We don't include open
positions in the diff (so running this mid-day will report the
unmatched-broker-leg for any open position -- expected). Run it
end-of-day or set ``--ignore-symbols`` to mute those.

Usage
-----
    # default: today
    python tools/reconcile_trade_book.py

    # historical
    python tools/reconcile_trade_book.py --date 2026-05-13

    # only flag bad mismatches; ignore open-position legs
    python tools/reconcile_trade_book.py --ignore-symbols HCLTECH,RELIANCE

    # output as JSON (for cron/CI)
    python tools/reconcile_trade_book.py --json

Exit codes
----------
    0  -- DB and broker agree on every (symbol, side) bucket
    1  -- at least one mismatch found
   77  -- could not connect to broker / could not read DB
   99  -- uncaught exception
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_DB_PATH = ROOT / "data" / "trading_agent.db"
DEFAULT_CONFIG = ROOT / "config.yaml"


def _today_iso() -> str:
    return datetime.now(IST).date().isoformat()


def _expand_db_trade_to_legs(row: dict) -> list[tuple[str, str, int]]:
    """Expand a closed-trade row into the (symbol, side, qty) legs that
    the broker would have recorded.

    For a long trade in the DB (side='BUY'):
        entry leg = BUY,  qty  (at entry_price)
        exit  leg = SELL, qty  (at exit_price)
    For a short trade in the DB (side='SELL'):
        entry leg = SELL, qty  (at entry_price)
        exit  leg = BUY,  qty  (at exit_price)
    """
    symbol = row.get("symbol", "")
    qty = int(row.get("quantity") or 0)
    side = (row.get("side") or "").upper()
    if not symbol or qty <= 0 or side not in ("BUY", "SELL"):
        return []
    opposite = "SELL" if side == "BUY" else "BUY"
    return [(symbol, side, qty), (symbol, opposite, qty)]


def _load_db_legs(db_path: Path, day_iso: str) -> list[tuple[str, str, int]]:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, side, quantity, entry_time, exit_time, pnl, "
            "strategy, exit_reason FROM trades "
            "WHERE substr(exit_time,1,10) = ? ORDER BY exit_time",
            (day_iso,),
        ).fetchall()
    legs: list[tuple[str, str, int]] = []
    for r in rows:
        legs.extend(_expand_db_trade_to_legs(dict(r)))
    return legs


def _broker_legs_for_day(config_path: Path, day_iso: str) -> list[tuple[str, str, int]]:
    """Connect to AngelOne and return tradebook legs for the given date.

    Note: AngelOne's tradeBook endpoint returns ONLY today's fills (it does
    not accept a date arg). We honour ``day_iso`` defensively by filtering
    out rows whose ``filltime`` is not on that date.
    """
    import yaml

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    from core.secrets import load_dotenv
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(str(env_path))

    broker_cfg = config.get("broker", {}) or {}
    creds = {
        "api_key":     os.getenv("ANGELONE_API_KEY")     or broker_cfg.get("api_key", ""),
        "api_secret":  os.getenv("ANGELONE_API_SECRET")  or broker_cfg.get("api_secret", ""),
        "client_id":   os.getenv("ANGELONE_CLIENT_ID")   or broker_cfg.get("client_id", ""),
        "password":    os.getenv("ANGELONE_PASSWORD")    or broker_cfg.get("password", ""),
        "totp_secret": os.getenv("ANGELONE_TOTP_SECRET") or broker_cfg.get("totp_secret", ""),
        "min_required_cash": 0.0,
    }
    bcfg = {"broker": creds, "market": {"exchange": config.get("market", {}).get("exchange", "NSE")}}

    from brokers.angelone import AngelOneBroker, BrokerLoginError  # noqa: F401
    broker = AngelOneBroker(bcfg)
    broker.connect()
    try:
        # SmartAPI exposes both `tradeBook` and `trade_book`; try in order.
        smart = broker.raw_api
        resp = None
        for attr in ("tradeBook", "trade_book"):
            fn = getattr(smart, attr, None)
            if fn:
                try:
                    resp = fn()
                except Exception:
                    resp = None
                if resp:
                    break
        if not resp or not resp.get("status"):
            return []
        data = resp.get("data") or []
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass

    legs: list[tuple[str, str, int]] = []
    for row in data:
        symbol = (row.get("tradingsymbol") or row.get("symbol") or "").upper()
        side   = (row.get("transactiontype") or row.get("side") or "").upper()
        try:
            qty = int(float(row.get("filledshares") or row.get("quantity") or 0))
        except (TypeError, ValueError):
            qty = 0
        # Defensive date filter (AngelOne returns today only, but the fill
        # timestamp format is "DD-Mon-YYYY HH:MM:SS"; we just trust it).
        ft = (row.get("filltime") or row.get("exchtime") or "")
        if day_iso != _today_iso() and day_iso not in str(ft):
            continue
        if symbol and side in ("BUY", "SELL") and qty > 0:
            legs.append((symbol, side, qty))
    return legs


def _aggregate(legs: list[tuple[str, str, int]]) -> dict[tuple[str, str], dict]:
    """Group legs by (symbol, side); return count + total_qty for each."""
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "qty": 0})
    for symbol, side, qty in legs:
        bucket = agg[(symbol, side)]
        bucket["count"] += 1
        bucket["qty"]   += qty
    return dict(agg)


def reconcile(
    *,
    db_path: Path,
    config_path: Path,
    day_iso: str,
    ignore_symbols: set[str],
) -> dict:
    """Return a reconciliation report dict and the legs from each source.

    Report shape:
        {
            "date": "2026-05-13",
            "db_legs":     int,
            "broker_legs": int,
            "buckets":     [{symbol, side, db_count, db_qty, broker_count,
                             broker_qty, ok}, ...]
            "mismatches":  int,
            "ok":          bool
        }
    """
    db_legs = _load_db_legs(db_path, day_iso)
    broker_legs = _broker_legs_for_day(config_path, day_iso)

    db_agg = _aggregate(db_legs)
    br_agg = _aggregate(broker_legs)

    keys = set(db_agg.keys()) | set(br_agg.keys())
    buckets = []
    mismatches = 0
    for key in sorted(keys):
        symbol, side = key
        if symbol in ignore_symbols:
            continue
        d = db_agg.get(key, {"count": 0, "qty": 0})
        b = br_agg.get(key, {"count": 0, "qty": 0})
        ok = d["count"] == b["count"] and d["qty"] == b["qty"]
        if not ok:
            mismatches += 1
        buckets.append({
            "symbol":      symbol,
            "side":        side,
            "db_count":    d["count"],
            "db_qty":      d["qty"],
            "broker_count": b["count"],
            "broker_qty":   b["qty"],
            "ok":          ok,
        })

    return {
        "date":        day_iso,
        "db_legs":     len(db_legs),
        "broker_legs": len(broker_legs),
        "buckets":     buckets,
        "mismatches":  mismatches,
        "ok":          mismatches == 0,
    }


def _format_report(report: dict) -> str:
    lines = []
    lines.append("=" * 76)
    lines.append(f"Trade book reconciliation -- {report['date']}")
    lines.append(f"DB legs: {report['db_legs']}    Broker legs: {report['broker_legs']}")
    lines.append("=" * 76)
    if not report["buckets"]:
        lines.append("(no trades to reconcile)")
    else:
        lines.append(f"{'SYMBOL':<14} {'SIDE':<5} "
                     f"{'DB#':>4} {'DB_qty':>7} "
                     f"{'BR#':>4} {'BR_qty':>7}  STATUS")
        lines.append("-" * 60)
        for b in report["buckets"]:
            status = "OK   " if b["ok"] else "DIFF "
            lines.append(
                f"{b['symbol']:<14} {b['side']:<5} "
                f"{b['db_count']:>4} {b['db_qty']:>7} "
                f"{b['broker_count']:>4} {b['broker_qty']:>7}  {status}"
            )
    lines.append("=" * 76)
    if report["ok"]:
        lines.append(f"VERDICT: OK -- all {len(report['buckets'])} (symbol,side) buckets match.")
    else:
        lines.append(f"VERDICT: {report['mismatches']} MISMATCH(ES) -- INVESTIGATE.")
    lines.append("=" * 76)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile SQLite trades vs AngelOne tradeBook for a given date.",
    )
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD date. Default: today (IST).")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH),
                    help=f"Path to SQLite DB. Default: {DEFAULT_DB_PATH}")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help=f"Path to config.yaml. Default: {DEFAULT_CONFIG}")
    ap.add_argument("--ignore-symbols", default="",
                    help="Comma-separated list of symbols to skip (e.g. for "
                         "open positions whose exit leg hasn't happened yet).")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON to stdout instead of the human report.")
    args = ap.parse_args()

    day_iso = args.date or _today_iso()
    ignore = {s.strip().upper() for s in args.ignore_symbols.split(",") if s.strip()}

    try:
        report = reconcile(
            db_path=Path(args.db),
            config_path=Path(args.config),
            day_iso=day_iso,
            ignore_symbols=ignore,
        )
    except FileNotFoundError as e:
        print(f"[reconcile] FATAL: {e}", file=sys.stderr)
        return 77
    except Exception as e:
        print(f"[reconcile] FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 99

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_report(report))

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
