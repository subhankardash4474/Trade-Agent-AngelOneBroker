# Freeze v3.0 — Charter, Pre-Committed 2026-05-30

> **For the 2026-06-05 verdict meeting, read [`wind_down_criteria_2026-06-05.md`](wind_down_criteria_2026-06-05.md) first.** This charter activates IF that verdict is "wind-down-of-v2.1-hypothesis" (the most likely outcome per current data). It is the equivalent of FREEZE_v2.1.md but for the new hypothesis, written BEFORE the v2.1 verdict so the v3 framing can't be result-driven by what slot #4 produces.

**Pre-commit timestamp:** 2026-05-30 ~01:30 IST (~3.5 hours before slot #4 finishes ~05:00-08:00 IST).

**Author:** trading agent + operator joint commitment.

**Status:** ACTIVE pre-commit. Becomes the operating contract on the day the 2026-06-05 verdict goes wind-down-of-v2.1. If by surprise T1+T2+T3 of v2.1 all show edge, this charter is shelved and v2.1 continues.

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

## 6. 4-week timeline (activates post-2026-06-05 verdict)

This timeline activates the day the verdict goes wind-down-of-v2.1-hypothesis. If verdict goes "v2.1 survives," timeline is shelved.

### Week 1 (June 8-14) — subtractive cleanup + product-type pivot

* Mon: branch `v3-swing` off `main`. Archive the six retiring strategies into `packages/strategies/_archive/v2.1/`. Remove imports.
* Tue: change broker order placement from MIS → DELIVERY. Remove the 15:15 flat-out logic (one function in `trading_agent.py`).
* Wed: rewrite `rsi_momentum.py` → `trend_pullback.py` per Rule 1. New `breakout_20d.py` per Rule 2. Total ~300 lines.
* Thu: switch primary candle frame from 5-min to 1-day. Universe shrink config to top-30. Daily refresh runs once at 09:20 IST; place orders at 09:25 IST.
* Fri: write `tests/unit/test_v3_strategies.py`. ~30 cases (entry conditions, SL/TP math, trail logic, daily-bar boundary semantics).

### Week 2 (June 15-21) — backtest validation

* Mon-Tue: run battery on backtester VM: 30 stocks × 180 days × daily bars × 2 strategies. Should complete in <2 hours given the new candle frame (vs the 14h+ at 5-min).
* Wed: walk-forward — train on first 120 days, holdout on last 60. Walk it twice with different cutoffs.
* Thu-Fri: read results.

**Backtest gate (binary, pre-committed):**
* Rolling PF ≥ 1.5 on at least one rule with ≥ 30 trades over 180 days, AND
* Other rule PF ≥ 0.9 (not actively destroying value).

If both rules fail backtest: **kill v3, reconsider.** Do NOT iterate to "let me try a slightly different rule" — that is exactly the v2.1 failure pattern this charter exists to prevent.

### Week 3 (June 22-28) — paper-trade live

* Mon: deploy v3-swing to trader VM in paper mode with ₹100k notional. CNC product. Live capital pause flag still on.
* Mon-Fri: 5 paper trading days. Expect 2-4 trades. Watch them work or fail.
* Fri: read paper-vs-backtest delta.

**Paper-vs-live agreement gate (binary, pre-committed):**
* PF agreement within 15%
* Per-trade expectancy agreement within 25%

If outside: H3 entry-lag forensic from v2.1 still applies; don't go live until execution layer validates.

### Week 4 (June 29-July 5) — live with ₹25k seed

Activates only if Week 2 + Week 3 gates both pass.

* Mon: enable live mode with **₹25k seed** (NOT ₹100k — match what the operator can afford to lose). Pre-committed kill criteria (§7) written before the first trade fires.
* Mon-Fri: 4 trading days of supervised live. Expect 1-2 trades.
* End of Week 4: verdict on whether to advance to Phase Scale-1 (per §4 trigger conditions).

---

## 7. Pre-committed live kill criteria (active from first live trade onwards)

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

## 9. What v3 will NOT do

* **Don't keep XGBoost as a future option.** File retired, .pkl archived, import removed. The strongest evidence for retirement is AUC 0.49 on a clean pipeline; the second strongest is "we keep being tempted to retrain it." Adding ML back is a v4 conversation, after v3 has 6 months of live data.
* **Don't carry forward the 6-strategy ensemble.** Two rules above. If edge is found, add a third only after the first two have 100+ trades each on the new config.
* **Don't reuse the freeze-v2.1 contract.** That contract was correct for v2.1's question. v3 has its own slots, exit criteria, kill conditions in this charter.
* **Don't run any "while we figure it out" continued capital deployment.** v2.1 capital pause stays paused until v3 backtest + paper gates pass.
* **Don't simultaneously scale capital and iterate strategy.** Phase rules in §4 above.
* **Don't re-engineer the existing 5-min code "to make it work."** The data settled that question. If the operator finds themselves drafting a "let's try once more at 5-min" plan, re-read §1 finding #4 (cost-math) and stop.

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

## 11. The single sentence

> Cut to swing CNC delivery on Nifty 30 in 4 weeks, two simple rules with no ML, reuse 70% of v2.1 infrastructure, accept that real side-hustle income requires ₹3-5L of capital eventually, prove the system at ₹25k seed first.

---

## 12. Sign-off

| Role | Action | Timestamp |
|---|---|---|
| Operator | Reframed "kill the project" → "kill the v2.1 hypothesis"; proposed swing-CNC pivot with cost-math justification (commission drag drops 10× at swing horizon, which was the binding constraint v2.1 hit, not strategy edge). | 2026-05-30 ~01:11 IST |
| Trading agent | Acknowledged swing-CNC pivot is materially better than the prior A+B+C pick. The cost-math finding (76-146% commission drag at retail MIS) flips the analysis: cost-regime change is the dominant lever, strategy choice is downstream. Committed charter pre-write before slot #4 finishes. | 2026-05-30 ~01:30 IST |
| Joint pre-commitment | This charter is the operating contract for v3 IF 2026-06-05 verdict is wind-down-of-v2.1-hypothesis (the most likely outcome per current data). | 2026-05-30 |

**Document version:** v1.0 (2026-05-30). Any amendment requires explicit operator + agent acknowledgement and a version bump. Versioned diffs preserved in git history as audit trail.
