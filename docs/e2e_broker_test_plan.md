# End-to-End Broker Test Plan (LIVE)

**Status**: drafted 2026-05-10. Target execution window: weekend of
2026-05-17. Scope: 1 → 5 stocks, real AngelOne orders, ~Rs 1k–5k risk,
mechanical kill-switches.

---

## 0. Why we're doing this

Every strategy decision so far has been validated on paper. Paper trading
has known blind-spots that **only** real-broker execution surfaces:

- **Slippage realism**: paper assumes mid-price fill with a static
  slippage_bps. Real fills depend on book depth, time-of-day, ATM-side,
  and broker latency.
- **Order lifecycle quirks**: AngelOne SmartAPI surfaces order rejections
  for reasons paper never simulates -- circuit-limit hits, instrument
  freezes, RMS rejections, NRML/MIS mismatches, lot-size violations.
- **Auth + session lifecycle**: TOTP rotation, feed-token expiry, partial
  reconnects -- impossible to test without real credentials in the loop.
- **Position-state truth**: paper holds positions in our SQLite. Live
  has both our SQLite AND the broker's books, and they can drift.

**This is a controlled scaffolding exercise, not a money-making run.**
Capital is set so a complete blow-up loses < Rs 5,000 (lunch money). The
deliverable is *confidence*, not P&L.

---

## 1. Scaffolding stages (each gates the next)

### Stage 0 — Read-only auth (target: weekday evening before, ~30 min)
**Goal**: prove the AngelOne client can log in, fetch a quote, and gracefully
log out. No orders.

- [ ] `python tools/test_angelone_auth.py` (script TBD): login →
      `getProfile` → `getMarketData` for RELIANCE → logout.
- [ ] Confirm TOTP generation works from `ANGELONE_TOTP_SECRET`.
- [ ] Confirm `feed_token` is populated in the in-memory session.
- [ ] **Pass criterion**: zero exceptions, all four steps log successfully.
- [ ] **Fail action**: do NOT proceed. Fix auth before any order test.

### Stage 1 — Single AMO order, single stock (target: Saturday evening)
**Goal**: place ONE After-Market Order for ONE stock, verify it lands in
the broker's order book, then cancel it.

- [ ] Pick **single most liquid stock**: RELIANCE (HDFCBANK as backup).
- [ ] Capital allocated: Rs 1,000. Quantity: 1 share.
- [ ] Order type: **AMO LIMIT BUY at -10% of LTP** (deeply out-of-money,
      will not fill).
- [ ] Place via `core/broker/angelone.py:place_order(...)`.
- [ ] Verify order_id is returned and present in `orders` SQLite table.
- [ ] Verify the same order_id appears in `getOrderBook` from broker.
- [ ] **Cancel within 30 seconds** of placing.
- [ ] Verify both our DB and broker book show the cancellation.
- [ ] **Pass criterion**: place + cancel round-trip with consistent state.
- [ ] **Fail action**: stop. Don't proceed to live-hours stage until the
      lifecycle works on AMO.

### Stage 2 — Single live order, single stock (target: next Monday 09:30)
**Goal**: same flow as Stage 1 but during market hours. THIS is where
real fills can happen.

- [ ] Pre-flight: verify `EMERGENCY_STOP` file exists and is 0 bytes
      (kill-switch armed; remove ONLY when ready to launch).
- [ ] Stock: RELIANCE. Quantity: 1. Capital: ~Rs 3,000.
- [ ] Order type: **LIMIT BUY at LTP - 0.1%** (likely fills within 1-2
      ticks; not a market order, so we don't get hosed by spread).
- [ ] Time: 09:30 IST (15 min after open, past the worst opening volatility).
- [ ] After fill: hold for max **5 minutes**, then place LIMIT SELL at
      LTP + 0.1%.
- [ ] If sell doesn't fill within 5 more minutes: convert to MARKET SELL.
- [ ] **Hard cutoff**: full round-trip MUST complete before 09:50 IST.
      Set a wall-timer; if not flat by 09:50, force-flatten.
- [ ] **Pass criteria** (ALL must hold):
  - Both legs fill within target windows.
  - Our SQLite `trades` table reflects entry+exit with correct PnL.
  - Slippage observed (LTP-vs-fill diff) is within 5 bps of model
    assumption (15 bps).
  - No errors in `logs/agent.log` between order placement and exit.
- [ ] **Fail action**: pull the plug, flatten any open position via
      AngelOne web UI, postmortem before Stage 3.

### Stage 3 — 5 stocks, single round-trip each (target: weekend after Stage 2)
**Goal**: prove the daemon can manage a small basket without state drift.

- [ ] Universe: RELIANCE, HDFCBANK, INFY, TCS, ICICIBANK (Nifty top-5,
      max liquidity, low chance of weird circuits).
- [ ] Per-stock cap: Rs 1,000. Total capital: Rs 5,000.
- [ ] Use the regular `daemon` flow with overrides:
  - `--max-positions 5`
  - `--max-trades-per-day 5`
  - `--instruments RELIANCE,HDFCBANK,INFY,TCS,ICICIBANK`
  - `--mode live` (instead of paper)
- [ ] Run from 09:30 IST to 12:30 IST (3 hours, single trading session).
- [ ] **Mandatory daemon flags for this stage**:
  - `--max-loss-rs 500` (force-exit if cumulative P&L < -Rs 500)
  - `--single-shot` (one round-trip per stock; once exited don't
    re-enter — to be added if not already present)
- [ ] After session: full reconciliation between SQLite `trades` and
      broker `getTradeBook`. Any mismatch is a P0 issue.
- [ ] **Pass criteria**: 0 unreconciled trades, 0 untriggered SL/TP,
      max-loss kill-switch never needed (but verified in pre-flight).

### Stage 4 — Slow scaling (4-week incremental rollout)
Beyond Stage 3, expand by **stocks-per-week**, not by capital, until
either (a) we hit our intended live capital of Rs 30k or (b) we observe
a meaningful divergence between paper and live results that needs
investigation.

| Week | Stocks | Capital | Pass-gate before next week |
|---:|---:|---:|---|
| 1 | 5 | Rs 5k | Stage 3 above |
| 2 | 10 | Rs 10k | Reconciliation + slippage report match within 10 bps |
| 3 | 20 | Rs 20k | Same + 0 RMS rejections |
| 4 | 30 | Rs 30k | Stable for the full week, EOD diagnostic stays green |

---

## 2. Kill-switches (in order of fastest-to-trigger)

These exist BECAUSE Murphy's Law applies most aggressively to first-time
live trading. Each one independently can stop trading.

1. **EMERGENCY_STOP file**: `Path("EMERGENCY_STOP").exists()` → daemon
   refuses to place new orders, exits all open positions on next tick,
   then halts. **Already implemented** in current daemon. Pre-flight: file
   should exist with 0 bytes; remove right before "go", recreate the
   moment something feels off.
2. **Per-trade max-loss**: existing `stop_loss_pct` config. Already wired.
3. **Daily max-loss kill-switch**: `--max-loss-rs N` flag (TBD if not
   already present). If realised+unrealised P&L drops below -Rs N for
   the day, force-flatten and halt.
4. **Hard wall-clock cutoff**: every stage above has a hard "flatten by"
   timestamp. Set a separate alarm on the user's phone for each one as
   redundancy with the daemon's own timer.
5. **Manual web-UI override**: AngelOne web has a "square off all" button
   on the positions page. **User: bookmark it now.** Pre-test: practice
   logging in and locating that button; it should take < 30 seconds from
   browser tab to executed flatten.

---

## 3. Pre-flight checklist (run the morning of each stage)

Run from a clean PowerShell:

```powershell
cd "C:\Users\subhanda\OneDrive - AMDOCS\Documents\Trading Agent"

# 1. Daemon NOT running yet (we want a clean cold-start for live)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*run_daemon*' }
# Expected: empty. If something is running, kill it first.

# 2. EMERGENCY_STOP exists (kill-switch armed)
Test-Path EMERGENCY_STOP
# Expected: True

# 3. No open positions in DB
python -c "import sqlite3; c=sqlite3.connect('data/trading_agent.db'); print('open_positions:', c.execute('SELECT COUNT(*) FROM open_positions').fetchone()[0])"
# Expected: open_positions: 0

# 4. Config check
Select-String -Path config.yaml -Pattern "^\s+(name|mode|initial_balance):"
# Expected to see: mode: live, initial_balance: <stage-appropriate amount>

# 5. AngelOne credentials parse
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print('all set:', all(os.getenv(k) for k in ['ANGELONE_API_KEY','ANGELONE_API_SECRET','ANGELONE_CLIENT_ID','ANGELONE_PASSWORD','ANGELONE_TOTP_SECRET']))"
# Expected: all set: True

# 6. Phone alarm set for the stage's hard-cutoff time
# (manual)
```

If ANY of the above fails: ABORT. Do not edit config to make a check
pass — that's exactly the corner-cutting that kills traders.

---

## 4. Post-mortem (run after each stage, regardless of outcome)

Always run, even on green:

- [ ] `python packages/research/diagnostic.py --start <date> --end <date>`
      → check that the live-mode trades show up and PF/expectancy match
      paper-trading expectations.
- [ ] `python tools/post_mortem_analysis.py --run live --stage N` (TBD if
      not already implemented).
- [ ] Compare live-fill prices vs LTP-at-decision: compute realised
      slippage in bps, append to `data/slippage_log.csv`. After 20+ live
      trades we have enough samples to update the paper-mode
      `slippage_bps` config to match reality.
- [ ] Compare our SQLite `trades` count to broker `getTradeBook` count
      for the same window. Any mismatch is a P0.
- [ ] Write a 5-line summary to `docs/e2e_broker_test_log.md` (append-
      only journal, started fresh for this exercise).

---

## 5. Open implementation gaps (must close before Stage 0)

These are needed code/tooling pieces. Status as of 2026-05-11:

| Item | Status | Owner notes |
|---|---|---|
| `tools/test_angelone_auth.py` | **DONE** (2026-05-11) | 8-stage script: env -> import -> instantiate -> connect -> profile -> funds -> orders -> disconnect. Zero order mutations. `--dry-run` mode validates env without network. Dry-run 2/2 green; real run scheduled for tonight after market close. Diagnostics built in for AB1050 (IP whitelist) and AB1007 (TOTP drift). |
| `core/broker/angelone.py:place_order` | Done in Phase 1 (verify) | Spot-check signature accepts AMO + LIMIT + lot=1. |
| `core/broker/angelone.py:cancel_order` | TBD - verify | Required for Stage 1. |
| `--max-loss-rs N` daemon flag | TBD | Probably 30 lines in `trading_agent.py`. |
| `--single-shot` daemon flag | TBD | Required for Stage 3. |
| `EMERGENCY_STOP` flatten-on-trigger | Probably done | Needs explicit verification: when file appears mid-session, does the daemon flatten existing positions, or only refuse new ones? Stage 0 sub-test. |
| Slippage logger | TBD | Append (LTP_at_decision, fill_price, bps_diff) to `data/slippage_log.csv` on every live fill. |
| `getTradeBook` reconciliation | TBD | Post-mortem helper that diffs SQLite `trades` vs broker. |

**Estimated work to close all gaps**: ~1.5 days. Realistic plan:
- Sat morning: knock out auth script + cancel_order + max-loss flag (~3h).
- Sat afternoon: Stage 0 + Stage 1 (auth + AMO test) (~2h, mostly waiting).
- Sun morning: pre-flight + Stage 2 reconciliation harness + slippage
  logger (~3h).
- Sun morning at 09:30 IST: Stage 2 live single-stock test.

---

## 6. What "success" looks like at the end of this exercise

After 4 weeks of incremental scaling we should be able to say:

- **Quantitative**: realised slippage matches model within 10 bps median,
  zero unreconciled trades over 50+ live round-trips, daemon uptime > 95%
  during market hours.
- **Qualitative**: we trust the live path enough to scale to Rs 30k AND
  we have a well-rehearsed kill-switch sequence we can execute in < 30
  seconds when something looks off.

If we don't have BOTH after 4 weeks, we don't scale further -- we step
back, look at the diff between paper and live, and fix the system before
risking more capital. That's the whole point of the scaffolding.

---

*Updated: 2026-05-10. Next review: at start of each stage; record
outcomes in `docs/e2e_broker_test_log.md` (to be created).*
