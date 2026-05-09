"""One-shot: insert the missing ZYDUSWELL protective close into the trades
table. The Portfolio.close_position() removed it from open_positions and
updated cash correctly, but it never got persisted to the `trades` SQL table
because my _protective_close.py script forgot to call db.store_trade().
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.database import Database


def main() -> None:
    db = Database("data/trading_agent.db")

    trade = {
        "symbol": "ZYDUSWELL",
        "side": "SELL",
        "entry_price": 509.25,
        "exit_price": 505.45,
        "quantity": 15,
        "entry_time": "2026-05-06T11:39:00",
        "exit_time": "2026-05-07T10:23:28",
        "pnl": 48.92,
        "pnl_pct": 0.64,
        "strategy": "mean_reversion",
        "exit_reason": "manual_protective_trend",
        "commission": 8.08,
        "slippage": 0,
        "market_context": "manual close - +12.4% above 50d SMA, against trend, near SL",
    }

    # Idempotency check
    conn = sqlite3.connect("data/trading_agent.db")
    try:
        existing = conn.execute(
            "SELECT COUNT(1) FROM trades WHERE symbol=? AND exit_time=?",
            (trade["symbol"], trade["exit_time"]),
        ).fetchone()[0]
        if existing:
            print(f"ZYDUSWELL trade already exists at {trade['exit_time']} "
                  f"(count={existing}). Skipping insert.")
            return
    finally:
        conn.close()

    db.store_trade(trade)
    print(f"Inserted ZYDUSWELL trade: PnL +{trade['pnl']:.2f}")

    conn = sqlite3.connect("data/trading_agent.db")
    try:
        rows = conn.execute(
            "SELECT symbol, side, exit_price, pnl, exit_reason "
            "FROM trades WHERE exit_time LIKE '2026-05-07%' ORDER BY exit_time"
        ).fetchall()
        print()
        print("Today's trades after backfill:")
        total = 0.0
        for r in rows:
            print(f"  {r[0]:<12} {r[1]:<5} exit={r[2]:>9.2f} pnl={r[3]:+.2f} reason={r[4]}")
            total += r[3] or 0
        print(f"  TOTAL: {total:+.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
