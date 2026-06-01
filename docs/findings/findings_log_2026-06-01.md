# Findings Log — 2026-06-01 (Charges Model Calibration vs AngelOne)

**Author:** trading-agent audit pass
**Trigger:** operator-supplied AngelOne brokerage calculator + rate schedule
**Scope:** `packages/core/charges.py` (one file, freeze-safe — not on the
`FREEZE_v2.1.md` frozen list)
**Status:** code fixed + tests pinned + per-variant PF adjustment computed.
Pre-existing battery JSONs were NOT re-run; the adjustment is a transparent
post-hoc recomputation from each trade record's `entry_price`, `exit_price`,
and `quantity` so the original pre-committed verdict numbers stay traceable.

---

## Executive summary

The cost model in `packages/core/charges.py` was calibrated for **Zerodha**
when the live trader has been on **AngelOne** the entire run. Across 5 rate
constants the gap consistently UNDER-counted charges, producing optimistic
profit factors in every backtest variant since the model was written:

| ID     | What was modelled               | What AngelOne actually charges | Direction of P&L bias |
|--------|----------------------------------|---------------------------------|------------------------|
| CHG-01 | Intraday brokerage 0.03%, no floor | 0.1% with ₹5 floor, ₹20 cap   | Optimistic by 3-4× on small intraday |
| CHG-02 | No minimum brokerage             | ₹5 floor per executed order     | Optimistic on every trade < ₹5k notional |
| CHG-03 | Delivery brokerage **0.0%**      | 0.1% with ₹5 floor, ₹20 cap   | Optimistic by ~₹40 round-trip on EVERY v3 swing trade |
| CHG-04 | Stamp duty single rate 0.003%   | Intraday 0.003%, **delivery 0.015%** (5×) | Optimistic on delivery buy leg |
| CHG-05 | DP charge ₹13.5 (CDSL pass-through) | AngelOne markup ₹20 + 18% GST | Optimistic by ~₹7.67 per delivery SELL |

The most material miss is **CHG-03**: until today, `BROKERAGE_DELIVERY_PCT`
was `0.0`, so every v3 swing variant ran with zero brokerage. Combined with
CHG-05's under-stated DP charge, every V20–V25 trade was credited with
~₹47.67 less cost than it would incur in production. With v3 already
producing PF < 1.0 at zero brokerage (see `v3_phase_a5_forensic_2026-05-30.md`,
`brutal_review_2026-05-30.md`), the corrected PF is materially worse.

The adjustment numbers are in `docs/findings/charges_pf_adjustment_2026-06-01.md`.

---

## Discipline note — was this allowed during the freeze?

Yes, but with a caveat that's worth pinning here so future audits can find
the reasoning.

* **`packages/core/charges.py` is NOT on the freeze list.** `FREEZE_v2.1.md`
  enumerates `risk_manager.py`, `position_sizer.py`, `strategies/`,
  `ensemble.py`, model artefacts and config knobs. Charge math is upstream
  infrastructure (everyone — backtester, paper trader, live trader, risk
  gate — calls it) and was explicitly outside the "behavioural freeze"
  envelope. Fixing it does **not** consume a slot.
* **The change is baseline-shifting** — every backtest run from now on will
  report tighter PFs than runs done before this commit. This is the right
  direction (less optimism, fewer surprises in live), but it means the
  pre-committed V25 verdict numbers from Saturday were measured against
  the OLD baseline. To keep the pre-commit traceable, the adjustment is
  done by post-hoc recomputation from the existing trade-level JSONs —
  **the JSONs themselves are not modified**, and the v3 forensic note is
  not rewritten. New footnotes simply point at the adjustment report.
* **The live trader VM is paused** (capital exit Friday per the wind-down
  sheet). The deployed AngelOne calibration cannot hurt anything because
  no trades are being placed. When/if the v3 phase B paper-trading
  begins, the new rates will apply to the paper-traded P&L and be
  comparable to live; that is the correct invariant.

---

## CHG-01 — Intraday brokerage rate (Zerodha 0.03% → AngelOne 0.1%)

**Severity:** High (3.3× under-count on every intraday trade below cap)
**File:** `packages/core/charges.py:110-114` (pre-fix line numbers)
**Old:** `BROKERAGE_INTRADAY_PCT = _env_float("BROKERAGE_INTRADAY_PCT", 0.0003)`
**New:** `BROKERAGE_INTRADAY_PCT = _env_float("BROKERAGE_INTRADAY_PCT", 0.001)`

**Source of truth:** AngelOne brokerage calculator —
https://www.angelone.in/calculators/brokerage-calculator

> "Equity Delivery & Intraday: Lower of ₹20 or 0.1% of transaction value
> per executed order (with a minimum brokerage of ₹5)."

**Worked example (AngelOne docs, intraday, 50 × ₹1000 buy + 50 × ₹2000 sell):**
Their published brokerage is **₹40.00** (₹20 + ₹20, both legs at the ₹20 cap).
The pre-fix code returned ₹35 (₹15 buy leg below cap at 0.03% × ₹50k + ₹20
sell leg at cap). After the fix the model returns ₹40 — see
`tests/unit/test_charges_angelone_2026_06_01.py::test_angelone_documented_example_brokerage_matches`.

**Impact on v2.1 intraday battery:**
v2.1 trades are MIS (intraday). Most v2 variants traded near or above the
cap threshold (turnover ≥ ₹6,666 per leg → brokerage capped at ₹20). For
those, CHG-01 raised brokerage from ₹15-20/leg to ₹20/leg — small absolute
change. For small-notional trades it can raise per-leg brokerage from ~₹1-3
to the ₹5 floor or ₹20 cap. Aggregate impact on PF: see adjustment report.

---

## CHG-02 — Minimum brokerage of ₹5 per executed order

**Severity:** Medium
**File:** `packages/core/charges.py` (NEW: `BROKERAGE_MIN_PER_ORDER`)

The pre-fix model had no per-order floor: a ₹1,000 intraday trade paid
0.03% × 1000 = **₹0.30** brokerage on each leg. AngelOne charges ₹5 per
executed order regardless of how small the turnover is. Implementation:

```python
def _brokerage_dec(turnover, product):
    rate = BROKERAGE_DELIVERY_PCT if product == "DELIVERY" else BROKERAGE_INTRADAY_PCT
    brok = turnover * Decimal(str(rate))
    cap = Decimal(str(BROKERAGE_MAX_PER_ORDER))      # ₹20
    floor = Decimal(str(BROKERAGE_MIN_PER_ORDER))    # ₹5 (NEW)
    capped = brok if brok < cap else cap
    return capped if capped > floor else floor
```

**Impact:** Mostly affects very-small intraday trades (notional < ₹5,000).
v2 battery shows most trades are sized well above this threshold (`risk.max_position_value` ≥ ₹15k in shipped configs), so absolute impact is
modest. For trades at or below ₹5k notional, brokerage roughly doubles or
triples.

---

## CHG-03 — Delivery brokerage was modelled as ZERO (Zerodha free; AngelOne charges 0.1%)

**Severity:** **Critical** — affects every v3 swing trade
**File:** `packages/core/charges.py:111` (pre-fix)
**Old:** `BROKERAGE_DELIVERY_PCT = _env_float("BROKERAGE_DELIVERY_PCT", 0.0)`
**New:** `BROKERAGE_DELIVERY_PCT = _env_float("BROKERAGE_DELIVERY_PCT", 0.001)`

This is the largest single miss. Zerodha runs zero-brokerage on delivery as
a discount-broker marketing position; AngelOne does **not**. AngelOne
charges the same 0.1% / ₹20 cap / ₹5 floor rule on delivery as on intraday.

**Per-trade impact at v3 swing sizing:**
v3 swing battery uses notional around ₹15–25k per trade (configurable up
to ₹50k). At ₹20k notional per leg, 0.1% → ₹20 cap on each leg. So every
v3 delivery round trip silently lost ₹40 of brokerage. With ~150–200
trades per 180-day variant, this is **₹6,000–₹8,000 of un-modelled cost
per variant** — material against the absolute PnL of ±₹3,000–₹10,000 the
v3 variants reported.

This finding alone is large enough to flip several v3 variants from
"PF < 1.0 but close" to "PF clearly < 1.0", and turns V25's already-poor
PF=0.23 into something worse.

---

## CHG-04 — Stamp duty applied intraday rate to delivery (SEBI rate is 5×)

**Severity:** Medium (delivery only)
**File:** `packages/core/charges.py:119` (pre-fix)
**Old:** `STAMP_DUTY_BUY = _env_float("STAMP_DUTY_BUY", 0.00003)` — applied uniformly
**New:** Split constant by product:

```python
STAMP_DUTY_BUY_INTRADAY = _env_float("STAMP_DUTY_BUY_INTRADAY", 0.00003)  # 0.003%
STAMP_DUTY_BUY_DELIVERY = _env_float("STAMP_DUTY_BUY_DELIVERY", 0.00015)  # 0.015%
```

**Source:** SEBI Uniform Stamp Duty Act 2020 (the "state-wise" language in
AngelOne's UI is a vestige of the pre-2020 regime). Intraday is 0.003%;
delivery is 0.015% — exactly 5×.

**Per-trade impact at v3 swing sizing:** On a ₹20k delivery buy leg,
stamp duty rises from **₹0.60 → ₹3.00**, a ₹2.40 absolute per round
trip. Modest by itself but stacks with CHG-03 and CHG-05.

---

## CHG-05 — DP charge ₹13.5 (CDSL) → ₹20 (AngelOne)

**Severity:** Medium (delivery only, sell leg only)
**File:** `packages/core/charges.py:123` (pre-fix)
**Old:** `DP_CHARGE_CDSL = _env_float("DP_CHARGE_CDSL", 13.5)`
**New:** `DP_CHARGE = _env_float("DP_CHARGE", 20.0)`

Zerodha bills the CDSL pass-through cost literally (₹13.5). AngelOne marks
it up to **₹20 + 18% GST** per their published schedule:

> "Angel One charges a DP charge of Rs. 20 plus GST."

The constant + env-var were renamed from `DP_CHARGE_CDSL` to `DP_CHARGE`
because the cost is no longer just the CDSL pass-through — it's an
AngelOne-specific number. A deprecation handler logs a CRITICAL line if
the operator still has `TRADING_CHARGES_DP_CHARGE_CDSL` set on the VM, so
the env-var rename is not silent:

```python
def _deprecated_dp_env():
    legacy = os.environ.get("TRADING_CHARGES_DP_CHARGE_CDSL")
    if legacy is not None:
        logger.critical(
            f"[charges] env var TRADING_CHARGES_DP_CHARGE_CDSL={legacy!r} is "
            f"DEPRECATED -- rename to TRADING_CHARGES_DP_CHARGE..."
        )
```

**Per-trade impact:** Each delivery SELL pays ₹23.60 (₹20 × 1.18) instead
of ₹15.93 (₹13.50 × 1.18). Delta = **₹7.67 per delivery round trip**.

---

## Bonus fix: NUM-10 invariant repair

While the CHG fixes were being pinned with regression tests, the
"`compute_round_trip == compute_one_leg(BUY) + compute_one_leg(SELL)`"
invariant (NUM-10 from the May 27 audit) was caught violating on
delivery trades by ±1 paisa. Root cause: STT-delivery was computed as
`_q((buy_val + sell_val) × STT_DELIVERY)` (single quantize on the sum)
while the per-leg path quantizes each leg independently. Fixed by
quantizing per leg, then summing:

```python
if product == "DELIVERY":
    stt_buy = _q(buy_val * Decimal(str(STT_DELIVERY)))
    stt_sell = _q(sell_val * Decimal(str(STT_DELIVERY)))
    stt = stt_buy + stt_sell
```

The 1-paisa drift only mattered for delivery; intraday was always per-leg
correct (STT only on the sell leg, single quantize is the whole thing).
Pinned by `test_round_trip_equals_sum_of_legs_invariant_preserved`.

---

## Disclosure logging

`packages/core/charges.py` now emits a single INFO line at module-import
time naming the active calibration:

```
[charges] active rates: broker=AngelOne | intraday_brokerage_pct=0.001 |
delivery_brokerage_pct=0.001 | brokerage_cap=Rs20.0 | brokerage_min=Rs5.0 |
stt_intraday_sell=0.00025 | stt_delivery=0.001 | stamp_intraday_buy=3e-05 |
stamp_delivery_buy=0.00015 | dp_charge=Rs20.0
```

The absence of a line like this was the root cause of the
Zerodha-vs-AngelOne gap surviving six months of audits — there was no
breadcrumb in the daemon log to make the broker calibration visible.
Future operators reading the log on day one will see the broker name and
can immediately spot a mismatch.

---

## Tests

`tests/unit/test_charges_angelone_2026_06_01.py` — 22 tests, all pass:

* Pin each of the 5 new rate constants against AngelOne defaults.
* Replay AngelOne's published worked example (50 × ₹1k buy + 50 × ₹2k sell
  intraday) and assert brokerage = ₹40.00.
* Pin the ₹5 floor for both intraday and delivery.
* Pin the product-aware stamp duty helper (`_stamp_duty_rate`).
* Pin the DP charge applies to delivery SELL only and is ₹20 × 1.18 = ₹23.60.
* Pin the deprecated `TRADING_CHARGES_DP_CHARGE_CDSL` env var does NOT
  silently override the new default.
* Re-verify the NUM-10 round-trip-equals-legs invariant on both products.
* Verify env-var override mechanism is still wired correctly for all new
  constants.

**Tests broken by the rate change, then fixed in this commit:**

* `tests/unit/test_portfolio.py::TestPnLCalculation::test_total_value` —
  had `expected_cash = 10000.0 - 3500.0 - (3500.0 * 0.0003)` (hard-pinned
  the Zerodha rate inline). Rewritten to assert the structural invariant
  (`total_value == cash + position_value` byte-exact) plus a sanity-bound
  (entry commission < ₹20). The test no longer pins a specific broker.
* `tests/unit/test_short_selling.py::TestShortPortfolioTotalValue::test_total_value_unchanged_at_entry_price`
  — the `abs=5.0` tolerance assumed brokerage < ₹1; widened to `abs=20.0`
  with an inline note explaining the CHG context.
* `tests/integration/test_trade_perspective_fixes.py::TestStrategyAwareRRGate::test_mean_reversion_rejects_rr_0p4`
  — was rejecting at the earlier `reward_vs_charges` gate (AngelOne
  charges are now higher), masking the `poor_rr` assertion it pins.
  Bumped quantity 100 → 1000 so the trade clears the charges gate and
  actually exercises the RR gate the test claims to pin. Inline note
  explains why.

**Full test run after fixes:** 2,052 passed, 4 failed.

The 4 remaining failures are **pre-existing on `main`** (verified by
stashing the charges fix and re-running) and unrelated to this audit:

* `test_drain_replays_and_removes_spool_on_success`,
  `test_drain_keeps_files_when_replay_still_fails`,
  `test_drain_handles_missing_spool_dir_gracefully` — `AlertManager.drain_failed_alerts`
  now returns a `purged_test` dict key that the test doesn't expect.
* `test_eod_summary_does_not_also_send_daily_report` — `_maybe_send_eod_summary`
  in `trading_agent.py` was reformatted recently and no longer contains
  the literal string `'send_alert("EOD Summary"'`.

These are out-of-scope for the charges audit. They should be fixed in
follow-up commits and are tracked elsewhere; flagging here only to
acknowledge they were known-failing on `main` before this commit.
