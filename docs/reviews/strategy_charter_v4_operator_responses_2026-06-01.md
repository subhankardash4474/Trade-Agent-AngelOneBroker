# Strategy Charter v4 — Operator Responses to §10 Open Questions

> **Filed:** 2026-06-01 12:42 IST (in-session with the agent as scribe).
> **Charter:** [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md) §10.
> **Path-forward:** [`path_forward_assessment_2026-06-01.md`](path_forward_assessment_2026-06-01.md).
> **Activation gate:** the charter §10 stated "operator must answer in writing before any v4 code lands". This document satisfies that gate.

## Headline

**Operator accepted all 10 adviser recommendations from charter §10.** No overrides. Charter design is now fully locked for Phase 1 (officially Mon 2026-06-08; pre-Phase-1 scaffolding starts 2026-06-01 afternoon per operator's 12:31 IST directive).

The operator's stated rationale on the pre-Phase-1 start (paraphrased from the 12:31 IST exchange):

> "We are not deploying anything on the trader VM where our paper-mode intraday is running. Since there will be no deployment, there is no issue with developing v4 in parallel — if good backtest results come in, we quickly start ASAP on paper-mode for live data on the next Monday."

This reading honors FREEZE_v2.1.md's letter (the contract enumerates specific frozen files; new v4 files outside that list are freeze-safe) AND its intent (no behavioural change to the live trader VM during the validation window). The freeze-safe development path is:

- ✅ New files in `packages/strategies/swing_cash/`, `packages/research/signals/`, `packages/research/instruments/`, `data/`, `tests/`
- ✅ Extensions to `packages/research/backtest_ensemble.py` and `packages/research/battery.py` (research-only, not freeze-listed)
- ❌ No modifications to `packages/strategies/{rsi_momentum,ensemble,...}.py`, `packages/core/{risk_manager,position_sizer}.py`, or `config.yaml` strategy/risk blocks
- ❌ No deployment to trader VM until post-2026-06-05 verdict + operator decision

---

## Responses (Q1–Q10)

| # | Question | Operator's answer | Status |
|---|---|---|---|
| **Q1** | Donchian entry channel period for V27 | **55 days** (charter default; Turtle/Dunn-Capital standard) | ✅ Default accepted |
| **Q2** | Volatility-target risk per trade | **0.5% of equity per trade** (charter default; conservative) | ✅ Default accepted |
| **Q3** | NIFTYBEES benchmark window | **Same 5-year window** as V27 backtest (primary); rolling 5-year for sensitivity check | ✅ Default accepted |
| **Q4** | F&O paper-broker fill source | **NSE EOD premium** for all decision-grade results; Black-Scholes-with-IV for dispatcher unit tests only | ✅ Default accepted |
| **Q5** | Dispatcher cutover strategy | **Hard cutover** after Phase 1 backtest passes — one commit, legacy code path removed atomically | ✅ Default accepted (the most consequential design decision; Q5 directly determines the dispatcher API surface, and a hard-cutover decision keeps the surface small) |
| **Q6** | Mode D (cointegration pairs research) timeline | **Phase 5 or never** — lowest priority; reserve dev cycles for higher-EV tracks | ✅ Default accepted |
| **Q7** | Live-trading capital source | **AngelOne API daily** for live modes; fall back to `data/self_sufficiency.json` if API unreachable | ✅ Default accepted |
| **Q8** | Override-capital-gate friction string | **Keep verbatim**: `"I accept ruin risk"` — the friction IS the point | ✅ Default accepted |
| **Q9** | Phase 1 calendar start date | **Mon 2026-06-08** for official Phase 1 (mode-flag flip to paper / live). **Pre-Phase-1 scaffolding** (new files, no deployment) starts 2026-06-01 afternoon. | ✅ Default accepted with operator's pre-Phase-1 clarification |
| **Q10** | Cost-of-fight cumulative tracking | **`data/cost_of_fight_<YYYY>.csv`** updated monthly; auto-emit reminder at PK3 75% and PK1 12mo | ✅ Default accepted |

---

## Implications for the Phase 1 build (effective immediately)

With all defaults accepted, the Mode A V27 build is fully spec'd:

- **Signal:** Donchian 55-entry / 20-exit + 200-day SMA regime filter + 1.2× volume confirm + 5.0% ATR cap + 10-day whipsaw guard + ADX(14) ≥ 20 + 50-day SMA slope > 0.
- **Sizing:** Vol-target 0.5%-of-equity-per-trade per position, capped at 8% per name.
- **Allocation:** Inverse-volatility risk-parity across active positions, capped per name.
- **Concurrency:** 12 max concurrent positions, 3 per sector, 100% max long exposure.
- **Cost model:** `packages.core.charges:CashCNCCharges` (post-CHG-01..05 AngelOne rates).
- **Benchmark:** NIFTYBEES same-window + GOLDBEES + 60/40 blend.
- **Stop criteria:** A1-A5 per charter §3.10 — PASS gate is PF ≥ 1.20 AND CAGR ≥ NIFTYBEES + 2% AND MaxDD ≤ 25%.

**Dispatcher API design** (Q5 = hard cutover) — the new dispatcher (`packages/trader/mode_dispatcher.py`) will be a single class that:
- Reads `strategies.modes.*` from `config.yaml` at boot.
- For each enabled mode, instantiates the mapped `signal_module` + `cost_model`.
- Routes each cycle's per-symbol signals through the mode's runtime (CNC for swing, intraday product for F&O).
- Enforces the `capital_allocation_pct` per mode at portfolio level.
- Enforces the capital gate (Q8: `"I accept ruin risk"` verbatim override required to bypass).
- Has a single-commit cutover migration that removes the legacy per-strategy `strategies.rsi_momentum.*` blocks from `config.yaml`.

No feature-flag coexistence path. Pre-cutover, the legacy code path runs. Post-cutover, only the dispatcher path runs. No half-states.

---

## What this UNBLOCKS today

Per operator's 12:31 IST directive and the Q9 clarification:

1. **Pre-Phase-1 scaffolding** can begin immediately:
   - Universe file: `data/v4_universe_swing_cash.txt` (75 instruments per charter §3.1)
   - Signal utilities: `packages/core/signals/{donchian,volatility_sizer,risk_parity}.py` (**charter §1 listed these under `packages/research/signals/`; corrected to `packages/core/signals/` because `tests/unit/test_pod_boundaries.py` only permits `strategies -> core`, not `strategies -> research`** — the asymmetry is intentional: research is upstream of strategies at audit time)
   - Instrument loaders: `packages/core/instruments/etf_universe.py` (same correction as above)
   - Strategy module: `packages/strategies/swing_cash/cross_asset_trend_v27.py`
   - Pin tests: `tests/unit/test_cross_asset_trend_v27_2026_06_01.py` + `tests/unit/test_v27_signals_2026_06_01.py`
2. **First V27 backtest** (TODAY):
   - Uses existing v2.1 position sizer (not yet wired vol-target). Labelled "V27 signals + v2.1 sizing — not the true V27 number" in the comparison.md.
   - Provides a directional read on whether Donchian-55/20 on a 75-instrument universe has any visible edge before we invest the next ~5 days extending the engine.
3. **Engine extension** (TUESDAY 2026-06-02):
   - `packages/research/backtest_ensemble.py` extended with `--sizer` and `--allocator` flags so V27 (and future V-variants) can use the vol-target + risk-parity stack.
   - This is research-engine work, NOT in the FREEZE_v2.1 file list. Freeze-safe.
4. **True V27 backtest** (TUESDAY/WEDNESDAY 2026-06-02/03):
   - Full V27 with vol-target sizing + risk-parity allocation. This is the number that decides A1-A5 per charter §3.10.

---

## Charter §1 corrections filed (file-path drift)

Charter §1 originally listed these new modules under `packages/research/`,
but the pod-boundary rule (`tests/unit/test_pod_boundaries.py`) only
permits `strategies` to import from `core` (NOT from `research`). The
correct landing pads are:

| Charter §1 path (drafted) | Actual landing path | Reason |
|---|---|---|
| `packages/research/signals/donchian.py` | `packages/core/signals/donchian.py` | strategies must import the Donchian signal at runtime |
| `packages/research/signals/volatility_sizer.py` | `packages/core/signals/volatility_sizer.py` | same |
| `packages/research/signals/risk_parity.py` | `packages/core/signals/risk_parity.py` | same |
| `packages/research/instruments/etf_universe.py` | `packages/core/instruments/etf_universe.py` | strategies must read the universe file at runtime |

Charter §1's `packages/research/backtest_fno.py` and `fno_universe.py`
correctly belong under `packages/research/` because those are
backtester-only (F&O paper engine extension), not runtime strategy code.

Charter §1 should be re-issued with the corrected paths in any future
v5/v6 charter; for v4 this responses-doc serves as the binding correction.

---

## What this still BLOCKS

- **Mode B / Mode C engine code** (F&O paper) — blocked until: (a) charter §4.2 F&O backtester validation gate clears (NSE EOD premium data fetchable + matches within 1.5σ for 10 spot-check days), (b) capital ≥ ₹500k OR `"I accept ruin risk"` override. Operator at ₹120k; gate not met.
- **Mode dispatcher implementation** — blocked until Q1-Q10 answered (now done as of this doc). Dispatcher implementation can start Wednesday after Tuesday's engine extension lands.
- **Any modification to `packages/strategies/{rsi_momentum,ensemble,...}.py`, `packages/core/{risk_manager,position_sizer}.py`, `config.yaml` strategy/risk blocks** — blocked through 2026-06-05 verdict per FREEZE_v2.1.md.
- **Any deployment to trader VM** — blocked through operator's post-2026-06-05 decision per charter §6.1.

---

## Cross-references

- [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md) — the technical charter this doc answers.
- [`path_forward_assessment_2026-06-01.md`](path_forward_assessment_2026-06-01.md) — the operator decision paper.
- [`../freeze/verdict_meeting_packet_2026-06-05.md`](../freeze/verdict_meeting_packet_2026-06-05.md) — Friday verdict meeting packet (must still be applied mechanically; this responses doc does NOT pre-empt the verdict).
- [`../freeze/wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md) — pre-committed gate sheet.
- [`../changes/changes_done_2026-06-01.md`](../changes/changes_done_2026-06-01.md) — single-source ledger; Phase 6 records the v4 charter, Phase 7 (to be written today) will record Mode A scaffolding.

---

*Filed under the `changes-done` skill convention (this doc lives in `docs/reviews/` rather than `docs/changes/` because it is a charter response, not a change-ledger entry; the corresponding ledger entry lands in `changes_done_2026-06-01.md` Phase 7).*
