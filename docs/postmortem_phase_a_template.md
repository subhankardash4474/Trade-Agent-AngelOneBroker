# Phase A Postmortem — TEMPLATE (do NOT skip the questions)

> **Status:** TEMPLATE. Fill out only if Branch C fires
> (`docs/FREEZE_v2.1.md` §Forward-looking verdict — Branch C: PF stays
> < 0.8 across portfolio, no strategy at lower-CI > 1.0, or battery and
> live disagree fundamentally).
>
> **Promise to future-self:** if Branch C fires, you will fill out THIS
> document with honest, evidence-backed answers BEFORE deciding what
> comes next. "Let me try one more thing" is the path that consumes
> another six months of life on a non-edge configuration. The verdict
> already flagged this as the most likely operator failure mode.

---

## Section 0: Header (fill in literally)

- **Date written:** YYYY-MM-DD
- **Phase A window:** 2026-05-18 → YYYY-MM-DD
- **Total closed paper trades:** N
- **Cumulative paper PnL:** Rs ___
- **Cumulative paper PnL excluding contaminated days:** Rs ___
- **Number of contaminated days excluded:** N
- **Daemon uptime % during market minutes:** ___ %
- **Number of `freeze-bypass:` commits during the window:** N (max 3)
- **Active freeze gates at decision time:** original / revised (Branch 1)

---

## Section 1: What was the actual edge picture? (numbers only, no narrative)

Fill out exactly this table from `python -m tools.profit_diagnostic --days 21`:

| Strategy | N | WR % | PF (point) | PF lower-95-CI | Kelly | Expectancy | Long N | Short N | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| breakout |  |  |  |  |  |  |  |  |  |
| ensemble |  |  |  |  |  |  |  |  |  |
| mean_reversion |  |  |  |  |  |  |  |  |  |
| moving_average_crossover |  |  |  |  |  |  |  |  |  |
| opening_range_breakout |  |  |  |  |  |  |  |  |  |
| rsi_momentum |  |  |  |  |  |  |  |  |  |
| vwap_bounce |  |  |  |  |  |  |  |  |  |
| xgboost_classifier |  |  |  |  |  |  |  |  |  |
| lstm_price_model |  |  |  |  |  |  |  |  |  |

If any cell reads "INSUFFICIENT_DATA" you do not get to skip it — write
the N anyway and say "INSUFFICIENT_DATA at N=__".

---

## Section 2: The three failure-mode questions

You must answer **all three** before deciding what comes next. One
paragraph each, evidence-backed.

### 2a. Was it a strategy-edge problem?

The hypothesis: **no edge exists at this timeframe/universe**. The 5-min
NSE intraday strategies do not produce a positive-expectation trade
distribution after charges. The configuration is statistically clean,
but the edge isn't there.

Evidence FOR:
- [ ] No single strategy crosses PF lower-CI > 1.0 even on the battery
  (which is the better statistical instrument).
- [ ] Per-supersector PF is roughly uniform — not a sector concentration
  artefact.
- [ ] Entry-time distribution is uniform — not a time-of-day artefact.
- [ ] Long-side and short-side PF are both < 1.0 — not an asymmetry bug.

Evidence AGAINST:
- [ ] At least one strategy has PF lower-CI > 1.0 on the battery and
  agrees with live within ±20 %.
- [ ] The portfolio PF excluding contaminated days is > 1.0.

Your honest assessment, in one paragraph:

```
(write here)
```

### 2b. Was it a regime-fragility problem?

The hypothesis: **edge exists in some regimes but not the ones that
dominated the window**. The 2026-05-12 → 2026-06-XX window was
overwhelmingly one regime (likely `bear_high_vol` for the early part,
shifting later). The strategies that worked in May 12's all-shorts
window can't find footing in the widened-regimes window. This is not
"no edge" — it's "edge in regimes we didn't sample."

Evidence FOR:
- [ ] Per-regime PF shows clear separation (e.g. PF > 1.5 in
  `bull_low_vol`, PF < 0.5 in `sideways`).
- [ ] Adjacent battery runs on different 90-day windows disagree by
  > 30 % on PF for the same strategy.
- [ ] One regime accounts for > 60 % of the trades in the window.

Evidence AGAINST:
- [ ] Per-regime PF is roughly uniform within ±20 %.
- [ ] Multiple battery windows agree on the per-strategy PF.

Your honest assessment, in one paragraph:

```
(write here)
```

### 2c. Was it an implementation problem (live ≠ backtest)?

The hypothesis: **the live engine doesn't reproduce the backtest engine
in some measurable way**. Slippage, fill timing, missed signals on
WebSocket disconnects, late SL cancels — any of these eat the edge that
the backtester shows.

Evidence FOR:
- [ ] Battery PF > live PF by > 30 % on the same configuration and
  comparable window.
- [ ] Slippage proxy (paper fill price − signal-time price) > 0.15 %.
- [ ] Daemon uptime < 95 %.
- [ ] Audit log shows orphan SL-Ms, missed exits, late entries.

Evidence AGAINST:
- [ ] Battery and live agree within ±20 %.
- [ ] Slippage proxy < 0.05 %.
- [ ] Daemon uptime > 99 %.

Your honest assessment, in one paragraph:

```
(write here)
```

---

## Section 3: The three options (rank-order them, pick one)

You may not skip this section. The point of the freeze was to produce
an answer. The answer carries an obligation.

### Option A — Pivot timeframe / asset class

Same engine, different statistical regime. Move from 5-min NSE intraday
equities to:

- Swing on options (1-3 day holds, weekly expiry)
- Swing on Nifty / Bank Nifty futures
- Hourly intraday on Bank Nifty / Nifty index ETFs

Pros: same codebase, different statistical regime, possibly real edge.
Cons: starting a new validation window. The freeze-v2.1 evidence does
not generalise. Stage 2.1 timeline resets.

When to choose: **2a + 2b are AMBIGUOUS, 2c is NO**. Code is fine, the
chosen problem (5-min equities) just doesn't have edge.

### Option B — Convert to portfolio infrastructure

The trader becomes a **managed signal-emitter, not an autonomous
executor**. It still scans, computes signals, and writes them to a
log — but it does not place orders. Operator places orders manually
based on emitted signals, or another (future) system consumes them.

Pros: P&L ambition lowers (you're paid for signals, not execution),
risk lowers (no automated capital loss), the engineering work survives
as portfolio infrastructure.
Cons: this is a different product. The hypothesis that motivated the
project (autonomous side-income tool) is shelved.

When to choose: **2a is NO, 2b is YES, 2c is YES**. The strategies have
edge in some regimes but the live engine can't capture it reliably.
The diagnostic signal is real even if the execution layer isn't.

### Option C — Wind down

Declare the experiment complete. Archive the repo with a clear final
commit. Move the engineering and operational learning to your
CV/portfolio. The trader is not a P&L vehicle.

Pros: cleanest possible exit. Honest about what was learned. Frees
attention for other projects. The engineering is genuinely good and
that record stands regardless of P&L.
Cons: no income from this work. The 6 months of effort do not produce
the originally-targeted outcome.

When to choose: **2a is YES**. There is no edge at the chosen
configuration, AND attempts to pivot (Option A) would require another
6 months of validation that may also conclude "no edge."

### Your decision

Ranked:

1. ___________________________________________
2. ___________________________________________
3. ___________________________________________

Picked option: ___

Why this option, in one paragraph (evidence from §2 must support it):

```
(write here)
```

---

## Section 4: The thing not to do

Write this section LAST. Do not skip it.

**Anti-pattern:** "Let me try one more thing." Re-enable
`supertrend_follow` with a tighter SL. Retrain the model on the past
30 days. Switch to a different signal source. Add one more risk gate.

If the postmortem above honestly concludes the system has no edge, the
above ideas don't change that. They just produce another 6 weeks of
data that says the same thing.

Write out, in your own words, what "one more thing" you are most
tempted to try right now, and why §1, §2, and §3 rule it out:

```
(write here)
```

This section exists because the verdict explicitly warned about it.
Future-you on a bad Friday will want to skip it. Don't.

---

## Section 5: The successor commitments

If §3 picked Option A or B, define the contract:

- **New validation window length:** ___ weeks
- **New exit gates** (mirror `FREEZE_v2.1.md` style):
  1. ___________________________________________
  2. ___________________________________________
  3. ___________________________________________
- **What stays frozen during the new window:** ___________________
- **Halt criterion for the new window:** ___________________
- **Capital base:** Rs ___ (no add during the new window)

If §3 picked Option C, define the archive:

- **Final commit tag:** `phase-a-archive` or similar
- **README update:** "this project ran from 2026-XX-XX to 2026-XX-XX
  and concluded with the postmortem at `docs/postmortem_phase_a.md`"
- **Repo visibility:** public / private / archived
- **What to do with `data/trading_agent.db`:** snapshot to cold
  storage; do not lose the trade history.

---

## Section 6: Sign-off

This postmortem must be signed-off by the operator (you) **at least
24 hours after writing**. Sleep on it. Re-read in cold blood the next
morning. If the conclusion still holds, sign:

- Written: YYYY-MM-DD HH:MM IST
- Signed: YYYY-MM-DD HH:MM IST (must be >= 24 h after Written)
- Operator: Subhanda

The 24-hour rule exists because §3 is a project-scope decision and
project-scope decisions made on the same day as bad data are
statistically the worst kind of decisions.

---

*Template author: Trading Agent dev (Subhanda) + Claude.*
*Drafted: 2026-05-19, pre-Phase-A-conclusion, deliberately while calm.*
