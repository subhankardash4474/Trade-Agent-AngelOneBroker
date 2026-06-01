# Changes Done — 2026-06-01 (CHG / Charges Calibration Sweep + Verdict-Week Prep)

> **TL;DR:** Operator-directed CHG sweep + verdict-week (2026-06-05) packet
> preparation. The Zerodha-rate charges model in `packages/core/charges.py`
> was rewritten to AngelOne rates — the broker we have actually been
> trading on since 2026-05-08 — closing CHG-01..CHG-05 from
> `docs/findings/findings_log_2026-06-01.md`. The diff is **freeze-safe by
> category** (charges.py is upstream cost infrastructure, not on the
> frozen behavioural-strategy file list per `FREEZE_v2.1.md`) — **no
> bypass slot consumed**. A post-hoc per-variant PF adjustment was
> computed across all 80 backtest variants on disk; a true V25 + V26
> re-simulation against the new rates is queued in
> `logs/backtests/battery_chg_recompute_20260601T114500/` (running at
> the time of writing, ~30-40 min wall clock). 4 pre-existing
> baseline-test failures unrelated to CHG were repaired in the same
> sweep. **2,056 / 2,056 tests passing**. Trader VM is presumed
> untouched per freeze charter §6.1 (no deploy.ps1 invocation today);
> only the local paper daemon has the new code, which cannot reach
> live broker orders. Brutal-review §10's other findings (audit
> cadence outage, kill PID 7) self-resolved between brutal-review
> capture and CHG-sweep completion — both verified healthy.

---

## Headline

- **Findings raised:** 5 CHG correctness defects + 1 invariant repair (`docs/findings/findings_log_2026-06-01.md`) + 7 brutal-review findings (`docs/reviews/brutal_review_2026-06-01.md` §1-§7).
- **Findings fixed this sweep:** 5 CHG (CHG-01..CHG-05) + 1 invariant (NUM-10 re-pin) + 4 pre-existing baseline-test failures.
- **Tests:** **2,056 / 2,056 passing** (1,995 pre-existing green + 22 new CHG regression pins in `tests/unit/test_charges_angelone_2026_06_01.py` + 4 baseline repairs + 35 lifted in other categories since last sweep).
- **Commits landed today (origin/main, in order):**
  1. `e277e21` — `fix(charges): AngelOne calibration (CHG-01..CHG-05) + NUM-10 delivery STT`
  2. `0d541ed` — `fix(tests): repair pre-existing baseline failures unrelated to CHG`
  3. `4a00e82` — `docs(findings): CHG-01..CHG-05 findings log + per-variant PF adjustment`
  4. `4e381a7` — `docs(v2.1+v3): CHG footnotes on decision + analysis docs`
  5. `52f650c` — `infra(cursor): agent onboarding doc + project-specific skills + rules`
  6. `22434cd` — `docs(reviews): 2026-06-01 brutal review + strategy-folklore reference`
  7. `135d749` — `chore(gitignore): exclude *.pkl.sha256 sidecars from untracked-files noise`
- **Freeze impact:** ZERO slots consumed. `packages/core/charges.py` is not enumerated in `FREEZE_v2.1.md`'s frozen file list (the freeze targets behavioural strategy / signal / sizing / regime logic; cost-model constants are upstream infrastructure). The diff is freeze-safe by category. Operator may overrule and account for a slot if they prefer the conservative reading.

---

## Phase 1 — CHG cluster (charges.py rewrite, AngelOne calibration)

> **Commit:** `e277e21` · 282 lines added / 38 removed in `packages/core/charges.py`;
> 282-line regression-pin test file added at
> `tests/unit/test_charges_angelone_2026_06_01.py`.

Source-of-truth references in `packages/core/charges.py` docstring:
AngelOne brokerage page (operator-pasted on 2026-06-01) +
[Angel One pricing](https://www.angelone.in/brokerage-charges) +
[NSE transaction charges](https://www.nseindia.com/regulations/transaction-charges).

| Finding ID | Severity | Discrepancy (Zerodha-rate ← → AngelOne actual) | File:line | Resolution |
|---|---|---|---|---|
| **CHG-01** | P0 | Intraday brokerage **0.03%** ← → **0.1%** (lower of ₹20 / 0.1%, min ₹5/order) | `packages/core/charges.py:75` | `BROKERAGE_INTRADAY_PCT 0.0003 → 0.001`. New constant `BROKERAGE_MIN_PER_ORDER = 5.0`. `_brokerage_dec()` now applies `max(min(rate * turnover, cap), floor)` so ₹5k turnover charges ₹5 floor instead of ₹1.50 silent free trade. |
| **CHG-02** | P0 | Delivery brokerage **0%** (Zerodha promo legacy) ← → **0.1%** (lower of ₹20 / 0.1%, min ₹5) | `packages/core/charges.py:77` | `BROKERAGE_DELIVERY_PCT 0.0 → 0.001`. Pre-fix: delivery was modelled as broker-free; AngelOne charges identically to intraday since 2025. |
| **CHG-03** | P0 | Stamp duty buy **uniform 0.003%** ← → **0.003% intraday vs 0.015% delivery** (5x higher for delivery) | `packages/core/charges.py:83,84` | Split into `STAMP_DUTY_BUY_INTRADAY = 0.00003` and `STAMP_DUTY_BUY_DELIVERY = 0.00015`. New helper `_stamp_duty_rate(product: str)` returns product-aware rate. Backtest paths in `compute_round_trip` + `compute_one_leg` route through it. |
| **CHG-04** | P1 | DP charge **₹13.5** (Zerodha CDSL flat) ← → **₹20** (AngelOne flat per ISIN, per sell-side delivery only) | `packages/core/charges.py:88` | `DP_CHARGE_CDSL` renamed to `DP_CHARGE`, default `13.5 → 20.0`. Legacy env var `DP_CHARGE_CDSL` still honoured with a CRITICAL log warning surfaced from new `_deprecated_dp_env()`. |
| **CHG-05** | P0 | Charges not surfaced on daemon start; operator cannot verify which rate set the live process is using | `packages/core/charges.py:200-225` | New `_log_active_rates()` called from module init (and shown in `logs/daemon_*.log` immediately after `[main] TRADING AGENT DAEMON STARTED`). Emits a single INFO line: `[charges] active rates: broker=AngelOne | intraday_brokerage_pct=0.001 | delivery_brokerage_pct=0.001 | brokerage_cap=Rs20.0 | brokerage_min=Rs5.0 | stt_intraday_sell=0.00025 | stt_delivery=0.001 | stamp_intraday_buy=3e-05 | stamp_delivery_buy=0.00015 | dp_charge=Rs20.0`. Operator can grep one line to confirm. |
| **NUM-10** | P2 (invariant repair) | `compute_round_trip` delivery-STT quantized buy+sell *combined* once, drifting 1-paisa vs `compute_one_leg` × 2 (round-trip ≠ sum of legs) | `packages/core/charges.py:148-156` | Now quantizes each leg independently, then sums. Restores the pre-CHG `compute_round_trip == compute_one_leg_buy + compute_one_leg_sell` invariant. Pinned by `test_round_trip_equals_sum_of_legs_invariant_preserved`. |

**Validation against AngelOne's published example** (50 shares @ Rs1,000 buy / Rs2,000 sell, intraday, NSE — totals from AngelOne calculator: Brokerage ₹40, STT/CTT ₹24.50, Txn ₹4.77, Stamp ₹4.41, SEBI ₹0.15, GST ₹8.09, **Total Taxes & Charges ₹81.92**): pinned in `test_angelone_intraday_example_50_at_1000` in the new test file. Our output rounds to ₹81.93 (1-paisa float discipline; under 0.1% tolerance).

---

## Phase 2 — Baseline test repairs (4 pre-existing failures unrelated to CHG)

> **Commit:** `0d541ed`. Discovered when running the full suite to validate
> Phase 1; pre-date this sweep. Fixed in the same commit-sequence
> to preserve a fully-green tree for verdict-week observability.

| Test | Root cause | Fix |
|---|---|---|
| `test_alert_retry_and_spool.py::test_drain_replays_and_removes_spool_on_success` | `AlertManager.drain_failed_alerts()` was updated to return a 4-key dict (added `purged_test`) but tests still asserted the 3-key shape. | Asserted `{"sent": N, "failed": N, "skipped": N, "purged_test": 0}`. |
| `test_alert_retry_and_spool.py::test_drain_keeps_files_when_replay_still_fails` | Same root cause. | Same fix shape. |
| `test_alert_retry_and_spool.py::test_drain_handles_missing_spool_dir_gracefully` | Same root cause. | Same fix shape. |
| `test_eod_audit_fixes.py::test_eod_summary_does_not_also_send_daily_report` | Test sliced `_maybe_send_eod_summary` to the first 5,000 chars to scan for `send_alert(...)`; the method grew beyond that ceiling between sweeps and the assertion silently missed the call. | Removed the 5,000-char cap; slice extends to the next sibling `def` so the entire method body is scanned regardless of length. |

Also touched by the CHG diff (not failures, but assertions hardcoded to Zerodha rates):

| Test | Fix |
|---|---|
| `tests/unit/test_portfolio.py::TestPnLCalculation::test_total_value` | Switched from `expected_cash = 10000 - 3500 - (3500 * 0.0003)` (hard-pinned to Zerodha intraday rate) to a **structural invariant**: `abs(total_value - (cash_after + 3600)) < 0.01` (total_value == cash_remaining + mark-to-market) + a sanity bound on commission charged (`0 < entry_commission < 20.0`). Rate-set independent. |
| `tests/unit/test_short_selling.py::TestShortPortfolioTotalValue::test_total_value_unchanged_at_entry_price` | Tolerance widened `abs=5.0 → abs=20.0` to accommodate AngelOne's higher per-trade floor + brokerage. Explanatory comment cites the CHG sweep. |
| `tests/integration/test_trade_perspective_fixes.py::TestStrategyAwareRRGate::test_mean_reversion_rejects_rr_0p4` | `quantity 100 → 1000` so the trade clears the `reward_vs_charges` gate (which now fires correctly with higher AngelOne costs) and proceeds to the `poor_rr` gate that was the test's intent. |

---

## Phase 3 — Findings ledger, PF adjustment, doc footnotes

> **Commits:** `4a00e82` (findings + adjustment), `4e381a7` (footnotes on 6 freeze/diagnostics docs), `22434cd` (today's brutal-review docs).

**3a — `docs/findings/findings_log_2026-06-01.md`** — CHG-01..CHG-05 + NUM-10 entries, severity, evidence (file:line + AngelOne calculator screenshot reference), impact, fix, and a "discipline note" explaining why the diff did not consume a freeze slot.

**3b — `tools/audit/charges_pf_adjustment_2026_06_01.py`** — post-hoc re-pricing script. Walks every `logs/backtests/*/results/*.json`, replays each trade through the new AngelOne `compute_round_trip`, computes the **extra charge** AngelOne would have levied on top of what the simulator recorded, subtracts that from each trade's PnL, and re-computes per-variant PF. Output: `docs/findings/charges_pf_adjustment_2026-06-01.md` (human readable) + `.csv` (Excel friendly).

**Headline aggregates from the post-hoc table** (full per-variant numbers in the .md):

| Family | Variants touched | Aggregate un-modelled charges (Rs) | PF range pre / post |
|---|---|---|---|
| v3 swing (all variants) | 24 | ~Rs +18,400 across all variants | PF 0.21-0.42 → **0.05-0.21** |
| **V25 specifically (verdict-meeting headline)** | 1 | **Rs +3,702.31** (189 trades, +Rs 19.59/trade extra) | PF **0.23 → 0.05** (35 winners flip to losers under correct charges) |
| v2.1 60d / 90d slate (long-only baseline) | 32 | ~Rs +9,200 across all variants | PF mostly already <1; further compressed |
| Tier-9 / capital-throttle / nifty50 deep slate | 24 | ~Rs +5,500 | Marginal but consistent direction |

> **Caveat (documented in the report):** the adjustment is a paper-arithmetic re-pricing,
> not a true re-simulation. It does NOT capture (a) trades that would have been
> rejected by the `reward_vs_charges` gate under AngelOne, (b) different position
> sizing under the higher cost floor, or (c) compounding effects on subsequent
> trades. The true-re-sim run in `battery_chg_recompute_20260601T114500/` (Phase 5b
> below) closes this caveat for V25 and V26.

**3c — Footnotes added to 6 prior decision docs** (so the historical PF figures
they quote read as "PF X.XX **at Zerodha rates; see CHG-NN for AngelOne
re-derivation Y.YY**"):

- `docs/freeze/wind_down_criteria_2026-06-05.md`
- `docs/freeze/freeze_v2.1_exit_criteria_2026-06-05.md`
- `docs/freeze/freeze_v3.0_charter_2026-05-30.md` (§1 finding #4 — the verdict-meeting-defining economic case)
- `docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md` (the forensic that produced V25's PF 0.23)
- `docs/reviews/brutal_review_2026-05-30.md`
- `docs/reviews/friday_review_2026-05-29.md`
- `logs/backtests/battery_v3_swing_a5_180d_eff_20260530T050422/comparison.md`
- `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/comparison.md`

No historical numbers were altered — only contextualised. The pre-CHG PFs remain in the historical record exactly as committed, with a footnote pointing to today's re-derivation.

**3d — `docs/reviews/brutal_review_2026-06-01.md` + `docs/reviews/strategy_reference_review_2026-06-01.md`** — the morning adversarial review + the strategy-folklore correction doc. Filed under the existing brutal-review skill conventions.

---

## Phase 4 — Cursor agent onboarding (one-time infra)

> **Commit:** `52f650c`. New files: `AGENTS.md`, `.cursor/rules/*`, `.cursor/skills/*`.

| File | Purpose |
|---|---|
| `AGENTS.md` (repo root) | First-load briefing for any Cursor agent / sub-agent: project overview, current verdict-week context, how to consult skills, where the daemon lives, what the freeze contract is. |
| `.cursor/skills/trading-audit/SKILL.md` | Lets the agent answer "what's the latest checkpoint?" without re-deriving from raw logs each time. |
| `.cursor/skills/brutal-review/SKILL.md` | Codifies the brutal-review persona contract + evidence-sweep tiers + output format. |
| `.cursor/skills/changes-done/SKILL.md` | Codifies the `docs/changes/changes_done_YYYY-MM-DD.md` ledger convention (this file is the first one written under the new skill). |
| `.cursor/rules/secret-hygiene.mdc` | Project-specific guard: never commit `.env`, never log API tokens, refuse to expand any secret beyond its declared scope. |
| `.cursor/rules/test-conventions.mdc` | One-file regression-pin convention; `test_<concern>_<YYYY_MM_DD>.py` naming; structural-invariant assertions over hard-coded numerics. |

`.gitignore` also patched (`135d749`) to exclude `**/market_data.pkl.sha256` sidecars so a clean `git status` doesn't churn on cache-load mismatches.

---

## Phase 5 — Verdict-week operations

### 5a — Audit-cadence and laptop-daemon (Q3 §4 + §5, self-resolved)

> Brutal-review §4 claimed "no 10:00 or 11:00 checkpoint today" (captured ~11:10 IST).
> Verified at 11:38 IST:

| Checkpoint file | Generated-for (filename) | Daemon PID at capture | Daemon uptime at capture |
|---|---|---|---|
| `logs/audit/2026-06-01/checkpoint_0900.md` | 09:00 IST | PID 7 | ~67 h |
| `checkpoint_1001.md` | 10:01 IST | PID 7 | ~68 h |
| `checkpoint_1100.md` | 11:00 IST | PID 7 | **4080.6 min (68 h)** |
| `checkpoint_1136.md` | 11:36 IST | **PID 6** | **2.1 min** |

The audit cadence **is** healthy — 4 checkpoints today on the hourly cadence. The §4 finding was a transient observation that resolved before the CHG sweep completed.

**The daemon restarted at 11:34:36 IST.** PID 7 (66+ hours uptime, on pre-CHG code) was killed (operator action or container health-check) and replaced by PID 6 which booted with the new AngelOne charges (the `[charges] active rates: broker=AngelOne ...` line in `logs/daemon_2026-06-01.log` is `_log_active_rates()` from commit `e277e21`).

**Implications for the verdict packet:**
- Brutal-review §5 ("kill PID 7") is **closed**. PID 7 is dead. PID 6 is the fresh paper-mode daemon.
- The local paper daemon is now running with AngelOne charges — meaning the operator can read `logs/health.json` and `logs/audit/*/checkpoint_*.md` over the next 4 trading days at the same cost model the live broker actually applies. Verdict-week observability is **better** than the brutal review assumed.
- No new live trades since the restart; PID 6's `cycle_count: 4` at 11:44:24 IST.

### 5b — Battery re-simulation (V25 + V26 at AngelOne rates)

Launched at 11:48:54 IST as `logs/backtests/battery_chg_recompute_20260601T114500/`, variants `V25_swing_combined_shorts` + `V26_swing_combined_shorts_high_cap`, workers=2. **Completed at 11:50:16 IST** (101 seconds — much faster than the brutal-review's 25-40 min estimate because v3-swing is on 1d bars, not 5m).

**Headline numbers** (from `logs/backtests/battery_chg_recompute_20260601T114500/comparison.md`):

| Variant | PF | Trades | WR% | MaxDD% | Ret% | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| **V25-Zerodha** (May 30 baseline, historical) | 0.23 | 189 | ~5% | ~30% | -41% | (recorded) |
| **V25-AngelOne post-hoc estimate** (Phase 3b script) | 0.05 | 189 | — | — | — | — |
| **V25-AngelOne true re-sim** (this run) | **0.04** | 190 | **2.1%** | **76.6%** | **-76.6%** | -8.3 |
| **V26-AngelOne true re-sim** (this run, first ever V26 execution) | **0.01** | 195 | **1.0%** | **82.3%** | **-82.3%** | -8.2 |

**Two decisive findings for the verdict meeting:**

1. **V25 at real AngelOne costs is even worse than the post-hoc estimate** (PF 0.04 vs 0.05 estimated). Compounding amplifies the bleed: each loss shrinks the capital base, which makes the next AngelOne ₹20 brokerage cap-or-floor a larger percentage of available capital, which means smaller position sizes and worse R:R on the next trade. **MaxDD jumps from ~30% (Zerodha-rate read) to 76.6% (AngelOne true cost)** — V25 literally bleeds out three-quarters of the operator's capital over the 600-day window at realistic broker fees.
2. **V26 (loosened position cap from 5 to 15) is WORSE than V25, not better.** PF 0.01 < 0.04. The "more positions = more diversification = absorb the short-emission flood" thesis from charter §8.3 is **decisively refuted**: more positions just means more losing trades multiplied by AngelOne's per-trade cost floor. **Brutal-review §2 (Session 3's position-cap-dilution objection) is closed in favor of wind-down.** The escape clause "if V26 PF ≥ 1.0, defer wind-down" does not trigger; the escape clause "if V26 PF < 1.0, wind-down is complete-evidence" is met.

> Note: yfinance market-data cache from the 2026-05-30 V25 run was rejected on
> load by a pandas StringDtype upgrade between then and today; battery fell back
> to a fresh fetch (30 stocks, 600 days, ~30 s). The fresh data has 3 extra
> trading days vs the May 30 fetch and may carry tiny retroactive split/dividend
> adjustments. The V25-Zerodha 0.23 → V25-AngelOne 0.04 delta (≈82% PF
> compression) is orders of magnitude larger than any plausible
> data-refresh noise — the signal is unambiguous.

### 5c — Verdict-meeting packet (drafting in parallel)

`docs/freeze/verdict_meeting_packet_2026-06-05.md` will consolidate, in order:

1. `wind_down_criteria_2026-06-05.md` (pre-committed gate sheet — operator's trigger conditions).
2. V25 PF triplet (V25-Zerodha 0.23 / V25-AngelOne post-hoc 0.05 / V25-AngelOne true re-sim PENDING) + V26 PF + charges commit hash `e277e21`.
3. `strategy_reference_review_2026-06-01.md` (honest retail-trend benchmark calibration).
4. `brutal_review_2026-06-01.md` (both sessions) + `brutal_review_2026-05-30.md` predecessor.
5. `findings_log_2026-06-01.md` (CHG-01..CHG-05) + `charges_pf_adjustment_2026-06-01.md`.
6. This file (`changes_done_2026-06-01.md`).
7. EOD audit checkpoints 2026-06-01 → 2026-06-04 (daily, auto-generated; cadence verified healthy in 5a).
8. `freeze_v3.0_charter_2026-05-30.md` with §1 finding #4 already footnoted (Phase 3c).

### 5d — Trader-VM deploy status (Q3 §7)

`tools/cloud/deploy.ps1` requires explicit `-VmHost` invocation; **no deploy was triggered today**. Per freeze charter §6.1 ("trader VM untouched"), the live trader is still running the last commit before freeze opened (≤2026-05-30 14:47 IST). This means:

- **The live trader does NOT have today's CHG fix.** Live broker orders since 2026-05-08 have been priced at Zerodha rates internally; **AngelOne has been charging actual AngelOne rates regardless**. Net effect: every live trade has been silently more expensive than the daemon's internal PnL ledger believes by ~Rs 20-25/trade.
- **The live trader does NOT have Saturday's Bug A denylist / persist-guard fix.** Per the freeze contract this is intentional; the operator can choose to deploy after the 2026-06-05 verdict meeting.
- **The local paper daemon DOES have both fixes** (restarted at 11:34:36 IST per Phase 5a). Paper-mode short-circuits before any AngelOne place-order call, so no live execution risk from the local daemon running ahead of trader-VM code.

**Recommendation for the verdict packet:** explicitly note that the live trader's internal PnL ledger is silently optimistic by ~Rs 20-25/trade since 2026-05-08; the cumulative ledger figure of ₹-1,212 is actually closer to **₹-1,500 to ₹-1,700** at real broker-charged costs. This direction-only correction further strengthens the wind-down case rather than threatening it.

---

## Tests

- **2,056 / 2,056 passing.** No skipped tests in the new suite.
- New regression file: `tests/unit/test_charges_angelone_2026_06_01.py` (22 tests pinning defaults, the AngelOne calculator example, brokerage cap/floor logic, product-aware stamp duty, DP charge, deprecated env var handling, and the NUM-10 invariant).
- Three rate-set-coupled tests rewritten to be **structural-invariant** rather than hard-pinned to a specific rate set (Phase 2 table) — so the next broker switch won't need them re-touched.

---

## Deferred

Nothing was deferred from this sweep that's verdict-meeting-blocking. The
following are explicitly **not actioned today** and will be revisited
post-2026-06-05:

| Item | Defer reason |
|---|---|
| Brutal-review §3 — `tests/conftest.py` isolation (pytest → production-log leak) | Dev-time noise, not verdict-meeting blocking. Third request; will be queued for the post-verdict sweep. |
| Brutal-review §6 — DB-blindspot backfill (7 missing rows in `data/trading_agent.db.trades` vs `logs/trades.csv`) | Verdict packet will quote CSV directly and note the DB gap. Backfill under freeze risks introducing replay-time mismatches. |
| Trader-VM deploy of CHG + Bug A | Operator's call post-verdict. The freeze charter explicitly forbids touching the trader VM through 2026-06-05. |
| True re-sim of all 80 backtest variants under AngelOne rates | Only V25 + V26 are verdict-meeting-relevant; the other 78 variants are well-served by Phase 3b's post-hoc adjustment. A full re-sim sweep is a post-verdict cleanup task. |

---

## Cross-references

- `docs/findings/findings_log_2026-06-01.md` — full CHG-01..CHG-05 + NUM-10 detail.
- `docs/findings/charges_pf_adjustment_2026-06-01.md` + `.csv` — per-variant post-hoc PF adjustment.
- `docs/reviews/brutal_review_2026-06-01.md` (both sessions) + `docs/reviews/strategy_reference_review_2026-06-01.md` — the morning adversarial review and the strategy-folklore companion.
- `docs/freeze/verdict_meeting_packet_2026-06-05.md` — assembled in Phase 5c; references this file as ledger.
- `FREEZE_v2.1.md` — frozen-file list. Cross-checked 2026-06-01 11:25 IST: `packages/core/charges.py` is **not** enumerated; CHG diff is freeze-safe.
- `logs/backtests/battery_chg_recompute_20260601T114500/` — V25-AngelOne + V26 true re-sim (in flight at time of writing).
- Commit hashes (origin/main): `e277e21`, `0d541ed`, `4a00e82`, `4e381a7`, `52f650c`, `22434cd`, `135d749`.

---

> _Filed under the `changes-done` skill convention. This document is
> the verdict-meeting ledger for the CHG-and-prep work; the brutal
> review is the verdict-meeting adversarial record; the findings log
> is the verdict-meeting bug ledger; together they comprise the
> 2026-06-05 packet from the "what changed in the final week" angle._
