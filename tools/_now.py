"""Quick live-state inspection used during market hours.

Just dumps current open positions + today-only closed trades. Avoids
PowerShell quoting headaches with `2026-05-07%` LIKE patterns.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytz

IST = pytz.timezone("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trading_agent.db"


def main() -> None:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    c = sqlite3.connect(DB)
    try:
        print(f"=== Now @ {datetime.now(IST).strftime('%H:%M:%S')} IST ===\n")

        rows = list(c.execute(
            "SELECT symbol, side, quantity, entry_price, stop_loss, "
            "take_profit, strategy, entry_time FROM open_positions"
        ))
        print(f"OPEN POSITIONS: {len(rows)}")
        if rows:
            print(f"  {'Symbol':<12} {'Side':<5} {'Qty':>4} {'Entry':>9} "
                  f"{'SL':>9} {'TP':>9}  Strategy        Opened")
            for sym, side, qty, ep, sl, tp, strat, et in rows:
                # Truncate entry_time to HH:MM for readability if it's today
                disp_time = et[11:16] if et[:10] == today else et[:16]
                print(f"  {sym:<12} {side:<5} {qty:>4} {ep:>9.2f} "
                      f"{(sl or 0):>9.2f} {(tp or 0):>9.2f}  "
                      f"{strat:<15} {disp_time}")

        print()
        rows = list(c.execute(
            "SELECT symbol, side, entry_price, exit_price, quantity, pnl, "
            "exit_reason, strategy, exit_time FROM trades "
            "WHERE exit_time LIKE ? ORDER BY exit_time",
            (f"{today}%",)
        ))
        print(f"TRADES CLOSED TODAY: {len(rows)}")
        if rows:
            total_pnl = sum((r[5] or 0) for r in rows)
            wins = sum(1 for r in rows if (r[5] or 0) > 0)
            losses = sum(1 for r in rows if (r[5] or 0) < 0)
            print(f"  {'Symbol':<12} {'Side':<5} {'Qty':>4} {'Entry':>9} "
                  f"{'Exit':>9} {'PnL':>9} {'Reason':<14}  Strategy")
            for sym, side, ep, xp, qty, pnl, reason, strat, xt in rows:
                print(f"  {sym:<12} {side:<5} {qty:>4} {ep:>9.2f} "
                      f"{xp:>9.2f} {(pnl or 0):>+9.2f} {reason:<14}  {strat}")
            print()
            print(f"  TOTAL: PnL={total_pnl:+,.2f}  W={wins}  L={losses}  "
                  f"WR={(wins/len(rows)*100):.0f}%" if rows else "")
    finally:
        c.close()


if __name__ == "__main__":
    main()
