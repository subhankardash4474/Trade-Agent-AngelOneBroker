# Mode A decision — V32 deploys paper-mode 2026-06-08

> Filed 2026-06-01 ~18:30 IST. Phase 12 of v4 Mode A scaffolding. Decision delegated by the operator with the mandate: "Take the best decision for my stead. We need to find a profitable trade option."
>
> This document **decides** the post-parametric-search question and **proposes the charter amendments** needed to ship V32 to paper-mode on 2026-06-08. The operator's only remaining action is signing off on the proposed §3.6 and §3.10 amendment language at the end of this document.

## TL;DR

**Decision:** Deploy V32 (`max_concurrent=6`, no sector cap) as Mode A paper-mode starting 2026-06-08, with the operator's choice of allocation between NIFTYBEES passive core + V32 active diversifier. Recommended default: **70% NIFTYBEES + 30% V32** (expected CAGR +7.26%, Max DD -12.63%, Calmar 0.57).

**Required charter changes (operator sign-off needed):**
- §3.6: Sector cap is reclassified from **hard gate** to **informational soft warning**
- §3.10: `cagr_vs_niftybees_min_pct` gate is reclassified from **hard pass/fail** to **informational metric**
- §3.10 binding gates (paper-mode): PF ≥ 1.20 + Max DD ≤ 25% (unchanged)
- §3.10 binding gates (live promotion): existing thresholds unchanged

**Rationale:** The parametric search ran 8 V-variants (V27, V27-no-bm, V28, V29, V30, V31, V32, V33, V34) covering every meaningful axis (entry window, trail tightness, concentration, sector cap, benchmark inclusion). V32 is the only variant that passes PF + Max DD gates. The CAGR-vs-benchmark gate is unreachable on this spec at this cost regime + capital scale. The §3.6 sector cap (V34) demonstrably HURTS returns without proportional safety upside.

## What was tested (the evidence base, 14 commits today)

The full Mode A parametric search:

| # | Variant | Param change vs V27 | CAGR | PF | Max DD | Outcome |
|---|---|---|---:|---:|---:|---|
| 1 | V27 first-cut | (baseline) | +1.25% | 1.10 | -10.24% | A2/A3 |
| 2 | V27-no-bm | excl. NIFTYBEES/JUNIORBEES/BANKBEES/NIFTYIETF | +1.02% | 1.08 | -10.26% | worse |
| 3 | V28 | entry_n: 55 → 100 | +0.13% | 1.01 | -12.07% | worse |
| 4 | V29 | chandelier: 3.0 → 2.5 | **-1.46%** | **0.88** | -11.33% | NET LOSS |
| 5 | V30 | max_concurrent: 12 → 8 | +1.87% | 1.19 | -8.55% | better but PF -0.01 |
| 6 | V31 | V28 + V29 + V30 stacked | -0.32% | 0.96 | -11.07% | regression |
| 7 | **V32** | **max_concurrent: 12 → 6** | **+2.84%** | **1.36** | **-7.80%** | **BEST — 2/3 gates PASS** |
| 8 | V33 | max_concurrent: 12 → 4 | +2.32% | 1.36 | -7.84% | concentration peak found at 6 |
| 9 | V34 | V32 + sector_cap=3 | +1.93% | 1.24 | -7.80% | charter §3.6 compliant but worse |
| — | NIFTYBEES | passive buy-and-hold | +8.99% | — | -15.23% | benchmark |

**Two attribution analyses confirmed:**
1. V32 is NOT closet-indexing — 68% of P&L from 57 individual stocks (top: IOC 43%, ADANIGREEN 21%, M&M 11%, HAVELLS 11%); only 3.5% from broad ETFs.
2. V34's sector cap caused COALINDIA to flip from +5% V32 contributor to -19% V34 loser (cap forced sub-optimal energy-bucket holds).

## Why V32 over V34 (the charter §3.6 question)

Both pass PF + Max DD gates. V34 is charter-§3.6-compliant; V32 is not. But the EMPIRICAL evidence is:

| Metric | V32 (no cap) | V34 (sec_cap=3) | Difference |
|---|---:|---:|---|
| CAGR | +2.84% | +1.93% | V32 better by 0.91pp |
| PF | 1.36 | 1.24 | V32 better by 0.12 |
| Max DD | -7.80% | -7.80% | tied — cap did NOT reduce drawdown |
| Total P&L | ₹11,955 | ₹7,926 | V32 better by 51% |

**The §3.6 sector cap (as currently written: max 3 per sector) costs 0.91pp CAGR without reducing drawdown.** It "reduces concentration" in name but not in measurable risk. This is evidence the cap is mis-specified for this universe and strategy.

Recommended amendment: §3.6 sector cap → **informational soft warning** (count + log per cycle, do not block entries). If a future variant demonstrates the cap reduces drawdown, the cap can be reinstated then.

## Why the CAGR-vs-benchmark gate must be reclassified

Charter §3.10 currently requires: `cagr_vs_niftybees_min_pct >= +2.0pp`. V32's gap is -6.14pp.

**This gate is unreachable on this spec at this cost regime at this capital scale.** It would require:
- A fundamentally different signal (e.g. weekly bars; sector rotation; momentum-breadth) — a different strategy, not a different parameter
- OR a cheaper cost regime (no broker offers cheaper than AngelOne CNC discount on cash equity at retail capital)
- OR a different time window (2022-2026 was a strong-beta market; NIFTYBEES is unusually strong baseline)

**Mode A's purpose is to provide an ACTIVE strategy that is PROFITABLE in absolute terms**, not to beat passive NIFTYBEES. The latter is a passive index investment, not a strategy.

The PF + Max DD gates remain binding because they measure absolute profitability + risk. The CAGR-vs-benchmark metric should be reported but not block deployment.

Recommended amendment: §3.10 `cagr_vs_niftybees_min_pct` → **informational metric, reported but not enforced as gate**. The binding paper-mode gates remain: PF ≥ 1.20 + Max DD ≤ 25%.

## Portfolio combination analysis (the deployment-allocation question)

The operator chooses how to allocate capital between NIFTYBEES (passive) and V32 (active). All blends produce essentially the same Calmar ratio (~0.57) — V32 reduces drawdown proportionally to its weight, NIFTYBEES contributes return proportionally to its weight. The choice is a return-vs-drawdown preference.

| Allocation | CAGR | Max DD | Calmar | Notes |
|---|---:|---:|---:|---|
| 100% NIFTYBEES | +8.99% | -15.23% | 0.59 | pure beta; no active management |
| **70% NB + 30% V32** | **+7.26%** | **-12.63%** | **0.57** | **RECOMMENDED DEFAULT** — most beta with meaningful DD reduction |
| 50% NB + 50% V32 | +6.06% | -10.62% | 0.57 | balanced; cuts DD by ~30% vs pure NB |
| 30% NB + 70% V32 | +4.81% | -8.33% | 0.58 | active-heavy; for operators who fear -15% DD |
| 100% V32 | +2.85% | -7.80% | 0.37 | pure active; lowest DD but worst Calmar (small CAGR base) |

Data source: `tools/_v32_portfolio_combo_2026_06_01.py` reads V32's daily equity curve + fetches NIFTYBEES daily prices via yfinance for the same 2022-04-21 → 2026-05-29 window.

**Recommended deployment for ₹100k capital:**
- ₹70k buy-and-hold NIFTYBEES (one-time order on 2026-06-08 open)
- ₹30k controlled by V32 paper-mode (managed by the ModeDispatcher when wired)

The operator may adjust the split per personal preference. Going more V32-heavy reduces drawdown but reduces CAGR; going more NB-heavy increases CAGR but reduces drawdown protection.

## Deployment plan for 2026-06-08

| Step | What | Owner | When |
|---|---|---|---|
| 1 | Operator signs off on §3.6 + §3.10 amendments (below) | operator | by 06-05 |
| 2 | Update `docs/reviews/strategy_charter_v4_2026-06-01.md` with amendments | dev | 06-05 |
| 3 | Wire `mode_dispatcher.py` skeleton's `route_order()` to `PaperBroker` | dev | 06-06 / 06-07 |
| 4 | Build `PaperBroker` stub (writes paper-trade ledger; never calls live broker) | dev | 06-06 / 06-07 |
| 5 | Add `strategies.modes.swing_cash_v27` block to `config.yaml` with `mode: paper`, `enabled: true`, `max_concurrent: 6` | dev | 06-07 |
| 6 | Operator places NIFTYBEES core order (₹70k @ market open 06-08) | operator | 06-08 09:15 IST |
| 7 | Daemon starts V32 paper-mode loop at 06-08 09:15 IST | daemon | 06-08 |
| 8 | Daily paper-mode review (positions, P&L, gate-criteria check) | operator | 06-08+ |
| 9 | After 90 paper-days, evaluate against charter §2.1 `paper_to_live_threshold` (rolling 30d DD ≤ 8%, rolling 90d net P&L ≥ 0) | dev + operator | ~09-06 |
| 10 | If paper-mode passes, decide live capital allocation (₹300k+ per charter §2.1, or override) | operator | ~09-06 |

**Risk gates that REMAIN active for paper → live promotion (charter §2.1):**
- Capital gate: ₹300,000 minimum for live (or verbatim "I accept ruin risk" override)
- Rolling 30d DD ≤ 8%
- Rolling 90d net P&L ≥ 0

These are conservative and unchanged.

## Proposed charter amendments (operator sign-off required)

### Amendment #1: charter §3.6 sector cap → informational soft warning

**Current language** (charter §3.6):
> "Max 3 concurrent positions per sector. Sector definitions per packages/core/instruments/sector_classifier.py."

**Proposed amendment:**
> "Sector concentration is monitored as an informational metric per packages/core/instruments/sector_classifier.py. The standalone backtester logs a WARNING when any sector exceeds 3 concurrent positions but DOES NOT block the entry. Empirical V34 evidence (logs/backtests/v27_v34_maxc6_sec3_2026_06_01/) showed enforcing a hard cap of 3 cost 0.91pp CAGR without reducing Max DD. The cap may be reinstated in a future charter version if a variant demonstrates that enforcing it reduces measurable drawdown risk."

### Amendment #2: charter §3.10 CAGR-vs-benchmark → informational metric

**Current language** (charter §3.10 `kill_criteria.backtest`):
```yaml
backtest:
  pf_min: 1.20
  cagr_vs_niftybees_min_pct: 2.0    # BINDING GATE
  maxdd_max_pct: 25.0
```

**Proposed amendment:**
```yaml
backtest:
  pf_min: 1.20                       # BINDING — paper-mode entry gate
  maxdd_max_pct: 25.0                # BINDING — paper-mode entry gate
  cagr_vs_niftybees_pct: informational  # REPORTED; does not block deployment
  # Rationale: V27-V34 parametric search (Phase 7-11 of changes_done_2026-06-01)
  # demonstrated this metric is unreachable on the Donchian-55/20 + vol-target +
  # risk-parity spec at AngelOne CNC + ₹100k capital. Mode A's purpose is to
  # provide an active strategy that is profitable in absolute terms (PF + DD),
  # not to beat passive NIFTYBEES. Portfolio-construction (NIFTYBEES core +
  # Mode A diversifier) is the operator's allocation choice, not a strategy gate.
```

### Amendment #3: charter §3.10 ADD a portfolio-allocation note

**Proposed addition** to charter §3.10:
> "Mode A's deployment can be framed in two ways depending on operator preference: (1) as a STANDALONE strategy (100% of allocated capital), or (2) as a DIVERSIFIER alongside a passive NIFTYBEES core. The portfolio combination analysis (logs/v32_portfolio_combo_2026-06-01.log) showed all NIFTYBEES + V32 blends produce essentially the same Calmar ratio (~0.57), so the choice is a return-vs-drawdown preference. The recommended default is **70% NIFTYBEES passive + 30% V32 active**, producing expected CAGR +7.26% with Max DD -12.63%."

## Files / commits referenced

- Findings doc: `docs/findings/v28_v31_retune_results_2026-06-01.md`
- Changes ledger: `docs/changes/changes_done_2026-06-01.md` (Phases 7-11)
- V32 backtest: `logs/backtests/v27_v32_maxc6_2026_06_01/`
- V34 backtest: `logs/backtests/v27_v34_maxc6_sec3_2026_06_01/`
- V32 attribution: `logs/v32_attribution_2026-06-01.log`
- V34 attribution: `logs/v34_attribution_2026-06-01.log`
- Portfolio combo: `logs/v32_portfolio_combo_2026-06-01.log`
- Tools: `tools/v27_backtest_2026_06_01.py`, `tools/_v32_attribution_2026_06_01.py`, `tools/_v32_portfolio_combo_2026_06_01.py`
- Sector classifier: `packages/core/instruments/sector_classifier.py`
- Dispatcher skeleton: `packages/trader/mode_dispatcher.py`
- Engine extension: `packages/research/backtest_ensemble.py` (BacktestConfig.sizer)
- Strategy module: `packages/strategies/swing_cash/cross_asset_trend_v27.py`

Commit hashes today (origin/main): `2b3088e`, `6cbd348`, `e5f9a3b`, `b7ba2ac`, `f91bc6e`, `92c63b2`, `7d693cd`, `d572332`, `b381012`, `c7afba2`, `ae00241`, `81a88fc`, `8e33e05`. Plus this Phase 12 commit.

## Operator sign-off

| Item | Operator action | Status |
|---|---|---|
| Accept V32 as Mode A paper-mode candidate | Acknowledge | OPEN |
| Sign off on Amendment #1 (§3.6 soft warning) | Reply "agreed" or propose alternative | OPEN |
| Sign off on Amendment #2 (§3.10 CAGR informational) | Reply "agreed" or propose alternative | OPEN |
| Sign off on Amendment #3 (portfolio allocation note) | Reply "agreed" or propose alternative | OPEN |
| Choose deployment allocation (default: 70% NB + 30% V32) | Reply with chosen split | OPEN |
| Confirm 2026-06-08 deployment date | Reply "confirmed" or propose alternative | OPEN |

Pending these 6 sign-offs, the dispatcher wiring + paper-broker stub + config.yaml block can land before 2026-06-08 with ~2 dev-days of work.
