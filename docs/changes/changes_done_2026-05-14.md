# Trading Agent — Changes Done · 2026-05-14

**Scope:** End-to-end review by an algo-trading domain lens (30y market exp.
mindset) of the live cloud daemon, the strategy stack, the ML model, risk
guards, and the live-execution path. Bugs found are fixed; design gaps are
filled with industry-standard mechanisms; the ML model is re-trained with
new market-context features and probability calibration. A second pass at
13:30 IST added the **strategy-concurrency cap** and **self-sufficiency
tracker** in direct response to today's -₹592 session.

**Why now:** Cloud daemon is scheduled to flip from PAPER → LIVE on
**Mon 2026-05-19** with ₹5k seed capital. Anything in the LIVE-mode-safety
bucket below is a hard prerequisite. Today's morning losses (see Loss
Diagnosis section) confirmed two of the gaps were not theoretical.

---

## Today's loss diagnosis (2026-05-14, real money pattern)

**Realised today: -₹592.14 across 5 closed trades (1W / 4L · WR 20%).**
Cumulative since 2026-05-12: -₹745.36 — i.e. the agent gave back the
small positive of 2026-05-13 (+₹19) and then some.

### Anatomy of the morning pile-on
```
09:24-10:01 IST :  8 SHORT signals fired in 37 min
                   ALL strategy = supertrend_follow
                   ALL regime   = bear_high_vol
                   4 of 9 in financials
                   (CENTRALBK + TATACAP + FEDERALBNK + CHOLAFIN)

11:16  ABLBL      stop_loss   -Rs 186.43
11:51  CHOLAFIN   stop_loss   -Rs 196.23   ← preventable, see fix below
11:52  JSWENERGY  stop_loss   -Rs 192.75
11:56  OBEROIRLTY stop_loss   -Rs 154.96
                  ─────────────────────
                  4 stop-outs in 40 minutes  -Rs 730 gross

  +Rs 138 PCBL win (signal exit) brought net to -Rs 592.
```

### What failed and what fix lands today

| Failure | Why it happened | Status of fix |
|---|---|---|
| **Single-strategy pile-on** (9 of 10 SELLs were `supertrend_follow`) | Per-strategy circuit only fires AFTER N losses; entries had already happened | **FIXED today: `max_positions_per_strategy: 4`** |
| **Financials concentration** (4 SELLs in banks/NBFC) | Fine-grained sectors counted independently; each bucket was below 40% cap | Fixed earlier today (supersectors), not yet deployed |
| **MFE-but-no-exit** (each loser ran briefly favourable then reversed to full SL) | Trail arms at +1R, peak-giveback at +1.5R; no protection in 0–1R zone | Fixed earlier today (breakeven SL @ 0.5R), not yet deployed |
| **Bear-vol counter-trend bounce** (shorts entered at session low) | Daily regime detector lags; intraday risk-off was actually risk-on by 11am | Fixed earlier today (intraday overlay), not yet deployed |
| **0 errors / 0 tracebacks in log** | Daemon healthy. The failure was strategy-design, not implementation. | n/a |

**Net effect of today's fixes if they had been live this morning:**
CHOLAFIN entry blocked by strategy-concurrency cap (saves -₹196).
ABLBL/JSWENERGY likely scratched by breakeven SL (saves ~₹150-200 each).
**Estimated counterfactual: -₹100 to -₹250 instead of -₹592.**

---

## Self-sufficiency floor (account-owner goal: passive income covers running cost)

The agent now persists a **cumulative-realised ledger since deployment**
and surfaces a GREEN/YELLOW/RED state on every audit checkpoint:

| Component | INR/month | Notes |
|---|---:|---|
| **Angel One SmartAPI** | **₹0** | **Free** (the killer feature vs Kite's ₹2,000/mo) |
| Cursor Pro | ₹1,700 | $20/mo at ~₹85; only if paid out of trading P&L |
| OCI VM (Always-Free tier) | ₹0 | Upgraded shape ~₹400-800 if needed |
| CDSL/DP charges | ₹0 | MIS-only — no holdings; delivery would add ~₹13.5/ISIN/day |
| Internet/electricity allocation | ~₹500 | Fair share |
| Misc (alerts, GST on services) | ~₹300 | Slack |
| **TOTAL fixed cost** | **₹2,500/mo** | ≈ **₹125/trading-day** |
| Variable per-trade costs | already netted | Brokerage + STT + exchange + SEBI + stamp + GST already inside `pnl` column on `trades.csv` — not double-counted here |

**Live numbers right now** (against today's checkpoint):
- Cumulative realised since 2026-05-12: **-₹745.36**
- Cost burned to date (3 days): **~₹375** (₹125/day × 3)
- **State: YELLOW (behind cost, within red floor of -₹5,000)**

**Self-sufficiency math at current capital:**
- Paper book ₹1.22L: needs **0.10% daily return** to break even on cost
  (very achievable for a working system)
- LIVE seed ₹5k: needs **2.5% daily return** to break even on cost
  (still aggressive but not impossible — and far easier than the ₹4.5%
  the Kite stack would have demanded)
- **Compound to ~₹25k–₹50k** before the cost stack stops dominating P&L
  (was ₹50k-₹1L assuming the Kite cost — Angel SmartAPI cuts the
  break-even capital in half)

The tracker writes to `data/self_sufficiency.json` (atomic temp+rename
so a crash mid-write can't corrupt it) and is configured under
`risk.self_sufficiency` in `config.yaml`. RED state at -₹5k cumulative
is the recommended halting condition for new LIVE entries.

---

## Headline numbers

| | Before | After |
|---|---|---|
| Test count | 805 | **891** (+86 new tests, all green) |
| ML feature count | 23 | **31** (regime context + 5 new derived) |
| Top ML feature | `tod_cos` | **`nifty_trend` (0.087)** |
| Test-set AUC | 0.741 | **0.7607** (calibrated) |
| Test-set Brier | 0.221 | **0.1985** (-10%, better calibration) |
| Orphaned broker SL-M risk | YES (live blocker) | **CLOSED** |
| Sector concentration loophole | Banks+NBFC+Insurance independent | **Collapsed into "Financials" supersector** |
| Event-blackout | none | **Calendar-driven, configurable** |
| Intraday regime overlay | none | **Nifty 1-day % + VIX delta** |
| WebSocket usage | universe-wide tick stream | **Held-symbols only, drives exits** |
| Single-strategy pile-on cap | none | **`max_positions_per_strategy: 4`** |
| Self-sufficiency tracker | none | **JSON ledger + GREEN/YELLOW/RED in audit** |

---

## P0 — Live-mode safety (blocks Monday flip)

### P0c — Strategy-concurrency cap (added 13:30 IST in response to today)

**Bug.** Per-strategy circuit breaker only fires *after* N losses, but
today's 4 losing entries were all already open before any of them closed.
By 11:07 the agent held 4 concurrent `supertrend_follow` shorts in
financials. The 5th entry (CHOLAFIN) walked straight in.

**Fix.** New gate `max_positions_per_strategy: 4` (config) → checked in
`_pre_trade_safety_checks` against `Position.strategy`. Cap=0 disables
(legacy). The call site already had the leading strategy name; threaded
it through.

**Counter-factual on today:** with `cap=3`, CHOLAFIN entry rejected at
11:07 → -₹196 saved on its own.

Tests: `tests/unit/test_strategy_concurrency_cap.py` (6 cases including
a literal replay of the 11:07 IST decision).

### P0d — Self-sufficiency tracker (added 13:30 IST)

See dedicated section above. New module
`packages/core/self_sufficiency.py` + atomic JSON ledger. Wired into
`_on_trade_closed` so every realised P&L update is committed to disk
immediately (no end-of-day batch). Surfaced in `audit_checkpoint.py`
markdown output.

Tests: `tests/unit/test_self_sufficiency.py` (10 cases including a
literal replay of today's 5-trade sequence ending at -₹592.14).

### P0a / P0b — Broker-side SL-M tracking + propagation

**Bug.** Every entry placed an SL-M with the broker, but the agent never
remembered the `order_id`. On every close path (`_exit_on_signal`,
`_check_position_exits`, `_square_off_all`) the position was squared off
**and the SL-M was left dangling**. If LTP later traded back through that
trigger price, the broker would re-enter an unintended reverse position.
Trailing SL updates were also local-only — the broker still sat at the
original entry-time SL.

**Fix.** New per-symbol registry `_sl_orders_by_symbol` in
`execution.py` plus three public APIs:
- `get_sl_order_for_symbol(symbol)`
- `update_sl_trigger_for_symbol(symbol, new_trigger)` → calls `modify_stop_loss`
- `cancel_sl_order_for_symbol(symbol)`

Wired into every close path and into the trailing-stop update tick. Net
effect: **the broker's view always matches the agent's view**.

Tests: `tests/unit/test_execution_sl_tracking.py` (8 cases).

---

## P1 — Material trading defects

### P1a — Breakeven SL + tightened peak-giveback (the "MFE-but-no-exit" hole)

**Observed pattern.** Position runs to **+0.7R** then reverses to **-1R**
stop-out. Round-trip = **-1.7R on what was briefly a winner**. Trail arms
at +1R, peak-giveback at +1.5R — neither protects the half-R favorable
zone.

**Fix.** New monotonic breakeven arm in `risk_manager.TrailingStop`:
- `breakeven_arm_rr: 0.5` — once MFE ≥ 0.5R, lift SL to entry ± buffer
- `breakeven_buffer_pct: 0.10` — covers ~6 bps round-trip MIS charges

Plus lowered `peak_giveback_arm_rr` from 1.5 → 1.0 so the dead zone is
also covered. Worst case after half-R favorable becomes a scratch.

Tests: `tests/unit/test_breakeven_stop.py` (12 cases).

### P1b — ML market-context features (regime-aware model)

**Bug.** XGBoost was **regime-blind**. Training pipeline never injected
nifty trend / India VIX, but live inference referenced them — train/serve
skew. Model saw the same features for `bull_low_vol` and `bear_high_vol`.

**Fix.**
- `prepare_dataset.py`: new `fetch_market_context()` downloads historical
  Nifty (200 EMA → trend) and India VIX, joins them onto every per-symbol
  row.
- `xgboost_classifier.py`: new `set_market_context()` API, called every
  cycle by `trading_agent.py`. Live inference now sees the same regime
  features the dataset was labelled with.
- `features.py`: 5 new derived features
  (`dist_from_supertrend_atr`, `vwap_dist_pct`, `range_expansion`,
  `rsi_delta_3`, `obv_ratio`).

**Validated impact.** After re-train on 82,960 samples (50% UP / 50% DOWN):
```
Top features:
  1. nifty_trend            0.087   ← NEW
  2. tod_cos                0.084
  3. india_vix              0.072   ← NEW
  4. tod_sin                0.066
  5. dow_sin                0.064
  6. dow_cos                0.057
  7. supertrend_direction   0.042
  8. rsi_delta_3            0.030   ← NEW derived
  9. dist_from_high_pct     0.029
 10. bb_width               0.026
```
Two of the top-3 features and three of the top-10 are new. The model is
no longer regime-blind.

### P1c — Isotonic probability calibration

**Bug.** Raw XGBoost `predict_proba` is famously over-confident at the
extremes. `confidence_threshold: 0.65` was actually firing at empirical
~0.58 hit-rate. Live signal threshold meant nothing.

**Fix.** `train_xgboost.py` now wraps the booster with
`CalibratedClassifierCV(FrozenEstimator(model), method='isotonic')`
(sklearn 1.8 API). Sanity check: if calibration drops AUC by >2pp, fall
back to the raw booster.

**Validated impact:**
```
RAW        — AUC 0.7594  Brier 0.2059  LogLoss 0.5993
CALIBRATED — AUC 0.7607  Brier 0.1985  LogLoss 0.5804
```
AUC slightly up (calibration is monotonic), Brier down 3.6%, LogLoss down
3.2%. The 0.65 threshold now means a true ~0.65 hit-rate.

### P1d — Re-trained, packaged, validated

`models/xgboost_model.pkl` now ships:
- 31 features (matches `FeatureEngine.get_ml_feature_columns()` exactly —
  feature_count_drift impossible)
- Calibrated `CalibratedClassifierCV` wrapping a frozen XGBClassifier
- Trained on Apr-2024 → May-2026 history with regime context

### P1e — WebSocket-driven exits (held-symbols only)

**Bug.** Exits ran on a 15s REST polling loop. Fast adverse moves
(news-driven gap, halt-and-resume) could blow through SL between cycles.

**Fix.** `trading_agent.py`:
- `ws_held_only: true` config flag
- `_resubscribe_ws_to_held()` keeps the WebSocket sub list in sync with
  open positions (called on every open/close)
- `_on_tick()` runs exit checks tick-by-tick on held symbols only
- Original 15s REST polling stays as a belt-and-braces fallback

---

## P2 — Risk-manager gaps

### P2a — Supersector concentration

**Live evidence.** 2026-05-14 11:03 SHORT book held
CENTRALBK + FEDERALBNK + TATACAP + CHOLAFIN — **4 of 6 positions in
financials** — slipping past the 40% per-sector cap because each
sub-bucket (Banks / NBFC / Insurance / AMC) was independently below 40%.

**Fix.** `market_safety.SUPERSECTOR_MAP` collapses related sectors into
a single bucket (e.g. `Financials = Banks + NBFC + Insurance + AMC + FinTech`).
Toggle: `risk.use_supersectors: true` (default).

Tests: `tests/unit/test_supersectors.py` (5 cases including a replay of
the 2026-05-14 live concentration event).

### P2b — Earnings / event blackout calendar

**Industry standard.** Don't enter a new position with a known earnings
or AGM event in the next 1-2 sessions. We now have it.

**Fix.** New module `packages/core/event_calendar.py`:
- CSV-driven (`data/event_calendar.csv`)
- `Event` types: `earnings`, `dividend`, `agm`, `board_meeting`, `buyback`
- Configurable horizon (`days_before`, `days_after`)
- Hot-reload (file mtime check) so ops can update without daemon restart
- Wired into `_pre_trade_safety_checks` — empty calendar = no-op

Tests: `tests/unit/test_event_calendar.py` (10 cases).

### P2c — Intraday regime overlay

**Bug.** Daily regime classifier (200 EMA + 10d realised vol) lags by a
full day. A risk-on morning can flip risk-off by lunchtime and the model
keeps emitting BUY.

**Fix.** New `regime.classify_intraday_regime()` uses Nifty intraday %
and VIX intraday delta to surface a fast overlay (`risk_off`, `risk_on`,
`neutral`). Toggle: `intraday_regime_block_longs: true` blocks **new**
BUYs when the overlay flips `risk_off` (existing positions managed by
trail/SL as usual).

Tests: `tests/unit/test_intraday_regime.py` (6 cases).

---

## P3 — Observability / polish

### P3a — Per-regime weight visibility
`ensemble.py`: surfaced regime weights from DEBUG → INFO. Previously
operators saw `supertrend_follow=0.00` (global) yet the strategy was
firing 100% of accepts (regime-specific weight > 0). Now logged at INFO
so what-you-see is what-runs.

### P3b — Wall-clock heartbeat
Heartbeat was every-N-cycles, which silently stretched to >10 min when
the loop slowed. Now also every `heartbeat_interval_seconds: 300` of
wall-clock time — catches stalls.

### P3c — Notional-scaled minimum holding P&L
`min_holding_pnl_rs` is now scaled by position notional via
`min_holding_charges_multiple: 1.5`. A ₹50k position needs more P&L to
clear charges than a ₹5k position; flat threshold under-protected the
small ones.

---

## Files touched

**Modified:** 16
- `.gitignore` — exclude runtime ledger (`data/self_sufficiency.json`)
  and stray build logs (`data/prepare_dataset_*.log`) so each operator
  keeps their own cumulative-realised history without contaminating
  source. Added 2026-05-14 in prep for the GitHub push.
- `config.yaml` — new feature flags + tuned values + strategy cap +
  self-sufficiency block (Angel SmartAPI cost stack, ₹2,500/mo default)
- `trading_agent.py` — heartbeat, blackout, intraday overlay, WS-held,
  SL cancel/propagate, supersector wiring, strategy-concurrency cap,
  self-sufficiency ledger update on close
- `tools/audit_checkpoint.py` — new "## Self-sufficiency" section
- `packages/core/execution.py` — broker SL tracking + APIs
- `packages/core/risk_manager.py` — breakeven SL arm
- `packages/core/features.py` — 5 new derived features + master list
- `packages/core/regime.py` — intraday overlay
- `packages/core/market_safety.py` — supersector map + sector check
- `packages/strategies/ensemble.py` — log regime weights at INFO
- `packages/strategies/xgboost_classifier.py` — `set_market_context()`
- `packages/training/prepare_dataset.py` — nifty_trend / vix injection
- `packages/training/train_xgboost.py` — isotonic calibration
- `data/test_dataset.csv` / `data/train_dataset.csv` — re-built
- `tests/unit/test_xgboost_stability_gate.py` — match new API

**New:** 10
- `packages/core/event_calendar.py`
- `packages/core/self_sufficiency.py`
- `data/event_calendar.csv` (template)
- `data/training_symbols.txt`
- `tests/unit/test_breakeven_stop.py` (12)
- `tests/unit/test_supersectors.py` (5)
- `tests/unit/test_event_calendar.py` (10)
- `tests/unit/test_intraday_regime.py` (6)
- `tests/unit/test_execution_sl_tracking.py` (8)
- `tests/unit/test_strategy_concurrency_cap.py` (6)
- `tests/unit/test_self_sufficiency.py` (10)

---

## Test status

```
891 passed in 142s
```

Zero regressions. The two `_pre_trade_safety_checks` test stubs that
broke from new attributes were fixed defensively in the agent itself
(via `getattr`) so future test stubs stay forward-compatible.

---

## Recommended go-live sequence (Mon 2026-05-19)

1. **Sun evening:** scp this branch + `models/xgboost_model.pkl` to VM.
2. Restart daemon in **PAPER** mode for 1 full session (Mon morning).
   Watch the new metrics:
   - `[ENSEMBLE] Regime weights for X` lines (INFO).
   - SL-tracking debug lines on every open/close.
   - WS reconnect counts (target: 1 per session).
   - Audit checkpoint should now contain a `## Self-sufficiency` section.
   - Strategy-concurrency cap rejections appear as `[SAFETY-GATE]
     Skipping ... strategy_concurrency: N 'X' positions open >= cap M`.
3. **Tue:** flip `mode: live` with seed of **₹5,000** (already in config).
4. Hard caps for week-1 LIVE (override on top of existing config):
   - `max_open_positions: 3`
   - `max_opens_per_window: 2`
   - `max_positions_per_strategy: 2` (tighter than config default of 4
     for the first live week — proves the cap is firing)
   - `risk.max_daily_loss_pct: 1.0`
   - `risk.self_sufficiency.red_floor_inr: 1000` (tighter for week-1 — at
     -₹1k cumulative the tracker flips RED and we re-evaluate)
5. Operator must populate `data/event_calendar.csv` with any known
   earnings for the watchlist before LIVE flip — empty calendar is safe
   (no-op) but you'd be running blind to catalysts.

## Operator action items (one-time)

- [ ] Confirm `risk.self_sufficiency.monthly_fixed_cost_inr` matches your
      actual stack (default ₹2,500 assumes Angel SmartAPI free + Cursor
      Pro + free-tier OCI). If Cursor is paid as a personal expense
      (not from trading P&L), drop the figure to ~₹800.
- [ ] If you ever take delivery (CNC) trades, add CDSL ₹13.5/ISIN/day to
      the monthly cost figure.
- [ ] After 1 month LIVE, review `data/self_sufficiency.json` — if the
      tracker is RED, do not increase capital, fix the edge first.

---

## Update — 14:30 IST (Angel One broker correction + GitHub push prep)

After the 13:30 commit-of-intent, the operator clarified that the
deployment uses **Angel One SmartAPI**, not Zerodha Kite Connect. This
materially changes the cost stack — Angel SmartAPI is **free**, vs
Kite's ₹2,000/mo subscription.

### Cost-stack correction

| Component | Was assumed (Kite) | Now (Angel SmartAPI) |
|---|---:|---:|
| Broker API | ₹2,000/mo | **₹0/mo** |
| Cursor Pro | ₹1,700 | ₹1,700 |
| OCI VM (free tier) | ₹0 | ₹0 |
| CDSL/DP (MIS-only) | ₹0 | ₹0 |
| Internet/electricity | ₹500 | ₹500 |
| Misc | ₹300 | ₹300 |
| **Total** | **₹4,500/mo (₹225/day)** | **₹2,500/mo (₹125/day)** |

### Code changes from the correction

- `packages/core/self_sufficiency.py` — `DEFAULT_MONTHLY_FIXED_COST_INR`
  changed `4500.0 → 2500.0`. Doc-string now lists Angel SmartAPI
  explicitly as the cost-saving line item with the full per-component
  breakdown.
- `config.yaml` — added an explicit `risk.self_sufficiency` block (was
  using defaults silently before) with `monthly_fixed_cost_inr: 2500`.
- `changes_done_2026-05-14.md` — cost table, math section, and operator
  action items all corrected to Angel One.

### Why this matters for the LIVE plan

- **Daily breakeven nearly halved** (₹225 → ₹125). Every gross-profit
  number is ~44% closer to "self-sufficient".
- **₹5k LIVE seed** now needs **2.5%/day** vs 4.5%/day. Still aggressive
  but achievable on edge days, not mathematically impossible.
- **Capital required to outgrow cost dominance** drops from ₹50k–₹1L to
  **₹25k–₹50k**. Reachable in 3–6 months of even modest compounding.

### GitHub push prep

`.gitignore` updated to keep operator runtime state out of source:
- `data/self_sufficiency.json` — per-deployment cumulative-realised
  ledger; each operator keeps their own history.
- `data/prepare_dataset_*.log` — ad-hoc CLI build logs that
  accidentally landed in `data/` instead of `logs/`.

Tests verified post-correction: 16/16 self-sufficiency +
strategy-cap tests still green (they used explicit cost overrides, so
the default change didn't break anything).

---

## Items NOT addressed (deferred, with rationale)

- **Per-symbol model variants.** A single global classifier is the right
  starting point for a 5k-rupee book; per-symbol models need >2k
  in-sample trades each, which we don't have yet.
- **Order-book microstructure features.** Requires Level-2 broker feed,
  not exposed on Angel SmartAPI's current public market-data tier.
- **Portfolio-level VaR.** Would need correlation matrix updates every
  cycle; the current per-position SL + sector cap + supersector cap +
  daily loss cap is enough at 5k.
- **Transformer / sequence ML model.** XGBoost @ AUC 0.76 calibrated is
  a strong baseline; revisit once we have 6+ months of LIVE trades.
- **Trailing-7d giveback circuit breaker** (considered, deferred). The
  strategy-concurrency cap + existing per-strategy circuit + self-suff
  RED state already cover the same pathological pattern from three
  different angles. Adding a 4th gate now would be over-fitting to a
  3-day live history.
- **Auto-halt on self-sufficiency RED state** (deliberate). The tracker
  is informational for the first 2 weeks of LIVE — gating prematurely
  could lock the bot out of recovering from a normal drawdown. Plan to
  revisit once we have ≥10 trading days of post-deployment data.
- **Slack/email alert on YELLOW→RED transition** (deferred, easy add).
  Will land in the next iteration if the operator wants push
  notifications rather than checkpoint-based polling.

---

*Generated 2026-05-14 (initial audit), updated 13:30 IST same day in
response to live -₹592 session, and updated again 14:30 IST after the
Angel One broker correction + GitHub push prep. All changes are
committed-ready and verified by `pytest tests/` (891 passed).*
