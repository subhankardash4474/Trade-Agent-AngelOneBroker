# Freeze v3.0 — Charter, Pre-Committed 2026-05-30 (v1.1)

> **For the 2026-06-05 verdict meeting, read [`wind_down_criteria_2026-06-05.md`](wind_down_criteria_2026-06-05.md) first.** This charter activates IF that verdict is "wind-down-of-v2.1-hypothesis" (the most likely outcome per current data). It is the equivalent of FREEZE_v2.1.md but for the new hypothesis, written BEFORE the v2.1 verdict so the v3 framing can't be result-driven by what slot #4 produces.

**Pre-commit timestamps:**

* **v1.0** — 2026-05-30 ~01:30 IST. Initial charter. Hypothesis, two rules, sizing, what-dies-vs-keeps, pre-committed gates, slot ledger, anti-temptation watch.
* **v1.1** — 2026-05-30 ~02:00 IST (THIS REVISION). Refines §6 into the granular Phase A1-A5 backtester-first structure proposed by the operator's advisor at 01:36 IST. Adds the explicit "trader VM untouched during Phase A" frozen-surface rule (§6.1), the 6-condition hard gate to Phase B (§7.1), the discipline list (§9.1), and the two non-obvious risks for daily-timeframe backtester work (§10.5). Net change: Phase A starts 2026-05-30 Sat afternoon (not post-2026-06-05); trader VM is in museum mode until Phase B; v2.1 verdict process is unaffected.

Both versions are pre-committed BEFORE slot #4 result lands (slot #4 finishes 2026-05-30 ~05:00-08:00 IST). Pre-commit discipline is the point.

**Author:** trading agent + operator joint commitment.

**Status:** ACTIVE pre-commit. Phase A activates 2026-05-30 Sat afternoon (backtester-only, trader VM untouched). Phases B and C activate IF the 2026-06-05 verdict goes wind-down-of-v2.1 AND the §7.1 hard gate to Phase B is met. If by surprise T1+T2+T3 of v2.1 all show edge on 2026-06-05, the v3-swing branch is shelved (preserved in git as audit trail) and v2.1 continues.

### Reconciliation with `wind_down_criteria_2026-06-05.md` §3

`wind_down_criteria_2026-06-05.md` §3 commits us to "**no architectural pivot (v3 timeframe-shift) before the wind-down decision is rendered.**" That commitment is intact and binding. It refers to **code pivots** — branch creation, broker product-type changes, strategy file edits, candle-frame switches. None of those happen until 2026-06-05 verdict.

This charter is **doc-only**. It pre-commits the v3 *framing* on 2026-05-30, before slot #4 lands, for the same reason the wind-down sheet was pre-committed on 2026-05-29 before slot #4 lands: **once the focus run produces a number, every framing written afterwards will be unconsciously calibrated to that number.** If we wait until after 2026-06-05 to write the v3 charter, we'll write it under the influence of slot #4's actual PF — and the operator's emotional state on verdict day. Pre-commit is what prevents that.

The wind-down sheet itself remains a frozen pre-commit. It is not amended by this charter; amending it would itself be the failure mode it exists to prevent. The two documents coexist: the wind-down sheet is the gate, this charter is what passes through the gate IF the gate opens.

---

## 0. The single hypothesis

> **A simple two-rule swing system on 30 liquid Nifty 50 largecaps, holding 3-10 days under CNC delivery, can produce 2-4% monthly net return on small capital after costs.**

One sentence. One hypothesis. No ML, no ensemble blending, no clever combinators. The test is whether this specific thesis survives a 180-day backtest gate, then a 5-day paper-mode gate, then a 30-day ₹25k-seed live gate. Each gate is binary. Failure at any gate kills v3.

---

## 1. Why this and why not "v2.1 + lessons"

v2.1 produced four settled findings that constrain v3:

1. **5-min XGBoost direction prediction on Indian-equity TA features.** AUC 0.49 on 271k clean samples with all 7 known pipeline bugs fixed. Features have ~zero information content for this problem at this horizon. Not retrain-able.

2. **Multi-strategy intraday-MIS ensemble on 232 NSE stocks.** Best variant V4 PF 0.84 over 60d. Every variant V1-V19 net-negative on the production universe.

3. **Short-side trading on this universe under MIS.** Structural -EV across both filter-on and filter-off regimes (-₹379 / -₹398 over 90d battery).

4. **5-min MIS commission drag dominates P&L at retail sizes.** v2.1 EOD diagnostics show commission as 76-146% of |monthly PnL| at PF 1.3. The cost stack is the binding constraint, not the strategy.

Finding #4 is the dominant insight and the reason "fix v2.1 incrementally" is structurally impossible. **Even a hypothetically successful v2.1 (say PF 1.3) would have produced no net income for the operator at retail capital sizes.** The cost regime is impossible to overcome at 5-min MIS without institutional sizing.

v3 changes the cost regime. Daily CNC drops commission drag from ~80% to 5-15%. That single change is the dominant lever. Strategy choice is secondary; the cost regime change is what turns the math from negative-EV to potentially positive-EV before strategy edge enters the picture.

---

## 2. The two rules — verbatim, no clever extensions

### Rule 1 — Trend pullback (the workhorse)

* Stock is in an uptrend: daily close > 200-day SMA AND > 50-day SMA.
* Pulled back to a buyable zone: daily close within 2% of 20-day SMA, with RSI(14) between 40-55 (cooled but not broken).
* Volume confirmation: today's volume ≥ 80% of 20-day average (no panic-low-volume pullbacks).
* **Entry:** next day open.
* **SL:** 3% below entry.
* **TP:** 8% target OR exit on breach below 50-DMA, whichever fires first.
* **Trail:** lock 50% of unrealised P&L once trade is +5% open.

### Rule 2 — 20-day high breakout (the kicker)

* Stock breaks above 20-day high on a day with volume ≥ 1.5× the 20-day average.
* Stock is above 50-DMA (no breakouts of downtrending stocks).
* ADX(14) > 20 (trending environment).
* **Entry:** next day open.
* **SL:** 4% below entry OR below the breakout-day low, whichever is tighter.
* **TP:** 12%.
* **Trail:** lock 50% of unrealised once +6%.

That's the entire trading logic. No XGBoost. No ensemble vote. No regime classifier (the daily SMA filter is the regime classifier). No intraday timing. No 5-min bars. No ATR floors. No opening lockouts. Daily decision, daily order placement at next open, multi-day hold, CNC delivery.

---

## 3. Universe

**Top 30 by 60-day average traded value from Nifty 50.** Snapshot taken at v3 charter commit (2026-05-30) and refreshed quarterly. Snapshot persisted in `data/v3_universe_top30.txt`.

The 30 will mostly overlap with F&O underlyings by virtue of being liquid; this is a coincidence of liquidity, not a strategy choice. We trade the cash equity (CNC delivery), NOT the F&O contract. The "no F&O" operator constraint is honoured.

No smallcaps, no penny stocks, no manual additions. Universe refresh = quarterly recompute by 60d ADTV; manual override only in writing with explicit reason.

---

## 4. Sizing + capital scaling phases

* **Per trade:** 5-8% of capital.
* **Max concurrent positions:** 5.
* **Leverage:** none (CNC = full payment).

Capital scaling — do NOT iterate strategy while scaling capital:

| Phase | Capital | Per-trade | Max concurrent | Trigger to advance |
|---|---:|---|---:|---|
| Seed | ₹25k | ₹1.25k-2k | 3 | 30 calendar days live, kill criteria not breached, monthly net return > 0% |
| Scale 1 | ₹100k | ₹5k-8k | 5 | 60 calendar days live (cumulative), rolling 30-trade PF ≥ 1.3 |
| Scale 2 | ₹250k | ₹12.5k-20k | 5 | 90 calendar days live (cumulative), rolling 60-trade PF ≥ 1.4 |
| Scale 3 | ₹500k | ₹25k-40k | 5 | 180 calendar days live (cumulative), rolling 100-trade PF ≥ 1.5 |

**Honest expected returns** (per published swing-on-largecap-Nifty literature, base case PF 1.6 / WR 50% / RR 1.8):

| Capital | Monthly net return (base case) |
|---:|---:|
| ₹25k (seed) | ₹250-700 |
| ₹100k (scale 1) | ₹1,000-4,000 |
| ₹500k (scale 3) | ₹5,000-25,000 |

The ₹25k seed produces proof-of-concept, not income. The realistic side-hustle income requires ₹3-5L (Scale 2 → Scale 3). **There is no math by which a ₹10k or ₹25k account produces meaningful monthly income on a PF 1.5 system.** The capital scaling phases are pre-committed so the operator doesn't blow up trying to compress phases together.

**Never iterate strategy while increasing size.** Strategy changes (if any) happen at the start of a phase, never mid-phase. Capital advancement happens at the trigger condition only.

---

## 5. What dies vs what carries forward

| Asset | Action |
|---|---|
| `xgboost_classifier.py` + retrained `.pkl` | DELETE imports; archive files to `packages/strategies/_archive/v2.1/` and `models/_archive/v2.1/`. The temptation goes away with the file. |
| `lstm_model.py`, `mean_reversion.py`, `vwap_bounce.py`, `opening_range_breakout.py`, `supertrend_follow.py` | Archive. Retained for v2.1 historical reproducibility, NOT imported into v3. |
| `rsi_momentum.py` | Rewrite as `trend_pullback.py` per Rule 1. ~150 lines. Old file archived. |
| New: `breakout_20d.py` | NEW (~150 lines) per Rule 2. |
| `prepare_dataset.py`, `train_xgboost.py` | Archive. v3 has no ML; nothing in production imports these. |
| Tick aggregator, WebSocket client, intraday-flat-by-15:15 logic | DELETE. Unused at swing horizon. |
| AngelOne broker integration (Phase-2 audit hardened) | KEEP. Swap order product MIS → DELIVERY. |
| Daemon, watchdog, OCI infra, Docker, dual-VM split | KEEP as-is. |
| Observability stack (EOD diagnostics, audit checkpoints, signal_audit CSV, [REGIME-INPUT] lines, post-mortem template) | KEEP, reduce frequency: per-day not per-minute. |
| Risk gates (circuit breakers, max-positions cap, drawdown limits, sector concentration) | KEEP, retune for swing: -5% daily, -10% weekly drawdown limits. |
| Battery harness | KEEP, re-window: 180d × 30 stocks × daily bars (vs 60d × 232 × 5-min). |
| Freeze-discipline framework (slot ledger, kill criteria template, bypass discipline) | KEEP the meta-process; reset the slot ledger to v3.0. |
| `_market_context` (VIX, Nifty trend) | KEEP as filters only, not as features. |
| Bug M / Bug N / Bug O fixes (commits f74547a, feff629) | KEEP. Already-correct code, deployed to whichever VM v3 runs on. |

The trading code shrinks ~70%. The infrastructure code stays. That's the right ratio for a pivot — keep the expensive-to-build assets, replace only the cheap-to-rewrite logic.

---

## 6. Phase A — backtester-only validation (starts 2026-05-30 Sat afternoon)

Phase A is **backtester-only**. The trader VM is in museum mode (running v2.1 with capital paused) for the entirety of Phase A. Phase A is independent of the 2026-06-05 v2.1 verdict — the verdict measurements (T1, T2, T3) are mechanical against pre-committed thresholds and unaffected by Phase A progress. Phases B and C are gated on **both** the 2026-06-05 verdict going wind-down-of-v2.1 **and** the §7.1 hard gate to Phase B being met.

### Phase A timeline (5 sub-phases, ~6 working days end-to-end)

| Sub-phase | When | Effort | Deliverable |
|---|---|---:|---|
| **A1 — Backtester capability gap analysis** | Sat 2026-05-30 afternoon | 2-3 h | `docs/v3_backtester_gap_analysis.md` listing each v3 requirement vs current backtester support, with effort estimate per gap. |
| **A2 — Backtester capability fixes** | Sun-Mon 2026-05-31 / 06-01 | ~1.5 d | Code changes to make the backtester correctly simulate v3 mechanics (daily candles, CNC product, multi-day holds, next-day-open fills, CNC charges). Each gap landed as its own small commit with a unit test + a regression test that confirms a v2.1 5-min variant remains byte-identical. |
| **A3 — v3 strategy implementation** | Tue 2026-06-02 | ~1 d | `packages/strategies/trend_pullback.py` (Rule 1, ~150 lines) + `packages/strategies/breakout_20d.py` (Rule 2, ~120 lines). Each with entry-condition unit tests, SL/TP/trail math tests, and a 30-day fixture integration test that confirms ≥1 trade fires. |
| **A4 — Battery variants for v3** | Tue 2026-06-02 afternoon | ~3 h | 5 variants in `packages/research/battery.py`: V20 (Rule 1 alone), V21 (Rule 2 alone), V22 (combined), V23 (combined, looser RSI 35-60), V24 (combined, tighter RSI 42-50). New universe fixture `tests/fixtures/nifty30_v3_universe.json` with `valid_from`/`valid_to` per stock. New entry in `data/battery_queue.yaml`. |
| **A5 — Run + read + walk-forward** | Wed-Thu 2026-06-03 / 06-04 | ~1.5 d | Slot launches. 5 variants × 30 stocks × 180 days × daily bars. Estimated runtime ~30-90 min on workers=2 (vs 14h+ for 5-min equivalent). Walk-forward = first 120d train + last 60d holdout, two cutoffs. Read results against §7.1 gate. |

### Phase A expected outcomes (three buckets, decide mechanically)

* **All 5 variants land PF ≥ 1.5 with ≥ 30 trades each.** Strong evidence; proceed to walk-forward and then Phase B.
* **V22 (combined) lands PF 1.0-1.5 but V20 or V21 alone lands PF ≥ 1.5.** One rule is dragging the other; ship the better single rule, drop the worse. Still proceed.
* **All variants PF < 1.0.** Surprise. Either swing CNC isn't the answer or there's a backtester bug surfacing only at daily timeframe. Do **NOT** debug into oblivion — read once, sleep on it, decide Friday whether to try a different rule set or pivot the pivot.

### Phase B — paper-trade live (starts the Mon after §7.1 gate passes)

Activates only when **all six §7.1 conditions** are true. Estimated start: Mon 2026-06-08 if Phase A finishes on schedule.

* Deploy `v3-swing` to trader VM in **paper mode**, CNC product, ₹100k notional, capital pause flag **still on**.
* 5 paper trading days. Expect 2-4 trades.
* Friday end-of-day: read paper-vs-backtest delta.

**Paper-vs-backtest agreement gate (binary, pre-committed):** PF within 15%; per-trade expectancy within 25%. If outside, the H3 entry-lag forensic from v2.1 still applies; do not go live until execution layer validates.

### Phase C — live with ₹25k seed (starts the Mon after Phase B gate passes)

Estimated start: Mon 2026-06-15. Live mode enabled with **₹25k seed only** (NOT ₹100k — match what the operator can afford to lose). K1-K4 from §7.2 active from the first trade. 4 supervised live days. End-of-week verdict on advancing to Phase Scale-1 (per §4 trigger conditions).

### Net timeline

~12-15 calendar days from charter v1.1 commit (2026-05-30 ~02:00 IST) to first ₹25k live trade — IF Phase A gates pass cleanly AND v2.1 verdict on 2026-06-05 goes wind-down. If verdict goes v2.1-survives, the `v3-swing` branch shelves; v2.1 continues per its own contract.

## 6.1 Frozen surface during Phase A — the trader VM rule

The trader VM is **untouched during Phase A**. This is the single most important discipline rule of v3.0 because it is the rule v2.1's May-14 panic patch violated.

* No deployment of `v3-swing` to trader VM.
* No "while I'm here" log cleanup, config tweak, or service restart on trader VM.
* No experimentation with broker product types on trader VM.
* No Bug-fix commits to `main` deployed to trader VM during Phase A unless they are P0 incidents specifically about the v2.1 capital-paused trader VM continuing to operate safely.

If Phase A reveals a bug in shared infrastructure (e.g., a `packages/core/portfolio.py` bug surfaced by the new daily-bar path), the **fix lands on backtester VM only**; the equivalent change to trader VM is queued in `docs/v3_trader_vm_pending_changes.md` and gated on Phase A passing the §7.1 hard gate. No exceptions, including for "obvious" fixes, until Phase B kicks off.

The trader VM stays running v2.1 with `strategies.active: ["mean_reversion"]` and `allow_shorts: false` (current state) and capital paused. It is a museum exhibit during Phase A. The audit checkpoints, EOD diagnostics, and monitoring continue running so that any operational anomaly is still caught — but no behavioural changes.

---

## 7. Pre-committed gates

### 7.1 Hard gate from Phase A → Phase B (six conditions, all AND)

Trader VM does not change until **all six** of the following are true. No exceptions, especially if the backtester numbers look exciting (that is when discipline matters most).

1. Backtester slot (V20-V24) produces **PF ≥ 1.5** on at least one variant with **≥ 30 trades** over the 180-day window.
2. **Walk-forward holdout** (last 60d on the same variant) confirms **PF ≥ 1.3**.
3. The Phase A2 backtester capability fixes each have a **regression test**, and the full unit suite is **green** (currently 1,718; should be ~1,750-1,760 after v3 additions).
4. **Bug K** (holdout-flag silently ignored, slice-before-cache reorder) is closed with a unit test.
5. The v3 charter is committed (this doc) and the Phase C kill criteria (K1-K4 in §7.2) are written and reviewed.
6. The operator has **slept on the result for at least one night** before deploying. No same-day "the numbers look great, let's ship" deploys.

If any of those is not true, Phase B does not start, regardless of how clean the rest looks.

### 7.2 Phase C live kill criteria (active from first ₹25k live trade onwards)

* **K1**: cumulative net PnL < -₹2,000 within 30 calendar days → re-pause, re-evaluate. (Not -₹500 like v2.1; swing PnL has higher per-trade variance, so the threshold accounts for natural drawdown.)
* **K2**: any single trade loses > 5% of capital after slippage → SL widening bug, immediate halt.
* **K3**: paper-vs-live delta exceeds 30% over 10 trades → execution-layer bug, immediate halt.
* **K4**: rolling 30-trade PF drops below 1.0 with N ≥ 30 → strategy-layer reality check, re-pause.

K1-K4 are non-negotiable. Re-pause means stop placing new orders, exit existing positions at SL/TP normally, write a re-evaluation document before any unpause.

---

## 8. Slot ledger — fresh for v3.0

v2.1's slot accounting is closed out as part of the verdict. v3.0 starts with **3 freeze slots**. Slot consumption rules are identical to v2.1's spirit:

* Slot consumed = behaviour-changing edit to trader-VM-deployable code under `packages/strategies/v3/*`, `packages/core/risk_manager.py`, `packages/core/portfolio.py`, `trading_agent.py`, `run_daemon.py`.
* Audit-only-semantically-neutral does NOT consume a slot.
* Audit-only-baseline-shifting requires explicit baseline-reset notice in `findings_log_v3.md`.
* 4th slot triggers explicit unfreeze decision, parallel to v2.1's contract.

v3.0 freeze becomes ACTIVE on the first paper-mode deploy (Week 3, Mon June 22). It runs until live capital is paused for any reason OR 90 days post-Phase-Seed-deploy, whichever comes first.

---

## 9. What v3 will NOT do (high-level)

* **Don't keep XGBoost as a future option.** File retired, .pkl archived, import removed. The strongest evidence for retirement is AUC 0.49 on a clean pipeline; the second strongest is "we keep being tempted to retrain it." Adding ML back is a v4 conversation, after v3 has 6 months of live data.
* **Don't carry forward the 6-strategy ensemble.** Two rules above. If edge is found, add a third only after the first two have 100+ trades each on the new config.
* **Don't reuse the freeze-v2.1 contract.** That contract was correct for v2.1's question. v3 has its own slots, exit criteria, kill conditions in this charter.
* **Don't run any "while we figure it out" continued capital deployment.** v2.1 capital pause stays paused until v3 backtest + paper gates pass.
* **Don't simultaneously scale capital and iterate strategy.** Phase rules in §4 above.
* **Don't re-engineer the existing 5-min code "to make it work."** The data settled that question. If the operator finds themselves drafting a "let's try once more at 5-min" plan, re-read §1 finding #4 (cost-math) and stop.

## 9.1 Phase-A specific discipline list (the operationally hardest one)

The strongest pull during Phase A is to do something that feels productive but isn't part of Phase A. Each of these is rejected in advance:

| Tempting move | Why not |
|---|---|
| Touch trader VM "to clean up a stale log" or "to re-check it's still paused correctly" | Trader VM is in museum mode. Even read-only inspection should go through the existing audit-checkpoint pipeline, not direct ssh. The discipline is the rule, not the exception. |
| Run a v2.1 variant on the new backtester capability changes "to see if it still works" | Write a regression test instead. The test is cheap and reusable; the manual run is a distraction that introduces sample-of-one anchoring on whatever the manual-run output is. |
| Add a third strategy "just in case the two rules don't work" | Two strategies is the experiment. If two fail, three fail. Iterate on the rules, not the count. Adding a third is the v2.1 "ensemble of 4" failure pattern reasserting itself. |
| Iterate on rule thresholds to fit the backtest result (e.g., V22 lands PF 1.2 → push RSI window to 42-48 to "rescue" PF 1.5) | Curve-fitting. The walk-forward holdout (§6 A5) will catch you. If V22 < 1.5, accept it: ship the better single rule (V20 or V21) or don't ship. |
| Retrain the now-archived XGBoost on daily bars "while we're at it" | v4 conversation. v3 has no ML by design. The retrain temptation is exactly why the .pkl gets archived rather than left in place. |
| Read slot #4 progress every hour Saturday morning | The data lands when it lands (~05-08 IST). Watching faster does not improve it. Read once when fresh; record T1 verdict; move to A1 gap analysis. |
| "Just one more variant" after V20-V24 land | The five variants cover the meaningful parameter space (rule alone × 2, combined × 1, threshold sensitivity × 2). More variants = p-hacking. |
| Deploy `v3-swing` to trader VM "in dry-run mode just to see config loads correctly" | This is what the §7.1 cooling-off requirement (slept on it one night) exists to prevent. Dry-run is just live with the safety off in disguise. |

If you find yourself drafting any of these, re-read this section and pick the disciplined alternative. The pattern that consumed v2.1's three slots was ALWAYS "this one feels productive and harmless" — none of v2.1's slots felt unjustified at the moment they were consumed.

---

## 10. Anti-temptation watch (parallel to wind-down §5)

If between now and the 2026-06-05 verdict — or during v3 build weeks 1-4 — the operator or agent finds themselves constructing reasons to:

* re-introduce ML "as a filter only" → that's how XGBoost came back in v2.1.
* add a third strategy "because it's cheap" → that's the ensemble dilution v2.1 confirmed doesn't work.
* skip the backtest gate "because the rules are well-published" → published edge does not exempt empirical verification on this universe.
* skip the paper gate "because the backtest passed cleanly" → execution-layer bugs are exactly what v2.1's PERF-01/02/14 finding flagged.
* deploy to ₹100k directly "because ₹25k is too small to learn from" → blowing up at ₹100k while learning is exactly what the seed phase exists to prevent.

Re-read this charter. Pick the disciplined action.

---

## 10.5 Two non-obvious risks specific to Phase A

These are not in the v2.1 discipline list because v2.1 didn't run at the daily timeframe. Phase A surfaces them. Both are flagged in advance so they don't get mis-attributed when they appear.

### Risk R1 — Backtester has bugs that only surface at the daily timeframe

v2.1 found 5+ backtester bugs in the 5-min path during the audit sprints (Bug E [O(N²) loop], Bug F [harness cascade-fail], Bug G [hardening], Bug H [xgboost-mount], Bug K [holdout-flag silently ignored]). The daily candle path has been exercised much less. Budget for **1-2 surprise backtester bugs** during A2 / A5 and do not be alarmed when they appear.

**The risk is psychological, not technical.** When a strategy produces a result that doesn't match expectation, the temptation is to assume the bug is in the strategy. In Phase A, the bug is at least as likely to be in the backtester's daily-bar handling. Specific suspect surfaces: SMA / RSI / ADX rolling-window calculations across the 5-min → 1-day resample boundary; CDSL per-day accrual on multi-day holds; gap-up / gap-down handling on entry-day open fills; corporate-action handling (dividend / split / bonus) over multi-day windows.

**Discipline:** when a Phase A result looks wrong, write a unit test that hand-computes the expected output for a 3-day fixture before touching strategy code. If the unit test fails, fix the backtester. If the unit test passes, fix the strategy.

### Risk R2 — Survivorship bias in the universe

"Top 30 by 60-day average traded value as of today" conditions on stocks that are currently large/liquid. A backtest 180 days ago should use the universe **as it was 180 days ago**. For Nifty 30 / Nifty 50 this is a small effect (the index turns over slowly), but it is not zero — and it is precisely the kind of subtle bias that inflates backtest PF without inflating live PF.

**Discipline:** the universe fixture file (`tests/fixtures/nifty30_v3_universe.json`) should ideally have a `valid_from` and `valid_to` field per stock. The battery harness reads the universe at the as-of-date of each backtest day, not the as-of-date of fixture creation. If implementing per-day universe lookup is too expensive for Phase A, use the index composition **as of the start of the backtest window** (2025-12-01-ish for a 180d window ending 2026-05-30) — never the as-of-today snapshot.

If neither approach is feasible in Phase A scope, document the bias explicitly in the gap analysis (A1 deliverable) and apply a haircut: divide reported PF by 1.05 before comparing to the §7.1 gate. Acknowledged bias is recoverable; unacknowledged bias is what causes paper-vs-live divergence.

---

## 11. The single sentence

> Phase A starts Sat afternoon (backtester-only, trader VM untouched). Five sub-phases over ~6 working days produce v3 backtester evidence by Thu/Fri. If §7.1 gate passes AND v2.1 verdict is wind-down, Phase B paper trades start Mon 2026-06-08. First ₹25k live trade lands ~2026-06-15.

---

## 12. Sign-off

| Role | Action | Timestamp |
|---|---|---|
| Operator | Reframed "kill the project" → "kill the v2.1 hypothesis"; proposed swing-CNC pivot with cost-math justification (commission drag drops 10× at swing horizon, which was the binding constraint v2.1 hit, not strategy edge). | 2026-05-30 ~01:11 IST |
| Trading agent | Acknowledged swing-CNC pivot is materially better than the prior A+B+C pick. The cost-math finding (76-146% commission drag at retail MIS) flips the analysis: cost-regime change is the dominant lever, strategy choice is downstream. Committed charter v1.0 pre-write before slot #4 finishes. | 2026-05-30 ~01:30 IST |
| Operator's advisor | Proposed the granular Phase A1-A5 backtester-first structure: gap analysis before strategy implementation; explicit "trader VM untouched" frozen-surface rule; 6-condition hard gate to Phase B; two non-obvious daily-timeframe risks (backtester bugs, survivorship bias). Argued correctly that Phase A can run in parallel with the v2.1 verdict process because the verdict measurements are independent of Phase A progress. | 2026-05-30 ~01:36 IST |
| Trading agent | Bumped charter to v1.1 with the advisor's structure baked in. Phase A activates 2026-05-30 Sat afternoon; trader VM is in museum mode; v2.1 verdict process unaffected. v1.1 still pre-slot-#4 (slot #4 lands ~05:00-08:00 IST). | 2026-05-30 ~02:00 IST |
| Joint pre-commitment | v1.1 is the operating contract for v3. Phase A is unconditional (starts Sat afternoon). Phases B and C are gated on §7.1 + 2026-06-05 verdict going wind-down-of-v2.1. | 2026-05-30 |

**Document version:** v1.1 (2026-05-30). Any further amendment requires explicit operator + agent acknowledgement and a version bump. Versioned diffs preserved in git history as audit trail.
