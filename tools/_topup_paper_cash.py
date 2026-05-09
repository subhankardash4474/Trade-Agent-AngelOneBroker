"""
One-shot helper: top up paper-mode cash to a target value.

Strategy:
  1. Read current open positions from DB to compute their cost-basis notional.
  2. Insert a fresh equity_curve row at NOW with cash=target, equity=target+positions,
     positions=N. This row becomes the authoritative latest snapshot.
  3. On the supervisor's next daemon relaunch, `_resolve_continuity` reads my
     snapshot and Portfolio._restore_positions sees cash=target.

This avoids needing to restart the supervisor (which has the old run args cached)
or use the --reset-balance flag (which would re-fire on every supervisor relaunch).

Usage:
  python tools/_topup_paper_cash.py --target 100000

Run AFTER killing the current daemon (Stop-Process -Id <pid> -Force) and BEFORE
the supervisor relaunches it (~30s window). The script also computes the new
peak_balance correctly so drawdown math stays sane post-topup.
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trading_agent.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, required=True,
                    help="Target paper cash (Rs).")
    ap.add_argument("--db", type=str, default=str(DB_PATH))
    args = ap.parse_args()

    target = float(args.target)
    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    cur.execute(
        "SELECT symbol, side, entry_price, quantity FROM open_positions"
    )
    rows = cur.fetchall()
    n_pos = len(rows)
    notional = sum(float(price) * float(qty) for _sym, _side, price, qty in rows)
    target_equity = target + notional

    cur.execute(
        "SELECT MAX(equity) FROM equity_curve"
    )
    prev_peak_row = cur.fetchone()
    prev_peak = float(prev_peak_row[0]) if prev_peak_row and prev_peak_row[0] else 0.0
    new_peak = max(prev_peak, target_equity)

    ts_iso = datetime.now().isoformat(timespec="microseconds")

    print(f"=== Top-up paper cash to Rs {target:,.2f} ===")
    print(f"Open positions   : {n_pos}")
    for sym, side, price, qty in rows:
        print(f"  {sym:14s} {side:4s} {qty:>4} @ Rs {float(price):,.2f}")
    print(f"Position notional: Rs {notional:,.2f}")
    print(f"New target equity: Rs {target_equity:,.2f}")
    print(f"Previous peak    : Rs {prev_peak:,.2f}")
    print(f"New peak         : Rs {new_peak:,.2f}")
    print(f"Timestamp        : {ts_iso}")

    cur.execute(
        "INSERT INTO equity_curve (timestamp, equity, cash, positions) "
        "VALUES (?,?,?,?)",
        (ts_iso, target_equity, target, n_pos),
    )
    conn.commit()
    conn.close()

    print()
    print("[OK] equity_curve row inserted. The next daemon boot will read this")
    print("     as the latest snapshot and Portfolio.cash will become Rs",
          f"{target:,.2f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
