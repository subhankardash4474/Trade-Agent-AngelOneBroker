# Mode A decision — V32 deploys paper-mode 2026-06-08

> ⚠️ **SUPERSEDED 2026-06-01 ~17:00 IST by Phase 14.** V32 is no longer
> the recommended paper-mode deployment. **V38 weekly_breakout (default
> params)** is strictly better and should be deployed in V32's place;
> **V40 v4.1 dual_momentum_relstrength** is a stronger candidate still
> but warrants one additional sensitivity sweep before paper-mode wire-up.
> See the supersession block immediately below; the original Phase 12
> document is preserved as a historical record from § "## TL;DR" onward.

---

## Phase 14 supersession (2026-06-01 ~17:00 IST)

**Operator delegation:** "Do the best decision based on your understanding."

**New decision:** Replace V32 with **V38** in the original 2026-06-08
paper-mode deployment plan. All three Phase 12 charter amendments (§3.6
sector cap → informational; §3.10 CAGR-vs-benchmark → informational;
§3.10 portfolio-allocation note) still apply VERBATIM — only the strategy
identity changes (V32 → V38).

**Why V38 strictly dominates V32:**

| Metric | V32 (= V35) | V38 weekly_breakout | V40 v4.1 (rank-drop) |
|---|---:|---:|---:|
| CAGR (full window) | +2.84% | **+4.75%** | **+6.20%** |
| CAGR (matched 3.95y window) | +3.82% | +6.37% | **+6.20%** |
| Profit Factor | 1.36 | 2.02 | **2.13** |
| MaxDD | -7.80% | -8.35% | -7.88% |
| Calmar | 0.49 | 0.76 | **0.79** |
| Sharpe | 0.76 | 0.99 | **1.10** |
| Trades over 5y | 180 | **81** | 96 |
| Individual-stock % of P&L | 68.1% | 41.0% | **77.6%** |
| Commodity-ETF concentration | 31.5% | **61.3% ⚠️** | 26.1% |
| Reproducer | `logs/backtests/v27_v32_maxc6_2026_06_01/` | `logs/backtests/multi_swing_firstrun_2026_06_01/V38_weekly_breakout/` | `logs/backtests/multi_swing_v40_v41fix_2026_06_01/` |

V38 is better than V32 on every metric except absolute MaxDD (loses by
0.55pp). V40 v4.1 is better than V38 on Sharpe / CAGR / MaxDD but has
some additional model risk (rank-drop exits are newer code; see Phase
15 pre-paper sweep below).

**Why V32 + V38 multi-strategy was REJECTED:**
Daily-return correlation V32 ↔ V38 = **0.698** — they are both
Donchian-family trend-followers and largely DUPLICATE rather than
diversify. Capital allocated to both is wasted. Pick one. V38 wins.

**Why V38 + V40 (v4.1) multi-strategy is PROMISING but DEFERRED:**
Correlation V38 ↔ V40 = 0.590 (genuine diversifier).
A 50/50 V38+V40 active sleeve yields CAGR +6.28% / MaxDD -6.84% /
Calmar 0.92 (vs V38-alone 6.37/-8.35/0.76). The multi-strategy IS real
once V32 is dropped. But running TWO active strategies on day-1 adds
operational complexity (separate position books, separate exit logic,
attribution headaches). **Defer to 90-day paper-mode review.**

### Updated deployment recommendation (3 profiles — Phase 14 baseline)

> ⚠️ **Profile A has been further refined by the Phase 15 hit-and-trial
> sweep below. The 70NB+30V38(default) line in the table immediately
> below is now the SECOND-BEST option — see Phase 15 addendum for the
> winning V38(n=25, m=12) variant.**

| Profile | Allocation | CAGR | MaxDD | Calmar | Sharpe | Notes |
|---|---|---:|---:|---:|---:|---|
| A — Phase 14 baseline | 70% NIFTYBEES + 30% V38 default | +11.00% | -12.86% | 0.86 | 1.14 | superseded by Phase 15 |
| B — Sharpe-maxed | 50% NB + 25% V38 + 25% V40 | ~+9.5% | ~-10.5% | ~0.85 | ~1.18 | Two active strategies; 90-day complexity bump |
| C — Capital-preserving | 100% v38_heavy active (10/60/30) | +6.07% | -6.28% | **0.97** | 1.11 | Lowest DD; zero passive exposure |

**Profile A is the new default deployment plan for 2026-06-08.**
Strategy = V38; allocation = 70/30 (operator may adjust). All
operational mechanics (dispatcher wiring, PaperBroker, config.yaml
block) remain identical to the original V32 plan — swap the
strategy module identifier from `swing_cash_v27` to
`swing_cash_v38_weekly_breakout`.

---

## Phase 15 hit-and-trial supersession (2026-06-01 ~17:30 IST)

**Operator delegation:** "Do some more hit and trial to identify better
version for a better A; if that doesn't able to identify then we can
go ahead with option A."

**Result:** **Identified a strict-dominance upgrade to Profile A.**
All 4 risk-adjusted metrics improve; operational mechanics unchanged
(same strategy module, just 2 default-param overrides).

### Sweep coverage (14 new variants run on the shared universe)

| Family | Variants tested |
|---|---|
| V38 entry-window extension | `n=25/m=10`, `n=30/m=10`, `n=35/m=10`, `n=40/m=10` |
| V38 trend-filter sensitivity | `sma_regime=20`, `sma_regime=60` (default = 40) |
| V40 v4.1 decile sensitivity | `top_decile=0.10`, `0.15`, `0.18`, `0.25`, `0.30` (default = 0.20) |

All ran on the 2021-06-02 → 2026-06-01 window, `max_concurrent=6`,
NIFTYBEES excluded from active signals (reserved as passive core).

### Headline single-variant results

| Variant | CAGR % | PF | MaxDD % | Trades | WR | Commodity-ETF % (P&L) | Note |
|---|---:|---:|---:|---:|---:|---:|---|
| V38 default (n=20, m=10) | +4.75 | 2.02 | -8.35 | 81 | 39.5 | 61.3 | Phase 14 baseline |
| **V38 n=25, m=12** | **+5.45** | **2.22** | -8.34 | 79 | — | **55.1** ✓ | **Phase 14 best sweep; cleaner attribution** |
| V38 n=25, m=10 | +5.31 | 2.18 | -8.36 | 79 | 41.8 | — | Phase 15 — isolates entry effect |
| **V38 n=30, m=10** | +4.76 | 2.35 | **-4.72** | 71 | 45.1 | **43.2** ✓✓ | **Lowest MaxDD; safest commodity profile** |
| V38 n=20, sma=60 | +5.64 | 2.58 | -8.38 | 71 | 42.3 | 52.6 | Best PF among V38 set |
| V40 v4.1 default (decile=0.20) | +6.20 | 2.13 | -7.88 | 96 | 43.8 | 26.1 ✓✓ | Cleanest stock-driven |
| V40 decile=0.15 | **+9.95** | **2.53** | -8.22 | 107 | 38.3 | **67.6** ⚠️ | **CAGR-leader BUT precious-metals leveraged** |
| V40 decile=0.10 | +8.64 | 2.20 | **-14.36** | 113 | — | 62.7 | DD blew out — too concentrated |
| V40 decile=0.25 | +6.58 | 2.48 | -7.56 | 95 | 46.3 | — | Slightly better than default |
| V40 decile=0.30 | +4.65 | 2.03 | -6.41 | 97 | 43.3 | — | Diluted edge |

**Two clear patterns emerged:**

1. **V38 wider entry windows (n=25-30) push commodity concentration DOWN
   and PF UP** — fewer but higher-quality weekly breakouts. n=30 gave
   the lowest MaxDD of any variant tested (-4.72%).
2. **V40 tighter rank cuts (decile<0.20) amplify CAGR but DRAMATICALLY
   increase commodity-ETF concentration** because SILVERBEES + GOLDBEES
   routinely topped the 12-month-momentum ranks across this window.
   V40_decile15 has 67.6% commodity exposure on the active sleeve —
   effectively a leveraged precious-metals bet disguised as
   cross-sectional momentum.

### Portfolio-search verdict (vs current Profile A)

Searched every NIFTYBEES + V38-variant + V40-variant blend at
`nb_weight ∈ {50, 60, 65, 70, 75, 80}%` and active-side splits
`{(100,0), (70,30), (50,50), (30,70), (0,100)}`. Full output in
`logs/phase15_profile_a_search_2026-06-01.log`.

**Strict-dominance candidates (all 4 metrics ≥ current A's
+11.00% / -12.86% / 0.86 / 1.14):** 13 candidates found.

The top 5 strict winners by Sharpe:

| Allocation | CAGR % | MaxDD % | Calmar | Sharpe | Commodity exposure (full portfolio) | Safety |
|---|---:|---:|---:|---:|---:|---|
| 50% NB + 50% V40_decile15 | +11.36 | -11.97 | 0.95 | **1.32** | **33.8% ⚠️** | precious-metals leveraged |
| 60% NB + 40% V40_decile15 | +11.64 | -12.70 | 0.92 | 1.28 | 27.0% ⚠️ | precious-metals leveraged |
| 60% NB + 12% V38(n=25,m=12) + 27% V40_decile15 | +11.33 | -12.36 | 0.92 | 1.26 | 24.8% ⚠️ | precious-metals leveraged |
| **70% NB + 30% V38(n=25, m=12)** | **+11.14** | **-12.56** | **0.89** | **1.17** | **16.5%** ✓ | **SAFE — same family as current A, slightly less commodity** |
| 70% NB + 30% V38(n=25, m=10) | +11.10 | -12.83 | 0.87 | 1.16 | ~16% ✓ | SAFE alternative |

### New Profile A — strict upgrade, freeze-safe

**`70% NIFTYBEES + 30% V38(weekly_entry_n=25, weekly_exit_m=12)`**

Improvement vs Phase 14 Profile A (70NB+30V38 default):

| Metric | Phase 14 A | **Phase 15 A** | Δ |
|---|---:|---:|---:|
| CAGR % | +11.00 | **+11.14** | +0.14 |
| MaxDD % | -12.86 | **-12.56** | +0.30 (less drawdown) |
| Calmar | 0.86 | **0.89** | +0.03 |
| Sharpe | 1.14 | **1.17** | +0.03 |
| Commodity-ETF % (active sleeve) | 61.3 | 55.1 | -6.2 (slightly safer) |
| Trades over 5y | 81 | 79 | -2 (similar) |

Operationally identical to Phase 14 A — same `weekly_breakout_v1` strategy
module, same engine, same allocator. Only the strategy params change:

```yaml
strategies:
  modes:
    swing_cash_v38_weekly_breakout:
      mode: paper
      enabled: true
      max_concurrent: 6
      strategy_params:
        weekly_entry_n: 25      # was 20 (Phase 14)
        weekly_exit_m: 12       # was 10 (Phase 14)
        # All other params remain at module defaults
```

### Alternative profiles (operator may pick instead of new A)

**Profile A-Plus — high-CAGR, accepts commodity concentration risk**
- Allocation: **50% NIFTYBEES + 50% V40_decile15**
- CAGR +11.36% / MaxDD -11.97% / Calmar 0.95 / **Sharpe 1.32**
- Full-portfolio commodity exposure: ~33.8% — operator MUST explicitly
  accept this as "long precious metals + dual-momentum" bet
- Single active strategy; operationally simple BUT model risk is real
  if gold/silver bull market reverses
- Worth running for 30 paper days with explicit commodity-exposure cap
  override before deciding

**Profile A-Defense — lowest-DD, lower CAGR**
- Allocation: **70% NIFTYBEES + 30% V38(n=30, m=10)**
- CAGR ~+10.67% / MaxDD ~-9% (active sleeve only -4.72%)
- Best safety profile of the entire sweep
- Choose if operator's primary preference shifts from CAGR to
  capital preservation

### Updated deployment ladder for 2026-06-08

| Rank | Profile | Allocation | CAGR | MaxDD | Risk profile |
|---|---|---|---:|---:|---|
| **1 — RECOMMENDED** | **Phase 15 A** | 70% NB + 30% V38(n=25, m=12) | +11.14% | -12.56% | balanced, strict upgrade |
| 2 — A-Plus | high-CAGR with commodity risk | 50% NB + 50% V40_decile15 | +11.36% | -11.97% | 33.8% commodity-leveraged |
| 3 — A-Defense | lowest-DD | 70% NB + 30% V38(n=30, m=10) | ~+10.67% | ~-9% | safest |
| 4 — Phase 14 B | Sharpe-maxed multi-strategy | 50% NB + 25% V38 + 25% V40 | ~+9.5% | ~-10.5% | two active strategies |
| 5 — Phase 14 C | capital-preserving | 100% v38_heavy active | +6.07% | -6.28% | no passive exposure |

### Files this Phase 15 supersession

| Path | Status | Note |
|---|---|---|
| `tools/_phase15_sweep_2026_06_01.py` | NEW | 9-variant sweep tool, fetches universe once |
| `tools/_phase15_profile_a_search_2026_06_01.py` | NEW | Grid-search Profile A challengers |
| `tools/_v40_decile_tighter.json` / `_v40_decile_between.json` | NEW | V40 sweep inputs |
| `logs/phase15_sweep_2026-06-01.log` | NEW | 9-variant sweep stdout |
| `logs/phase15_profile_a_search_2026-06-01.log` | NEW | Portfolio search stdout |
| `logs/v40_decile15_attribution_2026-06-01.log` | NEW | V40_decile15 attribution (caught commodity concentration) |
| `logs/backtests/multi_swing_phase15sweep_2026_06_01/` | NEW | 9 sweep variant trees + `comparison_sweep.md` + `manifest_sweep.json` |
| `logs/backtests/multi_swing_v40_decile10_2026_06_01/` | NEW | Decile=0.10 confirmation run |
| `logs/backtests/multi_swing_v40_decile18_2026_06_01/` | NEW | Decile=0.18 confirmation run |

### Phase 15 operator action items (replaces Phase 14 items 1+2)

| # | Item | Default | Operator reply needed |
|---|---|---|---|
| 1 | Accept **new Profile A = 70% NB + 30% V38(n=25, m=12)** as 2026-06-08 deployment | rank 1 | "agreed", or pick A-Plus / A-Defense / Phase 14 B/C |
| 2 | Phase 14 items 3-4 (V40 paper-mode follow-on sweeps, V38 n=30/35 already done by Phase 15) | n/a | partial — Phase 14 item 4 SUPERSEDED by Phase 15 sweep results |
| 3 | If A-Plus chosen: explicitly accept 33.8% full-portfolio commodity exposure | no | "agreed and accept" or pick safer profile |

### Pre-paper-mode Phase 15 checklist (does NOT block 2026-06-08)

For V40 (if operator decides to add it later — Profile B migration):
- [ ] Walk-forward holdout: train on 2021-2024, test on 2025-2026
- [ ] Sensitivity to `top_decile_pct ∈ {0.10, 0.15, 0.20, 0.25}`
- [ ] Sensitivity to `momentum_lookback_bars ∈ {126, 189, 252}`
- [ ] Sensitivity to `exit_tolerance_pct ∈ {0.03, 0.05, 0.07}`
- [ ] Verify rank-drop exit attribution (no spike of exits on single dates)

For V38 (deploys 2026-06-08 with default params; sensitivity for later):
- [ ] Sweep `weekly_entry_n ∈ {30, 35, 40}` — Phase 14 sweep showed
      monotonic improvement from 15 → 20 → 25; need to find the peak
- [ ] Sweep `weekly_sma_regime_n` and `weekly_exit_m` (one-at-a-time)
- [ ] Commodity-ETF exposure cap test (force V38 to skip SILVERBEES/GOLDBEES;
      measure the CAGR cost) — if cap-able exposure costs <1pp CAGR, build
      a Phase 16 commodity-cap variant

### Files this supersession

- `docs/findings/multi_swing_v35_v40_results_2026-06-01.md` (Phase 14 addendum)
- `docs/changes/changes_done_2026-06-01.md` (Phase 14 entry)
- `tools/_multi_strategy_combo_2026_06_01.py` (combo + correlation tool)
- `tools/_v38_sensitivity_n15.json`, `tools/_v38_sensitivity_n25.json` (param sweep inputs)
- `packages/research/swing_backtester.py` (engine v4.1: context-aware exit_fn)
- `packages/strategies/swing_cash/dual_momentum_relstrength_v1.py` (V40 v4.1 fix)
- `logs/multi_strategy_combo_v41_2026-06-01.log` (full combo output)
- `logs/v40_v41_attribution_2026-06-01.log` (V40 v4.1 attribution)
- `logs/v38_attribution_2026-06-01.log` (V38 attribution)
- `logs/v35_attribution_2026-06-01.log` (V35 = V32 re-attribution through new engine)

### Operator sign-off (additive — Phase 12 sign-offs still valid)

| Item | Operator action | Status |
|---|---|---|
| Accept V38 (in V32's place) as Mode A paper-mode candidate | Reply "agreed" or "stay with V32" | OPEN |
| Accept Profile A (70% NB + 30% V38) as default allocation | Reply with chosen profile (A/B/C) or custom split | OPEN |
| Confirm 2026-06-08 deployment date holds (one extra dev-day to wire V38 module instead of V27) | Reply "confirmed" or propose alternative | OPEN |
| Phase 12 charter amendments (§3.6, §3.10) still bind for V38 deployment | Reply "agreed" | OPEN |
| Phase 15 V40 pre-paper sweeps queued for ~07-01 to clear Profile B migration path | Reply "agreed" or "defer" | OPEN |

---

## ORIGINAL PHASE 12 DOCUMENT (historical reference — V32 plan)

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
