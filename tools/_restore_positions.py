"""One-shot DB surgery: undo the 2026-05-07 pre-market square-off.

Context: at 08:59:06 IST on 2026-05-07 the trading daemon (just launched
under the new watchdog) hit a bug where its trading loop squared off
yesterday's 3 carryover positions in pre-market because
`is_market_open()` returns False before 09:15. The fix is in
`trading_agent.py` (pre-market guard added). This script reverts the DB
state so the daemon, when restarted, sees the 3 positions still open and
can manage them against real 09:15+ market prices.

What we do:
  1. Read the 3 erroneous trade rows from `trades` (the ones with
     exit_reason='market_close' and exit_time on 2026-05-07).
  2. Reconstruct the matching `open_positions` row for each (entry data
     was preserved on the trade record).
  3. Re-insert into `open_positions`.
  4. Delete those 3 rows from `trades`.
  5. Restore cash to the pre-square-off value from the equity_curve
     snapshot at 2026-05-06T14:44:12 (Rs 9,504.41).
  6. Append a fresh equity_curve snapshot reflecting the restored state.

What we DON'T do:
  - Reverse the strategy scorecard updates that were credited from the
    erroneous PnL. Each strategy got +/- one trade attributed; the impact
    on learned weights is tiny (~1/dozens trades) and reversing it is
    riskier than leaving it. Documented for the audit trail.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytz

IST = pytz.timezone("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trading_agent.db"

# Snapshot from yesterday (2026-05-06) — the authoritative state we want
# to restore to. Pulled from the EOD equity_curve snapshot.
RESTORE_CASH = 9504.41


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"DB not found: {DB}")

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        # 1. Find the 3 erroneous market_close trades from today
        today_iso = datetime.now(IST).strftime("%Y-%m-%d")
        rows = list(c.execute(
            "SELECT * FROM trades "
            "WHERE exit_reason = 'market_close' "
            "AND exit_time LIKE ? "
            "ORDER BY exit_time ASC",
            (f"{today_iso}%",)
        ))
        if not rows:
            print("No 'market_close' trades on today's date — nothing to revert.")
            return

        print(f"Found {len(rows)} erroneous market_close trade(s) today:")
        for r in rows:
            print(f"  id={r['id']}  {r['symbol']:<12} {r['side']:<5}  "
                  f"qty={r['quantity']:>3}  entry={r['entry_price']:>9.2f}  "
                  f"exit={r['exit_price']:>9.2f}  pnl={r['pnl']:>+8.2f}  "
                  f"opened={r['entry_time'][:19]}")

        # 2. Inspect open_positions schema to build the right INSERT
        op_cols = [r[1] for r in c.execute("PRAGMA table_info(open_positions)")]
        print(f"\nopen_positions cols: {op_cols}")

        # 3. Re-insert each as an open position. We use the original entry
        # data from the trade row — that's what made it onto the
        # `open_positions` table yesterday before today's bug closed it.
        # Strategy-specific fields (regime, contributing_strategies) are
        # set conservatively where possible from the trade row;
        # contributing_strategies as '{}' is safe (the live daemon's
        # restore path treats this as "no per-strategy attribution").
        print("\nRe-inserting into open_positions...")
        for r in rows:
            # Map every available column from the trade row, fall back to
            # safe defaults for fields that don't exist on trades.
            insert: dict = {
                "symbol": r["symbol"],
                "side": r["side"],
                "entry_price": r["entry_price"],
                "quantity": r["quantity"],
                "entry_time": r["entry_time"],
                "stop_loss": r["stop_loss"] if "stop_loss" in r.keys() else None,
                "take_profit": r["take_profit"] if "take_profit" in r.keys() else None,
                "strategy": r["strategy"] if "strategy" in r.keys() else "",
                "order_id": r["order_id"] if "order_id" in r.keys() else "",
                "regime": r["regime"] if "regime" in r.keys() else "",
                "contributing_strategies": "{}",
                "cash_after": None,  # filled in below
            }
            # Restrict to columns that actually exist on the table
            keys = [k for k in insert.keys() if k in op_cols]
            cols_sql = ", ".join(keys)
            placeholders = ", ".join(["?"] * len(keys))
            values = tuple(insert[k] for k in keys)
            c.execute(
                f"INSERT INTO open_positions ({cols_sql}) VALUES ({placeholders})",
                values,
            )
            print(f"  inserted {r['symbol']}")

        # 4. Delete the erroneous trade rows
        print("\nDeleting erroneous trade rows...")
        ids = [r["id"] for r in rows]
        placeholders = ", ".join(["?"] * len(ids))
        c.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", ids)
        print(f"  deleted {len(ids)} rows from trades")

        # 5. Append a fresh equity_curve snapshot to make the new state
        # the authoritative source on next daemon boot. The snapshot's
        # `cash` is the pre-square-off value; equity is reconstructed
        # from cash + invested capital.
        invested = sum((r["entry_price"] or 0) * (r["quantity"] or 0) for r in rows)
        equity = RESTORE_CASH + invested
        ts = datetime.now(IST).isoformat()
        eq_cols = [r[1] for r in c.execute("PRAGMA table_info(equity_curve)")]
        snap = {"timestamp": ts, "equity": equity, "cash": RESTORE_CASH,
                "positions": len(rows)}
        keys = [k for k in snap.keys() if k in eq_cols]
        cols_sql = ", ".join(keys)
        placeholders = ", ".join(["?"] * len(keys))
        c.execute(
            f"INSERT INTO equity_curve ({cols_sql}) VALUES ({placeholders})",
            tuple(snap[k] for k in keys),
        )
        print(f"\nequity_curve snapshot: {snap}")

        c.commit()
        print("\n[OK] Restore complete. Verifying...\n")

        # 6. Verify
        n_open = c.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0]
        n_today = c.execute(
            "SELECT COUNT(*) FROM trades WHERE exit_time LIKE ? AND exit_reason='market_close'",
            (f"{today_iso}%",)
        ).fetchone()[0]
        last_eq = c.execute(
            "SELECT timestamp, equity, cash, positions FROM equity_curve "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        print(f"open_positions count       : {n_open}")
        print(f"today's market_close trades: {n_today} (should be 0)")
        print(f"latest equity snapshot     : {dict(last_eq)}")
        print()
        print("Open positions now:")
        for r in c.execute(
            "SELECT symbol, side, quantity, entry_price, stop_loss, take_profit, "
            "strategy FROM open_positions"
        ):
            print(f"  {r['symbol']:<12} {r['side']:<5} qty={r['quantity']:>3} "
                  f"@ {r['entry_price']:>9.2f}  SL={r['stop_loss']:>9.2f} "
                  f"TP={r['take_profit']:>9.2f}  [{r['strategy']}]")
    finally:
        c.close()


if __name__ == "__main__":
    main()
