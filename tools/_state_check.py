"""One-shot state inspector for the trading agent's current capital/position state."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "trading_agent.db"
c = sqlite3.connect(str(DB))
cur = c.cursor()

print("=== tables ===")
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print(tables)

# Try common state tables.
for t in ("portfolio_state", "agent_state", "capital_state", "state"):
    if t in tables:
        cur.execute(f"PRAGMA table_info({t})")
        cols = [r[1] for r in cur.fetchall()]
        cur.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 3")
        rows = cur.fetchall()
        print(f"\n=== {t} (cols={cols}, last 3 rows) ===")
        for r in rows:
            print(r)

# Trades summary.
print("\n=== trades summary ===")
cur.execute("SELECT COUNT(rowid), ROUND(SUM(pnl),2), MIN(entry_time), MAX(exit_time) FROM trades")
print("count, total_pnl, first_entry, last_exit:", cur.fetchone())

cur.execute("SELECT date(exit_time) AS d, COUNT(rowid), ROUND(SUM(pnl),2) FROM trades WHERE exit_time IS NOT NULL GROUP BY d ORDER BY d DESC LIMIT 10")
print("\nLast 10 trading days (date, n_trades, pnl):")
for r in cur.fetchall():
    print(" ", r)

c.close()
