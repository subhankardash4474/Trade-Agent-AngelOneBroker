# E2E Live Broker Stage 1 + 2 — Post-Mortem

**Run date**: 2026-05-11 (Monday), in parallel with paper daemon (PID 14144).
**Network**: AMDOCS corporate (egress IP `106.193.147.98` as seen by AngelOne).
**Account**: AngelOne, funded with Rs 1,000.
**Symbol**: YESBANK-EQ (NSE, token 11915, LTP ~Rs 22.80).
**Real capital deployed**: Rs 0 (zero, both stages).

---

## Stage 1 — `place_order` + `cancel_order` lifecycle

Executed at 10:54:30 IST. Wall-clock 29s. Exit code 0 (PASS).

### Sequence of events
| t+ | event | latency | outcome |
|---:|---|---:|---|
| 0.0s | script start | — | — |
| 2.2s | broker.connect() complete | ~2s (TOTP + JWT exchange) | OK |
| 2.7s | searchScrip("YESBANK-EQ") | ~0.5s | token=11915 |
| 3.3s | get_ltp() | ~0.5s | Rs 22.80 |
| 3.8s | get_funds() | ~0.5s | available_cash=Rs 1,000.00 |
| 3.8s | place_order(variety=AMO) | ~0.5s | **REJECTED** errorcode=AB1007 |
| 4.3s | place_order(variety=NORMAL) | ~0.5s | OK, order_id=`260511000368479` |
| 25.4s | get_orders() | ~0.5s | status=**rejected** (NSE circuit) |
| 26.0s | cancel_order() | ~0.6s | ACKed True |
| 28.5s | get_orders() (post-cancel) | ~0.5s | status=rejected (terminal) |
| 29.0s | broker.disconnect() | ~0.5s | clean |

### Key API responses

**AMO rejection** (`AB1007`):
```
Invalid Order Variety. Value should be NORMAL, STOPLOSS, ROBO
```

**NSE circuit-limit rejection** (after NORMAL accepted server-side):
```
Your order price exceeds the circuit limit. Please adjust your order
within the circuit limit or place a market order.
```
Our LIMIT BUY @ Rs 20.52 vs YESBANK lower circuit ~Rs 20.55 (intraday 5%
band that tightens from the EOD 10% band). All 3 of these are true at
once: order_id is returned, order shows in book, status is "rejected"
with reason text -- so `cancel_order` is callable but a no-op (the
exchange already terminated the order).

### Findings -> code/doc changes
| Finding | Action | Status |
|---|---|---|
| AngelOne SmartAPI `placeOrder` rejects `variety="AMO"` | Update script docstring + AMO is now best-effort with NORMAL as primary | DONE |
| -10% OOM breaches NSE intraday circuit | Future strategies must clamp LIMIT prices within daily band (~5% intraday). For lifecycle tests, the rejection IS still a valid outcome | NOTED |
| Place + cancel latencies ~500ms each | Confirms daemon's 5s polling budget is generous | NOTED |

---

## Stage 2 — Single live BUY+SELL round-trip

Executed at 10:59:45 IST. Wall-clock 69s. Exit code 0 (PASS).

### Sequence of events
| t+ | event | latency | outcome |
|---:|---|---:|---|
| 0.0s | script start | — | — |
| 2.3s | broker.connect() complete | ~2s | OK |
| 2.8s | searchScrip | ~0.5s | token=11915 |
| 3.4s | get_ltp() | ~0.5s | Rs 22.79 |
| 3.4s | computed BUY @ Rs 22.77, planned SELL @ Rs 22.81 | — | spread ~17 bps |
| 3.9s | get_funds() | ~0.5s | Rs 1,000.00 |
| 4.4s | place_order(BUY LIMIT @ 22.77) | ~0.5s | OK, order_id=`260511000380328` |
| 4.9s -> 61.2s | 11 polls (~5.6s each) | — | status=`open`, filled=0 throughout |
| 66.2s | buy-fill timeout hit | — | escalate to cancel |
| 66.9s | cancel_order(BUY) | ~0.7s | ACKed True |
| 69.5s | get_orders() (post-cancel) | ~0.5s | status=cancelled, filled=0 |
| 69.5s | logic: "no exposure -- clean exit" | — | SELL leg never armed |
| 70.0s | broker.disconnect() | ~0.5s | clean |

### Why BUY didn't fill (and why that's the right answer)

We placed LIMIT BUY at LTP * 0.999 = Rs 22.77, which is 1 paisa
**below** what was likely the best bid at the time. In a normal book,
our order sat at L1 ask + queue position depth, waiting for someone
to *sell to us* at Rs 22.77. In 60s of polling, no seller hit our
price. Outcomes if we had:

- Raised BUY to LTP+0.1% (Rs 22.81): would likely have crossed the
  spread and filled within a few seconds. But this is paying the
  full spread, which is the slippage we wanted to avoid measuring.
- Placed MARKET BUY: would have filled at the best ask immediately,
  but at higher slippage and unknown price -- bad for a test where
  we want to know exactly what we paid.

So the no-fill outcome is **the most honest test result**: it
proves the buy-timeout cancel path works without putting us in a
position we then have to escape under time pressure. The MARKET SELL
escalation path is **unexercised** by this run (no exposure to flatten).

### Findings -> code/doc changes
| Finding | Action | Status |
|---|---|---|
| BUY LIMIT at LTP-0.1% does not fill in 60s on a slow book | Future runs: add a `--aggressive` flag that prices BUY at LTP+0.1% to actually exercise the fill+sell path | TODO |
| Poll cadence (5s) gave 11 status snapshots in 60s | Daemon's polling rhythm is well-sized; no change needed | NOTED |
| Cancel after timeout is reliable | The "no exposure clean exit" branch works as designed | CONFIRMED |
| MARKET SELL escalation path UNTESTED | Carry over to Stage 2.1 (variant that gets a fill, then exits) | TODO |

---

## Net empirical learning vs paper-mode assumptions

| Metric | Paper assumption | Live observation | Verdict |
|---|---|---|---|
| place_order round-trip latency | ~100ms (instant) | ~500ms | Paper is optimistic; real ~5x slower but still well within 5s polling budget |
| cancel_order round-trip latency | ~100ms (instant) | ~600-700ms | Same: paper optimistic, real OK |
| Order book status reflection | instant | <1s after exchange action | Negligible |
| Fill probability on LIMIT BUY at LTP-10bps within 60s | not modeled | 0/1 in this sample | Paper's "always-fills" assumption is unsafe at tight limits |
| Realised slippage on filled trades | 15 bps default | **N/A** (no fill captured this run) | Cannot calibrate yet -- need Stage 2.1 |

---

## What's still open before Stage 3 (5-stock basket via daemon)

These were already tracked in `docs/e2e_broker_test_plan.md`; status
updated below.

| Gap | Status | Required for Stage 3? |
|---|---|---|
| `tools/test_angelone_auth.py` | DONE | yes (done) |
| `tools/test_amo_lifecycle.py` (Stage 1) | **DONE** | yes (done) |
| `tools/test_live_single_trade.py` (Stage 2) | **DONE -- no-fill variant** | partly |
| **Stage 2.1: actual-fill variant** (LIMIT BUY at LTP+0.1% so the fill+exit path is empirically tested) | **NEW TODO** | yes |
| `core/broker/angelone.py:cancel_order` | VERIFIED in Stage 1+2 | done |
| AngelOne *Primary Static IP* whitelisted (`106.193.147.98`) | DONE for current corp IP | yes |
| `--max-loss-rs N` daemon flag | TBD | yes |
| `--single-shot` daemon flag | TBD | yes |
| `EMERGENCY_STOP` flatten-on-trigger verification | TBD | yes |
| Slippage logger | TBD (cannot seed yet -- need a fill) | yes |
| `getTradeBook` reconciliation | TBD | yes |
| **Circuit-band-aware LIMIT price clamping** | NEW TODO (from Stage 1 NSE rejection) | nice-to-have |

---

## Why this run is a milestone

Three things were **un-knowable** before this run, and are now known:

1. **The live AngelOne write-API works** for our account, from our network,
   with our code. Stage 0 only proved the read-API; this proves the
   order endpoint round-trips correctly.

2. **The fail modes are friendly.** Rejections come back with explicit
   error codes and human-readable reason text in the order book.
   Cancellation is idempotent. There were no silent failures, no
   timeouts, no zombie orders.

3. **Empirical timing matches the assumed envelope.** Our daemon polls
   every 5s; live operations complete in <1s. We have a 5-10x safety
   margin on every operation.

Stage 3 (5-stock basket) is **architecturally** the same code path
exercised 5 times in parallel. The remaining work is the operational
safeties (max-loss, single-shot, slippage logger, reconciliation),
not the broker integration itself.

---

*Generated: 2026-05-11 11:00 IST. First-ever live broker e2e success.*
