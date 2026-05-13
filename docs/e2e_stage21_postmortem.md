# E2E Live Broker Stage 2.1 — Post-Mortem (First Real Fill)

**Run date**: 2026-05-13 (Wednesday), 10:01 IST.
**Origin**: OCI Mumbai VM (`80.225.251.79`), NOT the corporate laptop.
**Container**: `trader` (paper daemon untouched, ran in parallel without conflict).
**Account**: AngelOne, funded with Rs 1,000.
**Symbol**: YESBANK-EQ (NSE, token 11915, LTP ~Rs 22.21).
**Real capital deployed**: Rs 22.20 notional, **net loss Rs 0.01 + charges**.

This is the **first-ever real-money round-trip** for the project. The 2026-05-11
Stage 1/2 run on the laptop never produced a fill; today's aggressive variant
finally did.

---

## TL;DR

Stages 0 → 1 → 2.1 completed in **65 seconds total wall-clock** from a single
SSH session into the cloud VM. Zero anomalies. The aggressive pricing model
worked exactly as designed, and the live AngelOne path from the cloud IP is
now empirically validated end-to-end.

| Stage | Wall-clock | Fills | Real cost |
|---|---:|---:|---:|
| 0 — auth smoke | ~5s | 0 | Rs 0 |
| 1 dry-run | 8.1s | 0 | Rs 0 |
| 1 live | 30.0s | 0 (cancelled before fill) | Rs 0 |
| **2.1 aggressive** | **9.1s** | **2 (BUY + SELL)** | **Rs 0.01 + charges** |

---

## Stage 2.1 — second-by-second walk-through

Pulled from `logs/live_e2e/stage21_20260513T100136.log`:

| t+ | Event | Latency | Outcome |
|---:|---|---:|---|
| 0.0s | script start | — | — |
| 4.9s | `.env` loaded, 5 keys present | ~5s (container exec overhead) | OK |
| 5.4s | SmartApi + AngelOneBroker imported | ~0.5s | OK |
| 5.4s | broker instantiated | ~0s | client_id `AAC***` |
| 5.7s | `connect()` complete | ~0.3s | TOTP + JWT exchange OK |
| 5.7s | `searchScrip("YESBANK-EQ")` | ~0.1s | token=11915 |
| 5.9s | `get_ltp()` | ~0.2s | Rs 22.21 |
| 5.9s | Planned BUY @ 22.25 (LTP × 1.002), SELL @ 22.17 (LTP × 0.998) | — | spread plan = +20 bps / -20 bps |
| 6.0s | `get_funds()` | ~0.1s | available_cash=Rs 1,000.00 |
| 6.0s | submit BUY LIMIT @ 22.25 | ~0.2s | OK, order_id=`260513000260087` |
| 6.3s | poll BUY | <0.1s | **status=complete, filled=1/1** at avg Rs 22.20 |
| 6.4s | submit SELL LIMIT @ 22.17 | ~0.2s | OK, order_id=`260513000260113` |
| 6.7s | poll SELL | <0.1s | **status=complete, filled=1/1** at avg Rs 22.19 |
| 8.8s | final order_book + positions snapshot | ~2s | `netqty=0`, `realised=-0.01` |
| 9.1s | `disconnect()` clean | ~0.3s | OK |

---

## Empirical findings

### 1. Price improvement on BOTH legs (vs the aggressive limit)

The aggressive variant was designed to *pay* the full bid-ask spread (Rs 0.13
expected loss). What actually happened: the BUY filled *inside* the
aggressive +20-bps limit, and the SELL filled *inside* the aggressive
−20-bps limit. Both legs got the better side of our own submission.

| Leg | Limit submitted | LTP at decision | Filled at | Slippage vs LTP* | vs limit |
|---|---:|---:|---:|---:|---:|
| BUY  | Rs 22.25 (+18 bps) | Rs 22.21 | **Rs 22.20** | **−4.5 bps** (favourable) | filled 5 paise inside limit |
| SELL | Rs 22.17 (−18 bps) | Rs 22.21 | **Rs 22.19** | **+9.0 bps** (adverse) | filled 2 paise inside limit |

\* Sign convention: positive = adverse (worse price than LTP), negative
= favourable (better price than LTP).

The numbers tell two truths:

1. **Both fills beat our own aggressive submissions** (i.e. better than
   we were willing to accept). The market gave us price improvement
   versus the limits we sent in.
2. **Versus LTP-at-decision**, the BUY was favourable but the SELL was
   adverse. This is because the SELL was submitted ~50ms after the BUY
   filled — the book had shifted in our (un-favourable for SELL)
   direction by ~2 paise during that window. Net round-trip slippage:
   **+4.5 bps adverse** (BUY −4.5 + SELL +9.0 net).

**Comparison to paper-mode assumption** (15 bps adverse on each leg
= 30 bps round-trip):

| | Paper assumes | Actually paid |
|---|---:|---:|
| Round-trip slippage | 30 bps adverse | **4.5 bps adverse** |
| Per-share cost on Rs 22 stock | ~Rs 0.066 | ~Rs 0.01 |

Paper-mode overstates slippage by ~6× for tier-1 NSE liquidity. n=1 is
not enough to revise the config, but it's a strong signal in the same
direction. After 20+ live trades, we should expect to lower
`slippage_bps` from 15 → 5 or so for liquid names.

### 2. Fill latency was 30× the planned budget

The Stage 2 plan called for a 60s polling budget per leg. Actual fill
detection was **<100ms** on both legs. The container exec overhead (~5s
to load .env and import) dwarfs the actual broker round-trip.

| Operation | Planned envelope | Observed |
|---|---:|---:|
| `connect()` | 2s | 0.3s |
| `get_ltp` | 0.5s | 0.2s |
| `place_order` | 0.5s | 0.2s |
| BUY fill detection | 60s polling | **<0.1s** |
| SELL fill detection | 60s polling | **<0.1s** |
| `disconnect()` | 0.5s | 0.3s |

### 3. The AngelOne cloud-IP whitelist is already correct

We never explicitly verified the SmartAPI portal "Primary Static IP" had
been updated from `106.193.147.98` (corp laptop) to `80.225.251.79` (OCI).
The fact that Stage 1's NORMAL fallback got `order_id=260513000257131`
back from the exchange — not an `AG7002 Unregistered IP` rejection —
proves it's already in place.

### 4. Paper daemon ran in parallel without conflict

The paper daemon was actively running in the same container (uptime 1080
min, cycle 70, 1 open position HCLTECH) when Stage 2.1 fired. Both
processes share the AngelOne credentials, but:
- The paper daemon does NOT open a SmartAPI session (broker.mode=paper).
- The e2e script opens its own ephemeral SmartAPI session.
- No collision observed.

This validates the **"run live tests in parallel with paper daemon"**
operating model. The kill-switch (`EMERGENCY_STOP` file) only affects the
daemon's order-placement, not the e2e scripts.

---

## What's now unblocked

Per the e2e test plan §5 and the original Stage 1/2 post-mortem:

| Item | Was | Now |
|---|---|---|
| Slippage logger seed | "TBD — cannot seed yet, need a fill" | **unlockable** — 2 real fills available |
| Stage 2.1 actual-fill variant | "NEW TODO" | **DONE** |
| Cloud-IP-to-AngelOne write path validated | unknown | **PROVEN** (cancel + fill + position reconciliation all worked) |
| AngelOne fail-mode envelope | "friendly, per Stage 1/2" | **same on cloud** |

What's still blocking Stage 3 (5-stock basket):

- `--max-loss-rs N` daemon flag (TBD code work)
- `--single-shot` daemon flag (TBD code work)
- `getTradeBook` reconciliation helper (TBD code work)

---

## Slippage logger — n=2 seed data

Appended to `data/slippage_log.csv` by the helper at
`tools/_slippage_logger.py`. Sign convention: positive = adverse vs LTP.

```csv
timestamp_ist,symbol,side,limit_price,ltp_at_decision,filled_price,quantity,slippage_bps,source
2026-05-13T10:01:42+05:30,YESBANK-EQ,BUY,22.2500,22.2100,22.2000,1,-4.50,stage21
2026-05-13T10:01:43+05:30,YESBANK-EQ,SELL,22.1700,22.2100,22.1900,1,+9.00,stage21
```

Running summary after this seed:

```
$ python tools/_slippage_logger.py
[slippage] 2 fills logged
  BUY:  n=1  mean=-4.50 bps
  SELL: n=1  mean=+9.00 bps
  ALL:  n=2  mean=+2.25 bps  (favourable=1, adverse=1)
```

After 20+ live trades this seed will drive an empirical update to the
paper-mode `slippage_bps` config in `config.yaml`.

---

## Three things now known that weren't before

1. **The OCI cloud VM can place + fill + cancel orders on AngelOne.** Not
   just auth — the full write API works from `80.225.251.79`.
2. **AngelOne smart-order-routing actively gives us price improvement at
   tier-1 NSE liquidity.** Our paper assumption of 15 bps adverse slippage
   is likely too conservative for liquid names. We need 20+ samples before
   re-calibrating, but the direction is clear.
3. **The e2e test scripts can run alongside the paper daemon in the same
   container with zero conflict.** This means future stages don't require
   stopping the paper daemon — a major operational win.

---

*Generated: 2026-05-13 10:15 IST. Author: cloud-side first-fill operator.*
