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

The audit cadence is healthy **at the daemon source-of-truth** — 4 checkpoints, each live-emitted by the running daemon for its respective hour window (verified by the PID + uptime stamps embedded in each file's body). The brutal-review Session 3 (filed 12:00 IST) correctly flagged that all 4 files have **local mtime 11:44:30** IST — that is a **OneDrive sync artefact**, not a backfill. The contents were written remotely at their nominal times; they arrived at this local Windows host in one sync batch when PID 6 booted and triggered an apparent file-set change OneDrive then propagated. **Implication for verdict-week observability**: treat checkpoint files as eventually-consistent from this local host; the authoritative cadence read is whatever the remote daemon writes. If verdict-meeting will quote in-week checkpoints, refresh from remote (or quote the daemon's `health.json` directly) before EOD on each day.

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

## Phase 6 — Forward plan (path-forward assessment + v4 strategy charter)

> **Time:** 12:14-12:18 IST. **Trigger:** operator question at 12:09 IST
> ("If I have to make the agent profitable what should I do, the strategies
> that we discussed can we build them and test in backtesting setup?") +
> 12:20 IST confirmation to file both `(a) path-forward` and `(b) V27 spec`
> AND add F&O paper-mode + mode-toggle flag system. **Commits:** uncommitted
> at time of writing.

Pure advisory writing, no code edits. Two markdown files filed under
`docs/reviews/` per `repo-conventions` "named non-EOD reviews + proposals"
convention. These docs are the post-wind-down forward plan; they do NOT
override `freeze_v3.0_charter_2026-05-30.md` pre-verdict — v3.0 remains
forward-plan-of-record until Friday, at which point it is archived
"never activated" and v4 activates conditional on operator answering
the charter's §10 Q1-Q10 in writing first.

| Artefact | Path | Length | Purpose |
|---|---|---:|---|
| Path-forward assessment | `docs/reviews/path_forward_assessment_2026-06-01.md` | 33 KB | Operator decision paper: records "fight till the end" stance verbatim, ₹42k/year cost-of-fight arithmetic at ₹120k capital, 4 parallel tracks (A cross-asset trend cash / B F&O swing paper / C F&O options paper / D cointegration research) + Track D discretionary parallel cross-check, mode-flag architecture, six-phase 18-month calendar, five project-wide kill criteria (PK1 18mo timeout most important), probability calibration table, seven open questions. |
| v4 strategy charter | `docs/reviews/strategy_charter_v4_2026-06-01.md` | 47 KB | Technical companion: four falsifiable hypotheses, 21 net-new modules, full canonical `config.yaml` mode-flag schema with capital-gate enforcement ("I accept ruin risk" override string), V27 cross-asset trend complete spec (75-instrument universe, Donchian-55/20, vol-targeted 0.5% risk, risk-parity allocator, AngelOne charges, NIFTYBEES benchmark, A1-A5 backtest stop criteria, V28-30 retune budget), F&O backtester extensions with six new failure modes + 10-day NSE premium validation gate (~52 person-day dev block), mode dispatcher contract + paper-broker isolation tests, ten open design questions. |

**Verdict-meeting packet edit** (same time-window): added a forward-plan
note between §7 step 2 and step 3 + a new §8.G appendix subsection
pointing at both new docs. Edits are non-disruptive to the mechanical
wind-down logic — they are pointers, not decisions.

| File touched | Edit type | Diff |
|---|---|---|
| `docs/freeze/verdict_meeting_packet_2026-06-05.md` | append in §7 + new §8.G | ~30 lines added, 0 modified |

### Why

Operator's 12:09 IST question pivoted the conversation from
verdict-week tactical review to post-wind-down strategic planning.
Two adversarial reasons to file BOTH a decision paper AND a separate
technical charter, instead of one combined document:

1. **Decision-evidence separation.** The decision paper (§2 cost
 arithmetic, §6 PK1-PK5 kill criteria, §7 probability calibration)
 should be re-readable in 6 months when the operator might be
 emotionally invested in defending a specific track. The technical
 charter (V27 params, F&O engine requirements, dispatcher
 contract) is reference material consulted during builds. Mixing
 them dilutes both.
2. **Pre-commit discipline.** Both docs are pre-committed BEFORE
 Phase 1 dev starts (Mon 2026-06-08), so the framing cannot be
 calibrated by what the first backtest produces. This is the same
 discipline `freeze_v3.0_charter_2026-05-30.md` v1.0/v1.1 used and
 the discipline `wind_down_criteria_2026-06-05.md` enforces for
 Friday.

The mode-flag dispatcher architecture (charter §2) is the
**single most important design decision** in the v4 plan. Building
it later requires a rewrite; building it up-front pays compound
interest as Modes B, C, D land. The charter's §10 Q5 (hard cutover
vs feature-flag rollout) is the operator's first non-trivial design
call.

### Type / Risk / Rollback / Verification

- **Type:** docs (pure markdown; no source-code edits).
- **Files touched:** 2 new + 1 modified
 - `docs/reviews/path_forward_assessment_2026-06-01.md` (new, 33 KB)
 - `docs/reviews/strategy_charter_v4_2026-06-01.md` (new, 47 KB)
 - `docs/freeze/verdict_meeting_packet_2026-06-05.md` (modified — §7 forward-plan note + §8.G appendix subsection added)
- **Trigger:** operator-requested (`yes please do both`, 12:11 IST and
 `yes both please both`, 12:20 IST).
- **Risk:** LOW. Pure documentation. Cannot affect daemon, trader VM,
 backtester, DB, or any executable code path. The
 verdict-meeting-packet edit adds content WITHOUT altering the
 mechanical wind-down logic (the recommendation in §7 and the
 evidence in §0-§6 are untouched).
- **Rollback:** `Remove-Item docs/reviews/path_forward_assessment_2026-06-01.md`
 + `Remove-Item docs/reviews/strategy_charter_v4_2026-06-01.md`
 + `git checkout docs/freeze/verdict_meeting_packet_2026-06-05.md`
 (or `git restore` equivalent). No downstream references exist yet —
 these docs are top-of-chain, nothing imports or links INTO them
 except the verdict-packet edit itself which the rollback also reverts.
- **Verification:** Both new files written, byte sizes match expected
 (33 KB / 47 KB), `ReadLints` clean on both. Verdict-packet edit
 verified by re-read of §7 (forward-plan note present between steps
 2 and 3) and §8 (G subsection present after F). Operator can grep
 `grep -r "path_forward_assessment" docs/` to confirm cross-references
 land.

### Freeze impact

ZERO slots consumed. Pure documentation under `docs/reviews/` and
`docs/freeze/` (the latter is appendix-only, not a freeze-list edit).
No `packages/` code touched. No `config.yaml` touched. No `tests/`
touched. The freeze contract is unaffected.

The charter explicitly states v4 activation is conditional on operator
answering its §10 Q1-Q10 in writing AND on the Friday verdict
producing wind-down. If the verdict surprises with "don't wind down"
(rendered overdetermined-improbable by V25/V26 at AngelOne rates per
§5b above), the v4 charter is shelved alongside v3.0 with the same
"never activated" archive treatment.

### Cross-links

- Companion: this file's Phase 5c (verdict-meeting packet drafting)
 — the v4 charter is a successor-pointer added to that packet.
- Up-chain: `docs/reviews/brutal_review_2026-06-01.md` Sessions 1-3
 — evidence base for why v2.1/v3 is winding down.
- Up-chain: `docs/reviews/strategy_reference_review_2026-06-01.md`
 — calibration of what's realistic for retail (3-7% CAGR trend,
 Virtu/Medallion impossible) that informs the charter's hypotheses.
- Sibling: `docs/freeze/verdict_meeting_packet_2026-06-05.md` §7 + §8.G
 (edited in this same phase).
- Successor: `docs/reviews/strategy_charter_v4_operator_responses_2026-06-XX.md`
 (NOT YET WRITTEN — operator's answers to charter §10 Q1-Q10, to
 be filed before Phase 1 starts 2026-06-08).

### Open follow-ups before Phase 1

| # | Action | Owner | Due |
|---|---|---|---:|
| 1 | Operator answers charter §10 Q1-Q10 in writing | operator | by 2026-06-08 |
| 2 | File `docs/reviews/strategy_charter_v4_operator_responses_2026-06-XX.md` | operator + agent | by 2026-06-08 |
| 3 | (Optional) Start Track D (manual discretionary swing) on existing capital | operator | any time post-verdict |
| 4 | Commit this changes-done update + the two new doc files + the verdict-packet edit | operator | EOD 2026-06-01 |

---

## Tests

- **2,056 / 2,056 passing.** No skipped tests in the new suite.
- New regression file: `tests/unit/test_charges_angelone_2026_06_01.py` (22 tests pinning defaults, the AngelOne calculator example, brokerage cap/floor logic, product-aware stamp duty, DP charge, deprecated env var handling, and the NUM-10 invariant).
- Three rate-set-coupled tests rewritten to be **structural-invariant** rather than hard-pinned to a specific rate set (Phase 2 table) — so the next broker switch won't need them re-touched.
- **Phase 6 (forward plan):** no tests; pure markdown.

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

## Phase 7 — V4 Mode A scaffolding & V27 first-cut backtest (12:42-13:30 IST)

Operator directive (12:31 IST): "Start building v4 strategies on the
backtester — we are not deploying anything to the trader VM where paper-mode
intraday is running, so development in parallel with the freeze window is
safe. If a good backtest result comes in, we deploy to paper mode for live
data on the next Monday 2026-06-08." This honors FREEZE_v2.1.md's letter
(no edit to enumerated frozen files) AND its intent (no trader-VM
deployment during validation).

### 7a — Charter §10 Q1-Q10 responses filed

All 10 charter §10 adviser recommendations accepted by operator without
override (full responses doc: `docs/reviews/strategy_charter_v4_operator_responses_2026-06-01.md`).
Highlights:

| # | Question | Operator answer |
|---|---|---|
| Q1 | Donchian entry channel | **55 days** (charter default) |
| Q2 | Vol-target risk per trade | **0.5% of equity** |
| Q5 | Dispatcher cutover | **Hard cutover** (one commit, no feature-flag coexistence) |
| Q7 | Live capital source | **AngelOne API daily** (fallback to self_sufficiency.json) |
| Q9 | Phase 1 start | **Mon 2026-06-08** (with pre-Phase-1 scaffolding starting today) |

Charter §10 gate is now satisfied **in writing**.

### 7b — Pod-boundary correction (charter §1 path drift)

Charter §1 listed Donchian signal utilities under
`packages/research/signals/`, but `tests/unit/test_pod_boundaries.py`
forbids `strategies -> research` imports (only `strategies -> core` is
allowed; the asymmetry is intentional — research is upstream of strategies
at audit time). Modules moved:

| Charter §1 path | Actual landing path |
|---|---|
| `packages/research/signals/donchian.py` | **`packages/core/signals/donchian.py`** |
| `packages/research/signals/volatility_sizer.py` | **`packages/core/signals/volatility_sizer.py`** |
| `packages/research/signals/risk_parity.py` | **`packages/core/signals/risk_parity.py`** |
| `packages/research/instruments/etf_universe.py` | **`packages/core/instruments/etf_universe.py`** |

`packages/research/backtest_fno.py` + `fno_universe.py` correctly remain
under `research/` (backtester-only; no strategy runtime call).

This is a NEW-file landing in `packages/core/` (new subdirectories
`core/signals/` + `core/instruments/`), not an edit to any FREEZE_v2.1
enumerated file (which lists only `core/risk_manager.py` + `core/position_sizer.py`).
Freeze-safe.

### 7c — V4 Mode A net-new files (10)

| File | LOC | Role |
|---|---:|---|
| `data/v4_universe_swing_cash.txt` | 75 | Mode A universe (50 Nifty + 15 Next 50 + 4 broad ETF + 2 commodity + 1 debt + 3 sector) |
| `packages/core/instruments/__init__.py` | 14 | Instrument-loader namespace |
| `packages/core/instruments/etf_universe.py` | 121 | `load_v4_swing_cash_universe()` + `universe_categories()` |
| `packages/core/signals/__init__.py` | 18 | Signal-utility namespace |
| `packages/core/signals/donchian.py` | 320 | Donchian entry/exit gates + chandelier stop |
| `packages/core/signals/volatility_sizer.py` | 130 | Vol-target sizing (0.5% risk, 8% per-name cap) |
| `packages/core/signals/risk_parity.py` | 180 | Inverse-vol allocator with iterative per-name cap |
| `packages/strategies/swing_cash/__init__.py` | 6 | Swing-cash namespace |
| `packages/strategies/swing_cash/cross_asset_trend_v27.py` | 200 | V27 strategy adapter (BaseStrategy contract) |

Plus 2 new test files (+54 new test cases):

| File | Test cases |
|---|---:|
| `tests/unit/test_v27_signals_2026_06_01.py` | 34 (Donchian, vol-sizer, risk-parity, universe loader) |
| `tests/unit/test_cross_asset_trend_v27_2026_06_01.py` | 20 (charter defaults pin, required-history, generate_signal paths, param customisation) |

Plus 2 new tools:

| File | Purpose |
|---|---|
| `tools/_v4_data_smoke_2026_06_01.py` | yfinance availability smoke test for the 75-instrument universe |
| `tools/v27_backtest_2026_06_01.py` | Standalone V27 backtester (does NOT use existing battery infra; uses new signals + sizer + allocator + AngelOne charges directly) |

### 7d — Data-availability smoke test result

`logs/v4_data_smoke_2026_06_01.json`: **73/75 OK** in 19.9s (5-year window).
2 failures, both known corporate-action artefacts:

- **TATAMOTORS.NS** — demerger Sep 2024; yfinance returns 404 for the
  current ticker because the merged-pre-demerger time-series sits under a
  different symbol now. Workaround for V28+: hand-splice the history from
  pre/post demerger.
- **LTIM.NS** — formed by LTI + Mindtree merger Nov 2022; yfinance has no
  pre-merger history under this symbol. Workaround for V28+: same.

Both are individual stocks; their absence drops the actual signal-candidate
universe to 73 (out of 74 after LIQUIDBEES cash-sweep exclusion).

All ETFs (commodity GOLDBEES/SILVERBEES, debt LIQUIDBEES, sector
ITBEES/PSUBNKBEES/AUTOBEES) returned data without issues. Two ETFs
launched within the 5-year window (SILVERBEES Feb 2022; AUTOBEES Jan 2022)
have ~4 years of data, which clears the 200-day SMA warmup + leaves ~3.5
years of in-sample history.

### 7e — V27 first-cut backtest result (THE NUMBER)

`logs/backtests/v27_firstcut_2026_06_01/comparison.md`. Window:
**2022-04-21 → 2026-05-29 (4.1 years)**. Initial capital: ₹100,000.

| Metric | V27 first-cut | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| **CAGR** | **+1.25%** | **+8.98%** | **-7.73pp** |
| Total return | +5.23% | +42.31% | -37.08pp |
| Max DD | -10.24% | -15.22% | +4.98pp |
| Final equity | ₹105,229 | ₹142,310 | -₹37,081 |
| **Profit factor** | **1.10** | — | — |
| Win rate | 36.9% (116/314) | — | — |
| Trades | 314 in 4.1y (≈ 76/yr) | — | — |
| Avg charges per trade | ₹46.27 | — | — |
| **Total charges** | **₹14,530** | — | — |

**Exit-reason breakdown (314 trades):**
- chandelier_stop: 201 (64%) — the 3.0×ATR trailing stop fires too often
- donchian_exit: 74 (24%) — the 20-day low breakdown
- time_in_trade: 31 (10%) — 60-day forced exits
- end_of_window_close_out: 8 (still open at backtest end)

**Charter §3.10 mechanical reading:**
- PF = 1.10 → **A2 borderline** ("defer to V28 with ONE param change")
- BUT CAGR 1.25% vs NIFTYBEES + 2% = 10.98% → if PF were ≥ 1.20 this would
  trigger **A3** ("academic-interest only — don't advance to paper")
- Reading: V27 is at the A1/A2 cliff AND would fall to A3 even if it
  cleared A2. **Does NOT meet A4 PASS gate** (PF ≥ 1.20 AND CAGR ≥ NIFTYBEES + 2%
  AND MaxDD ≤ 25%).

**Caveats (charter §3 deferred items for V28+; reading favours V27):**
- Sector cap (charter §3.6 max 3 per sector) NOT enforced — could be diluting
- NIFTYBEES quarterly-rebalance benchmark not yet wired (buy-and-hold only)
- Trade fills at TODAY'S close (charter implies next-bar-open; minor)
- 2 instruments dropped due to corp-action data gaps (negligible)
- The candidate-ranking when slots constrained picks LOWEST-vol names —
  which preferentially loads NIFTYBEES + JUNIORBEES + BANKBEES into the
  portfolio. **The strategy is partly trading the benchmark against itself.**
  V28 candidate: exclude NIFTYBEES/JUNIORBEES from the signal-candidate set
  (keep as benchmark only).

**Honest reading:** The first-cut CAGR gap (7.7pp below NIFTYBEES) is so
large that even if V28's single-param-change tightens things up
materially, closing 9.8pp (to clear NIFTYBEES + 2%) on a Donchian-55/20
strategy with 0.5% vol-target seems mechanically improbable. The
cross-asset trend hypothesis (charter §0 hypothesis #1) shows MARGINAL
edge (PF 1.10 > 1.0) but not enough to overcome benchmark drag at
₹120k capital + AngelOne CNC cost regime.

**Verdict-meeting implication:** V27 first-cut DOES NOT satisfy the
operator's "if good backtest result, deploy to paper mode 06-08" condition.
The 06-08 paper-mode flip is therefore **NOT** indicated on V27 first-cut data.

### 7f — Test sweep

```
$ pytest tests/unit -q
1872 passed in 39.63s
```

54 new V27 tests + 1818 prior = 1872. Pod-boundary test PASSED after the
research → core move. No regression.

### 7g — Recommended next moves (operator decision)

Three honest options for V28+ retune budget (charter §3.11 allows max 3
V-variants):

1. **V28 + V29 + V30** — exhaust the retune budget; expected gain: 2-4pp
   CAGR if all parameter changes help, still leaving a 4-6pp gap to
   NIFTYBEES + 2%. Cost: ~3 hours dev + 3 × 3.5min backtests. Reading: PROBABLE A3 (academic-interest only).
2. **V28 with NIFTYBEES/JUNIORBEES excluded from signal candidates** — single
   highest-impact change. Tests whether self-cannibalization explains the
   underperformance. Cost: ~30min dev + 3.5min backtest. If V28 closes >5pp
   of the gap, the hypothesis is alive; otherwise concede A3.
3. **Concede A3 on Mode A and pivot v4 to Mode B/C/D early** — but Mode B/C
   are F&O paper-only with the ₹500k capital gate; Mode D is research-only.
   At ₹120k operator capital, no live track is viable. Reading: this is
   the **honest** reading of the path-forward §3 + §6 PK1 ("if 18 months
   without any live-gate clearing, liquidate to NIFTYBEES").

My adviser recommendation: **Option 2 first** (30 min, single test of
the self-cannibalization hypothesis). If that closes the gap meaningfully,
proceed to V29/V30. If not, file A3 verdict on Mode A and start a fresh
path-forward refresh that questions whether ANY ₹120k retail trend
strategy can beat NIFTYBEES on a 5-year window.

This is operator's call. Will not initiate V28 without explicit directive.

### 7h — Files touched (Phase 7 cumulative)

Net-new (10 source + 2 tests + 2 tools + 1 universe data + 1 ops responses doc + this Phase 7 entry):

```
docs/reviews/strategy_charter_v4_operator_responses_2026-06-01.md     [+186 lines]
data/v4_universe_swing_cash.txt                                       [+75 instruments]
packages/core/instruments/__init__.py                                 [new]
packages/core/instruments/etf_universe.py                             [new]
packages/core/signals/__init__.py                                     [new]
packages/core/signals/donchian.py                                     [new]
packages/core/signals/volatility_sizer.py                             [new]
packages/core/signals/risk_parity.py                                  [new]
packages/strategies/swing_cash/__init__.py                            [new]
packages/strategies/swing_cash/cross_asset_trend_v27.py               [new]
tests/unit/test_v27_signals_2026_06_01.py                             [new, 34 tests]
tests/unit/test_cross_asset_trend_v27_2026_06_01.py                   [new, 20 tests]
tools/_v4_data_smoke_2026_06_01.py                                    [new]
tools/v27_backtest_2026_06_01.py                                      [new]
logs/v4_data_smoke_2026_06_01.json                                    [new artefact]
logs/v4_smoke_2026-06-01.log                                          [new artefact]
logs/backtests/v27_firstcut_2026_06_01/comparison.md                  [new artefact]
logs/backtests/v27_firstcut_2026_06_01/equity_curve.csv               [new artefact]
logs/backtests/v27_firstcut_2026_06_01/trades.csv                     [new artefact]
logs/backtests/v27_firstcut_2026_06_01/results.json                   [new artefact]
logs/backtests/v27_firstcut_2026_06_01/manifest.json                  [new artefact]
logs/v27_backtest_2026-06-01.log                                      [new artefact]
docs/changes/changes_done_2026-06-01.md                               [+this Phase 7 entry]
```

**Modified existing files:** NONE. All v4 work landed in net-new files;
freeze contract observed in both letter and spirit. No deployment to
trader VM.

---

## Cross-references

- `docs/findings/findings_log_2026-06-01.md` — full CHG-01..CHG-05 + NUM-10 detail.
- `docs/findings/charges_pf_adjustment_2026-06-01.md` + `.csv` — per-variant post-hoc PF adjustment.
- `docs/reviews/brutal_review_2026-06-01.md` (Sessions 1-3) + `docs/reviews/strategy_reference_review_2026-06-01.md` — the morning adversarial review and the strategy-folklore companion.
- **`docs/reviews/path_forward_assessment_2026-06-01.md`** (new, Phase 6) — post-wind-down operator decision paper.
- **`docs/reviews/strategy_charter_v4_2026-06-01.md`** (new, Phase 6) — technical companion charter (V27 spec + mode-flag dispatcher + F&O paper-mode).
- `docs/freeze/verdict_meeting_packet_2026-06-05.md` — assembled in Phase 5c; references this file as ledger. **Modified in Phase 6** (§7 forward-plan note + §8.G appendix).
- `FREEZE_v2.1.md` — frozen-file list. Cross-checked 2026-06-01 11:25 IST: `packages/core/charges.py` is **not** enumerated; CHG diff is freeze-safe.
- `logs/backtests/battery_chg_recompute_20260601T114500/` — V25-AngelOne + V26 true re-sim (in flight at time of writing).
- Commit hashes (origin/main): `e277e21`, `0d541ed`, `4a00e82`, `4e381a7`, `52f650c`, `22434cd`, `135d749`. **Phase 6 files: uncommitted at time of writing — operator follow-up #4.**

---

## Phase 8 — V27 sensitivity, engine extension, dispatcher skeleton (2026-06-01, 14:09-16:30 IST)

Continuation of Phase 7's v4 Mode A scaffolding. Operator directed "A → B → C" sequential after the V27 first-cut underperformance landed. Three discrete commits + one writeup.

### 8.A — V27-no-benchmark sensitivity test (commit `7d693cd`)

**Hypothesis tested:** V27's CAGR underperformance vs NIFTYBEES (+1.25% vs +8.98%) might be caused by risk-parity's low-vol preference loading NIFTYBEES + JUNIORBEES + BANKBEES heavily into the portfolio, leaving little capital for higher-alpha individual names ("self-cannibalization"). If true, excluding broad ETFs from the signal candidate set should close >5pp of the gap.

**Method:** Added `--exclude <CSV>` flag to `tools/v27_backtest_2026_06_01.py`. Symbols in the list are KEPT in `history` (so the benchmark + risk-parity sigma references remain intact) but EXCLUDED from the entry-signal candidate set. Excluded: NIFTYBEES, JUNIORBEES, BANKBEES, NIFTYIETF.

**Result (`logs/backtests/v27_no_benchmark_2026_06_01/`):**

| Metric | V27 first-cut | V27 no-benchmark | Δ |
|---|---:|---:|---:|
| CAGR | +1.25% | **+1.02%** | -0.23pp (worse) |
| PF | 1.10 | **1.08** | -0.02 |
| Win rate | 36.9% | 37.2% | +0.3pp |
| Max DD | -10.24% | -10.26% | ~flat |
| Trades | 314 | 312 | -2 |

**Verdict: HYPOTHESIS REFUTED.** Removing the broad ETFs from the signal candidate set produced *slightly worse* CAGR + PF, not better. Mode A's edge problem is **structural** (Donchian-55/20 + vol-target + risk-parity on Indian equity at AngelOne CNC rates), not allocational.

**Implication:** V28/V29/V30 single-parameter retunes on the SAME spec are unlikely to close the 7.7pp CAGR gap. The two honest paths forward are:
  (a) Concede A3 on Mode A; pivot v4 to a fresh hypothesis (weekly bars / sector rotation / momentum-breadth)
  (b) Burn the full V28-V30 retune budget on actual param changes — but expectations should be low

Operator decision deferred to post-Friday verdict. No V28 initiated without explicit directive.

### 8.B — BacktestConfig.sizer extension (commit `d572332`)

**Purpose:** Make V27+ Mode A swing variants runnable through the existing `EnsembleBacktester` (battery infra) instead of the standalone tool. Required for the Mode A pin tests (`tests/integration/test_mode_a_v27_pin.py`, charter §7.1) and for any future V-variant the battery harness will sweep.

**Schema additions** (`packages/research/backtest_ensemble.py:BacktestConfig`):

```python
sizer: str = "legacy"                     # default = v2.1 behaviour
vol_target_risk_pct: float = 0.5          # charter §3.3
vol_target_max_position_pct: float = 8.0  # charter §3.3
```

**Runtime dispatch** at the single sizing site (~line 858):
- `sizer == "vol_target"` → `core.signals.volatility_sizer.vol_target_size`
- else → `rm.calculate_position_size` (legacy, unchanged)

The `vol_target` branch computes portfolio equity using the mark-to-cost convention already established by `Portfolio._persist_state_after_event` (~line 771).

**Freeze contract (FREEZE_v2.1.md):**
- `RiskManager` UNTOUCHED — legacy path still calls `rm.calculate_position_size` identically.
- `BacktestConfig` added FIELDS only with defaults that preserve byte-identical V1-V26 reproducibility.
- `core.signals.volatility_sizer` was added in Phase 7 (commit `2b3088e`) as a NEW module.

**Allocator (risk-parity across firing candidates) DEFERRED.** The current per-bar loop processes signals one-at-a-time as they fire; a true portfolio-allocator pass requires a control-flow refactor (collect-all-firing-signals → allocate → size → execute) that is out of scope for today. For now, multi-symbol allocation stays inside `tools/v27_backtest_2026_06_01.py`. Tracked as v4-backlog item.

**Tests (`tests/unit/test_engine_sizer_extension_2026_06_01.py`):** 11/11 PASS.
- Defaults pinned (sizer == "legacy", risk_pct == 0.5, max_pct == 8.0)
- Dispatch correctness (vol_target → vol_target_size; legacy → rm.calculate_position_size)
- Unknown sizer name → defensive fall-back to legacy (no silent zero-shares)
- Import surface stable

**Regression sweep:** 182/182 backtest/sizer/sizing tests still PASS.

### 8.C — ModeDispatcher skeleton (commit `b381012`)

**Purpose:** Land the mode-flag enforcement layer per charter §2 (Q5: hard cutover after Phase 1 passes). SKELETON only — the full `route_order` + `kill_check` + `PaperBroker` land in the hard-cutover commit, after Phase 1 backtest passes.

**New file:** `packages/trader/mode_dispatcher.py` (~470 lines).

**Implemented (33 contract tests cover):**
- `ModeSpec` dataclass — typed parse of one `strategies.modes.*` entry
- Schema validation: `mode ∈ {backtest_only, paper, live}`; `runtime ∈ {swing_cnc, swing_fno_carry, intraday_fno_options, intraday_cash}`; enabled modes require `signal_module` + `cost_model` + `backtester_variant`; disabled-legacy modes (e.g. `swing_combined_shorts_legacy`) allowed to omit them
- **Capital gate** (charter §2.3): refuses `mode: live` if cash_inr < threshold
- **Verbatim override** "I accept ruin risk" — case-sensitive, whitespace-sensitive, logs CRITICAL audit line on use
- **Allocation sum gate**: sum of `capital_allocation_pct` of enabled paper+live modes must be ≤ `mode_router.max_capital_allocation_pct`. `backtest_only` modes don't count.
- **Module resolution**: `cost_model` / `signal_module` strings of form `a.b.c` or `a.b.c:Symbol` resolved via `importlib` (injectable resolver for tests)
- `active_modes()` — stable insertion-order
- `disable_mode(name, reason)` — operator-callable kill switch, in-memory toggle, CRITICAL [MODE-DISABLED] audit log

**Deferred (lands in hard-cutover commit):**
- `route_order()`: skeleton enforces structural rule (backtest_only modes never route, disabled modes never route, unknown modes raise KeyError) then raises NotImplementedError. Needs PaperBroker + live-broker adapter wiring.
- `kill_check()`: skeleton validates window + presence of criteria then raises NotImplementedError. Needs equity_curve DB rolling-window reader.
- NO `config.yaml` modification. Dispatcher accepts an in-memory dict; until the cutover commit lands a `strategies.modes` block in the live config, the dispatcher only runs against test fixtures.
- NO `mode_tag` DB migration (charter §7.6).
- NO PaperBroker (separate file in cutover commit, per charter §2.4).

**Pod boundaries:** `packages/trader/__init__.py` says imports allowed = core, strategies, brokers; forbidden = research, ui, training. `mode_dispatcher.py` imports only `importlib` + stdlib at module level; runtime resolution targets `packages.core.*` / `packages.strategies.*`. Pod-boundary test PASSES.

**Freeze contract:** NEW file in pod-internal namespace; no FREEZE_v2.1 enumerated file modified. Not yet wired into `trading_agent.py`, so no live-behaviour change is possible from this commit.

**Tests (`tests/unit/test_mode_dispatcher_2026_06_01.py`):** 33/33 PASS in 0.15s.

### Phase 8 totals

| Bucket | Count |
|---|---:|
| Commits this phase | 3 (`7d693cd`, `d572332`, `b381012`) + writeup |
| New files | 4 (`tools/v27_backtest_2026_06_01.py` modified; `tests/unit/test_engine_sizer_extension_2026_06_01.py` new; `packages/trader/mode_dispatcher.py` new; `tests/unit/test_mode_dispatcher_2026_06_01.py` new) |
| Modified freeze-safe files | 2 (`tools/v27_backtest_2026_06_01.py`, `packages/research/backtest_ensemble.py`) |
| New tests | 44 (11 sizer + 33 dispatcher) — all PASS |
| Full-suite regression | 2166/2166 PASS in 73.4s |
| Frozen files touched | **0** (`RiskManager`, `Portfolio`, `charges.py`, `position_sizer.py` all UNTOUCHED) |
| Live-behavior changes | **0** (dispatcher not wired; engine default sizer == "legacy") |

### Decision surface restated for the operator (POST-PHASE-9 UPDATE)

V27 first-cut: A2/A3 (CAGR underperforms NIFTYBEES by 7.7pp; PF 1.10).
V27-no-benchmark: A2/A3 (slightly worse; structural edge problem confirmed).

**Friday verdict still applies.** Mode A backtest evidence as of today does NOT clear the gate for 06-08 paper-mode flip. The standing condition ("if good backtest → paper Monday") is not met. **V30 (max_concurrent=8) is now the best Mode A variant, and it still fails the charter §3.10 CAGR-vs-benchmark gate by 7.11pp** (see Phase 9 below).

## Phase 9 — V28/V29/V30/V31 retune sweep (2026-06-01, 14:40-16:30 IST)

Operator directive: "burn V28+V29+V30 retune budget". Executed via the standalone tool (`tools/v27_backtest_2026_06_01.py`) — kept apples-to-apples with V27 first-cut by preserving the risk-parity allocator (which the engine sizer-only path doesn't have yet).

### 9.0 — CLI flag additions (no commit yet; lands with the result commit)

Added 4 flags to the standalone backtester:
- `--entry-n` (Donchian entry window; V27 default 55)
- `--exit-m` (Donchian exit window; V27 default 20)
- `--chandelier-mult` (trailing-stop ATR multiplier; V27 default 3.0)
- `--max-concurrent` (max concurrent positions; V27 default 12)

All 4 default to `None` → V27Params dataclass defaults preserved. No regression risk.

### 9.1 — V28: entry_n=100 (longer breakout window)

**Hypothesis:** Longer breakout = fewer false signals = stronger surviving trends.

**Result:** WORSE on all metrics.
- CAGR +1.25% → **+0.13%** (regression of 1.12pp)
- PF 1.10 → **1.01** (basically random)
- Max DD -10.24% → -12.07%
- Trades 314 → 294 (-6%; few signals pruned that mattered)

**Read:** V27's other filters (volume, ADX, regime) already do the breakout-quality work. Lengthening the Donchian window itself doesn't add information at daily-bar resolution.

### 9.2 — V29: chandelier_mult=2.5 (tighter trailing stop)

**Hypothesis:** Tighter trail = faster loss-cutting = better win/loss asymmetry.

**Result:** **NET LOSS.**
- CAGR +1.25% → **-1.46%** (regression of 2.71pp)
- PF 1.10 → **0.88** (the strategy loses money)
- Trades 314 → **371** (+18%; whipsaw confirmed)
- Charges ₹14.5k → ₹17.1k (+18% charge burn from re-entries)

**Read:** 2.5×ATR trail is INSIDE the normal volatility envelope of Indian equity. The V27 default of 3.0 is at or near optimal. Tightening is destructive.

### 9.3 — V30: max_concurrent=8 (concentration)

**Hypothesis:** Fewer slots → more capital per trade → bigger wins on the trades that work.

**Result:** **THE SOLE POSITIVE RETUNE.** Better on all three primary metrics:
- CAGR +1.25% → **+1.87%** (+0.62pp)
- PF 1.10 → **1.19** (+0.09; **0.01 short of the charter §3.10 PF gate of 1.20**)
- Max DD -10.24% → **-8.55%** (1.69pp improvement)
- Per-trade P&L: V27 ₹12.6/trade → **V30 ₹31.9/trade (~2.5×)**

**Caveat:** Concentration likely loads risk-parity even harder into low-vol broad ETFs (NIFTYBEES, JUNIORBEES, BANKBEES). V30 may be a worse-disguised version of NIFTYBEES buy-and-hold. Per-symbol P&L attribution would confirm/refute.

### 9.4 — V31: all three combined

**Hypothesis:** Stack the retunes additively.

**Result:** REGRESSES.
- CAGR -0.32% (between V29 and V28)
- PF 0.96

**Read:** Additive-retune hypothesis FAILS. V28 + V29 negatives wash out V30 positive.

### 9.5 — Charter §3.10 verdict against the V30 candidate

| Gate | Threshold | V30 actual | Verdict |
|---|---:|---:|---|
| `pf_min` | ≥ 1.20 | 1.19 | **MISS by 0.01** |
| `cagr_vs_niftybees_min_pct` | ≥ +2.0pp | **-7.11pp** | **FAIL by 9.11pp** |
| `maxdd_max_pct` | ≤ 25.0% | 8.55% | PASS (way under) |

V30 is **close on PF, fails CAGR-vs-benchmark badly**. The CAGR gate is binding, not PF.

### 9.6 — Recommended next steps (operator decision)

(a) **Concentration sweep.** V32 (maxc=6) + V33 (maxc=4) — ~30 min total. If one crosses PF 1.20, we have a viable Mode A candidate that passes PF (but still fails CAGR-vs-benchmark).

(b) **Per-symbol attribution.** Spend ~15 min reading V30's trades.csv to confirm/refute the "concentration into benchmark" hypothesis. Important to know BEFORE deploying.

(c) **Concede A3 on Mode A.** All four retunes tested; only concentration helps and only marginally. Mark Mode A as A3 and pivot v4 to a different hypothesis.

(d) **Park until Friday verdict.** Mode A's fate may be moot depending on the wind-down verdict.

### Phase 9 totals

| Bucket | Count |
|---|---:|
| Backtest runs | 4 (V28, V29, V30, V31) |
| Tool change | 4 new CLI flags on standalone backtester |
| Result artefacts | 4 dirs in `logs/backtests/v27_*_2026_06_01/` (force-added) |
| Findings doc | `docs/findings/v28_v31_retune_results_2026-06-01.md` |
| Frozen files touched | **0** |
| Live-behavior changes | **0** |
| Total commits this day | 11 (Phase 7 + 8 + 9) |
| Best Mode A variant found | V30 (max_concurrent=8) — still fails CAGR gate by 7.11pp |

## Phase 10 — Concentration sweep + V32 attribution (2026-06-01, 16:35-17:30 IST)

Operator directive: "concentration sweep V32 max_c=6, V33 max_c=4". Executed. Bonus: ran per-symbol P&L attribution on V32 to settle the "closet-indexing" question.

### 10.1 — V32: max_concurrent=6 (further concentration)

**Hypothesis:** V30 (max_c=8) was the only positive retune. Push concentration further.

**Result:** **NEW BEST VARIANT. Crosses charter §3.10 PF gate.**
- CAGR +1.87% (V30) → **+2.84%** (+0.97pp vs V30; +1.59pp vs V27 first-cut)
- PF 1.19 (V30) → **1.36** (**PASSES charter gate of 1.20** with +0.16 margin)
- Max DD -8.55% → **-7.80%** (best yet)
- Per-trade P&L: V27 ₹12.6 → V30 ₹31.9 → **V32 ₹66.4** (5.3× V27)
- Trades: V27 314 → V30 239 → V32 180

### 10.2 — V33: max_concurrent=4 (most concentrated)

**Hypothesis:** Continue the sweep — does V32 peak, or does concentration keep helping?

**Result:** **SLIGHT REGRESSION. V32 is the local CAGR peak.**
- CAGR V32 +2.84% → **V33 +2.32%** (-0.52pp)
- PF V32 1.36 → V33 1.36 (identical; still PASSES)
- Per-trade P&L V32 ₹66.4 → V33 ₹74.7 (better per trade)
- Trades V32 180 → V33 130 (-28%)

**Read:** Per-trade economics improve monotonically as concentration tightens, but total trades drop faster below max_c=6. V32 is the local CAGR optimum.

### 10.3 — V32 per-symbol attribution (the "closet-indexing" diagnostic)

The critical question: does V32's edge come from individual stock picks, or is risk-parity loading heavily into broad ETFs (NIFTYBEES, JUNIORBEES, BANKBEES) making V32 a closet-indexed NIFTYBEES?

Built `tools/_v32_attribution_2026_06_01.py` to group V32's 180 trades by symbol and instrument bucket.

**Result (180 trades, ₹11,955 total net P&L):**

| Bucket | Symbols | Trades | Net P&L | % of total |
|---|---:|---:|---:|---:|
| **Individual stocks** | 57 | 159 | **₹8,145** | **68.1%** |
| Commodity ETFs (SILVERBEES, GOLDBEES) | 2 | 10 | ₹3,761 | 31.5% |
| Broad ETFs (NIFTYBEES + JUNIORBEES + BANKBEES + NIFTYIETF) | 2 | 5 | ₹424 | **3.5%** |
| Sector ETFs | 2 | 6 | -₹374 | -3.1% |

**Top contributors:** IOC (₹5,183, 43%), SILVERBEES (₹3,289, 28%), ADANIGREEN (₹2,558, 21%), M&M (11%), HAVELLS (11%), HDFCLIFE (9%), APOLLOHOSP (8%), BAJFINANCE (8%), BHARTIARTL (7%), ZYDUSLIFE (6%).

**Top losers:** TATASTEEL (-12%), ADANIENT (-11%), JSWSTEEL (-9%), ADANIPORTS (-8%), PIDILITIND (-7%).

**CLOSET-INDEXING HYPOTHESIS: REFUTED.** Broad ETFs contribute only 3.5% of net P&L; IOC alone outweighs all 4 broad ETFs combined by 12×. V32 has genuine individual-stock-picking edge.

**Concentration risk noted:** Top winners + top losers both cluster in commodity/energy/Adani exposures. Future variant could test sector caps (charter §3.6 calls for max 3 per sector but standalone tool doesn't enforce).

### 10.4 — Charter §3.10 verdict against V32

| Gate | Threshold | V32 actual | Verdict |
|---|---:|---:|---|
| `pf_min` | ≥ 1.20 | **1.36** | **PASS** (+0.16) |
| `cagr_vs_niftybees_min_pct` | ≥ +2.0pp | -6.14pp | **FAIL** (by 8.14pp) |
| `maxdd_max_pct` | ≤ 25.0% | 7.80% | PASS (way under) |

**V32 passes 2 of 3 charter gates.** PF and Max DD clear comfortably. CAGR-vs-benchmark is the binding constraint and still fails.

**Risk-adjusted view** (CAGR / |Max DD|):
- NIFTYBEES: 0.59
- V32: 0.36

V32's risk-adjusted return is still lower than NIFTYBEES, but the gap is much smaller than raw CAGR suggests. NIFTYBEES has ~2× the drawdown of V32.

### Phase 10 totals

| Bucket | Count |
|---|---:|
| Backtest runs | 2 (V32, V33) |
| Attribution tool | 1 (`tools/_v32_attribution_2026_06_01.py`) |
| Result artefacts | 2 dirs in `logs/backtests/v27_v3*_2026_06_01/` (force-added) |
| Findings doc | Updated `docs/findings/v28_v31_retune_results_2026-06-01.md` |
| Frozen files touched | **0** |
| Live-behavior changes | **0** |
| Total commits this day (Phases 7+8+9+10) | 12 + this one = **13** |
| **Best Mode A variant** | **V32 (max_concurrent=6)** — 2/3 gates PASS, CAGR-gate FAIL by 6.14pp, NOT closet-indexing |

### Decision surface restated for the operator (POST-PHASE-10 UPDATE)

V32 is genuinely the best Mode A candidate we've found. It:
- PASSES the PF gate (1.36 > 1.20)
- PASSES the Max DD gate (-7.80% < -25.0% threshold, with massive margin)
- FAILS the CAGR-vs-benchmark gate (-6.14pp vs required +2.0pp)
- Has REAL stock-picking edge (68% of P&L from individual names)
- Has concentration risk (~10 names dominate)

The CAGR-vs-benchmark gate is the policy choice. Three paths:

(a) **Accept V32 as best; refresh charter** — propose §3.10 amendment to soften the CAGR-vs-benchmark gate (e.g. "underperformance OK if Max DD is half-or-better than benchmark"). Then V32 could deploy paper-mode.

(b) **Reframe V32's purpose** — instead of "beat NIFTYBEES", deploy V32 alongside an explicit NIFTYBEES core allocation. V32 becomes the diversifier (lower DD, uncorrelated stock picks) and NIFTYBEES provides the beta exposure. No charter change needed; just a reframing of what Mode A is FOR.

(c) **Concede A3** — charter §3.10 is binding; V32 fails CAGR gate; mark Mode A as A3 and pivot v4.

(d) **Park until Friday verdict** — 8 Mode A data points in the packet. Decide weekend.

(e) **Run sector-cap test** — charter §3.6 prescribed max 3 per sector but standalone tool ignores it. A V34 with sector cap enforced could reduce concentration risk + potentially improve CAGR. ~45 min dev + 1 backtest.

---

> _Filed under the `changes-done` skill convention. This document is
> the verdict-meeting ledger for the CHG-and-prep work; the brutal
> review is the verdict-meeting adversarial record; the findings log
> is the verdict-meeting bug ledger; together they comprise the
> 2026-06-05 packet from the "what changed in the final week" angle._
