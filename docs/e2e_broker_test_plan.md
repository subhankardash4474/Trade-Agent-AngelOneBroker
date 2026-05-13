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

### Stage 1 — Single order place + cancel ✅ **DONE 2026-05-11 10:54 IST**
**Goal**: place ONE order for ONE stock, verify it lands in the broker's
order book, then cancel it.

**Result**: PASSED in 29s. order_id=`260511000368479`. Zero rupees
spent. Full post-mortem at [`docs/e2e_stage12_postmortem.md`](e2e_stage12_postmortem.md).

Key findings (now baked into the codebase):
- AngelOne SmartAPI rejects `variety="AMO"` (AB1007). Valid varieties
  are only NORMAL / STOPLOSS / ROBO. The original plan to use AMO as
  safety was based on a false premise; NORMAL with a deep-OOM LIMIT
  achieves the same risk profile.
- NSE intraday circuit-limit bands tightened to ~5% (not 10% EOD).
  Our -10% OOM LIMIT was rejected as "exceeds circuit limit". For
  lifecycle tests this is fine (rejection is a valid cancellable
  outcome); for production strategies the LIMIT price must be clamped
  inside the daily circuit band.

Final test parameters used:

- [x] **Symbol**: YESBANK-EQ (~Rs 22.80, top-50 NSE liquidity, fits
      Rs 1,000 budget with massive headroom).
- [x] **Quantity**: 1 share.
- [x] **Order type**: LIMIT BUY at LTP * 0.90.
- [x] **Variety**: NORMAL (after AMO returned AB1007).
- [x] **Tooling**: `python tools/test_amo_lifecycle.py --confirm`.
- [x] **order_id captured**: `260511000368479`.
- [x] **Cancel ACKed** within ~26s of placement.
- [x] **Final order_book status** = rejected (NSE circuit) -> cancelled-by-us.
- [x] **PASS**: place + cancel round-trip exercised end-to-end. ₹0 cost.

### Stage 2 — Single live BUY+SELL round-trip ✅ **DONE 2026-05-11 10:59 IST (laptop) + 2026-05-13 10:01 IST (cloud)**
**Goal**: same flow as Stage 1 but during market hours. THIS is where
real fills can happen.

**Result**: PASSED in 69s. order_id=`260511000380328`. BUY LIMIT did
not fill (limit below best bid) → cleanly cancelled → no exposure.
Zero rupees spent. Full post-mortem at
[`docs/e2e_stage12_postmortem.md`](e2e_stage12_postmortem.md).

What got tested vs not (patient variant, laptop):
- ✅ place_order during market hours (NORMAL/LIMIT/BUY/DELIVERY/1share)
- ✅ Order accepted by exchange (no circuit issue this time)
- ✅ Order book status polling, multiple snapshots over 60s
- ✅ Buy-fill-timeout → cancel branch
- ✅ "No exposure -- clean exit" decision logic
- ❌ Fill detection (no counterparty matched our LIMIT at LTP-0.1%)
- ❌ SELL LIMIT path (never armed, no position to exit)
- ❌ MARKET SELL escalation (never armed)
- ❌ Slippage measurement (no fill to measure)

### Stage 2.1 — Aggressive variant, first real fill ✅ **DONE 2026-05-13 10:01 IST (cloud, FIRST EVER REAL FILL)**

**Result**: PASSED in 9.1s. BUY filled at Rs 22.20, SELL filled at Rs 22.19.
Net realised PnL **Rs −0.01** (one paisa) plus ~Rs 1-3 in regulatory
charges (STT/exchange/GST/stamp). Full post-mortem at
[`docs/e2e_stage21_postmortem.md`](e2e_stage21_postmortem.md).

What this proved (and what's still open):
- ✅ Fill detection (both legs filled <100ms after submission)
- ✅ SELL LIMIT path (armed and filled in 0.1s)
- ✅ Position reconciliation via `getPositions` (`netqty=0`)
- ✅ Cloud-IP → AngelOne write path (no `AG7002` — whitelist already
      updated to `80.225.251.79`)
- ✅ Paper daemon coexists with e2e script in same container (no conflict)
- ✅ Slippage measurement: **price improvement on both legs**
      (−4.5 bps BUY, −9.0 bps SELL vs LTP). Opposite of paper-mode
      assumption of 15 bps adverse. n=1 — need ≥20 samples to recalibrate.
- ❌ MARKET SELL escalation (still never armed — would need a forced
      timeout scenario)
- ❌ Slippage logger (`data/slippage_log.csv`) auto-write — still TBD

Key empirical learning: at tier-1 NSE liquidity (YESBANK), even
aggressive LIMITs get matched at the inside book mid, not at the
aggressive limit. Paper-mode `slippage_bps` is likely too conservative
for liquid names — confirm after 20+ samples.

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
3. **Daily max-loss kill-switch**: `--max-loss-rs N` flag (added
   2026-05-13). If realised P&L drops below -Rs N for the day, the
   risk manager refuses all new trades and surfaces the breaker reason
   in the next checkpoint. Existing positions still receive SL/TP
   management via `_check_position_exits` — the flag halts *new
   entries*, it does not auto-flatten.
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

# 6. AngelOne SmartAPI Primary Static IP whitelisted for ORDER APIs
#    (Stage 0 auth bypasses this gate; first placeOrder will hit it.)
#    Confirm by visiting https://smartapi.angelbroking.com/ -> My Apps ->
#    Edit App -> Primary Static IP. Must match the laptop's current
#    public IP (run `curl https://api.ipify.org` to see it).
# (manual)

# 7. Phone alarm set for the stage's hard-cutoff time
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
| `tools/test_angelone_auth.py` | **DONE** (2026-05-11) | 8-stage script: env -> import -> instantiate -> connect -> profile -> funds -> orders -> disconnect. Zero order mutations. `--dry-run` mode validates env without network. Dry-run 2/2 green; real run executed live: 8/8 PASS, Rs 1,000 confirmed. |
| `tools/test_amo_lifecycle.py` | **DONE** (2026-05-11 laptop + 2026-05-13 cloud) | Stage 1 script: env -> import -> instantiate -> connect -> resolve token -> LTP -> funds preflight -> place_order (AMO+NORMAL fallback) -> 20s wait -> cancel -> verify. Default dry-run; `--confirm` flag for live order. Cloud re-run 2026-05-13: PASS in 30s, order_id `260513000257131` cancelled cleanly. |
| **AngelOne *Primary Static IP* whitelisted for order APIs** | **RESOLVED** (2026-05-13) | Previously blocked Stage 1 (2026-05-11 10:17) with `AG7002: Access denied: Unregistered IP address` for laptop IP `106.193.147.98`. Now updated to OCI Mumbai IP `80.225.251.79`; confirmed working by Stage 1 + Stage 2.1 cloud runs on 2026-05-13. |
| `core/broker/angelone.py:place_order` | **VERIFIED** correct (2026-05-11 laptop + 2026-05-13 cloud) | Signature accepts AMO + LIMIT + lot=1. Filled BUY + SELL on cloud at 22.20 / 22.19 (Stage 2.1). |
| `core/broker/angelone.py:cancel_order` | **VERIFIED** (2026-05-11 + 2026-05-13) | Stage 1 lifecycle exercised it cleanly both runs. |
| `searchScrip` based token resolution | **DONE** in `test_amo_lifecycle.py` | Replaces hardcoded tokens. Resilient to instrument-master updates. |
| `--max-loss-rs N` daemon flag | **DONE** (2026-05-13) | Added to `run_daemon.py` argparse + `RiskManager.can_trade()` checks an absolute floor in addition to the existing % check. Required for Stage 3 basket. |
| `--single-shot` daemon flag | **DONE** (2026-05-13) | Added to `run_daemon.py` argparse; `TradingAgent` tracks per-symbol round-trips for the day and refuses re-entry once a symbol has closed. Required for Stage 3. |
| `EMERGENCY_STOP` flatten-on-trigger | Probably done | Needs explicit verification: when file appears mid-session, does the daemon flatten existing positions, or only refuse new ones? Stage 0 sub-test. |
| Slippage logger | **DONE** (2026-05-13) | `data/slippage_log.csv` schema + `tools/_slippage_logger.py` helper; auto-writes from `tools/test_live_single_trade.py` on every fill. Seed: Stage 2.1 fills (n=2, both favourable −4.5 / −9.0 bps). |
| `getTradeBook` reconciliation | **DONE** (2026-05-13) | `tools/reconcile_trade_book.py` diffs SQLite `trades` rows vs broker `getTradeBook` for any date. |

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
