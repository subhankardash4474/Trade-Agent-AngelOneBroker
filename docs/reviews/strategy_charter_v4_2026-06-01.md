# Strategy Charter v4 — Multi-Mode Research Agent, Pre-Committed 2026-06-01

> **Read [`path_forward_assessment_2026-06-01.md`](path_forward_assessment_2026-06-01.md)
> first.** This charter is the technical companion to that decision paper.
> It activates IF the 2026-06-05 verdict goes "wind-down-of-v2.1" (the
> overdetermined outcome per `brutal_review_2026-06-01.md` Session 3)
> AND the operator confirms the §10 open questions in the
> path-forward assessment.
>
> **This charter supersedes [`../freeze/freeze_v3.0_charter_2026-05-30.md`](../freeze/freeze_v3.0_charter_2026-05-30.md)
> only after Phase 0 wind-down completes.** Until then, v3.0 charter
> remains the active forward-plan-of-record. v4 is pre-committed in
> parallel for the same reason v3 was: framing must not be calibrated
> by what the verdict-week produces.

**Pre-commit timestamp:** 2026-06-01 12:35 IST (v1.0)
**Author:** trading agent (adviser persona) + operator joint commitment
**Status:** PRE-COMMIT ACTIVE. Phase 1 activates 2026-06-08 (Mon) post-wind-down.

**Reconciliation with `freeze_v3.0_charter_2026-05-30.md`:**

* v3.0 charter §0 hypothesis ("simple two-rule swing on Nifty 30") is
 the **direct ancestor** of this charter's Mode A. v3.0 has been
 **falsified at the backtester gate** (V25 PF 0.04, V26 PF 0.01 under
 AngelOne charges per
 [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md))
 and is winding down on 2026-06-05 without ever leaving Phase A.
* v4 inherits v3's hypothesis structure (pre-committed phases, hard
 gates, kill criteria) but extends to **four parallel tracks** per
 operator's 2026-06-01 12:09 IST decision.
* v4 keeps v3.0 §1 findings 1-4 as **closed verdicts** — no
 5-min XGBoost revival, no MIS ensemble revival, no short-side
 trading on Indian-equity universe, no further 5-min MIS commission
 sweeps. New hypotheses only.

---

## 0. The four hypotheses

> **Mode A — `swing_cash_v27`:** A diversified cross-asset trend
> system on 50-80 instruments (Nifty 100 stocks + 5-8 ETFs covering
> gold/silver/banking/liquid debt) at daily timeframe under AngelOne
> CNC delivery costs, using Donchian-channel signals with
> volatility-targeted position sizing and risk-parity capital
> allocation, produces **PF ≥ 1.2 over a 5-year backtest AND beats
> NIFTYBEES buy-and-hold by ≥ 2% CAGR**.
>
> **Mode B — `swing_fno_paper`:** A futures-swing system on Nifty
> and Bank Nifty current-month futures, holding 5-15 days with
> trend-pullback entry and ATR-based stops, produces **PF ≥ 1.3
> over a 3-year backtest** AFTER the F&O backtester extensions are
> validated to within 1.5σ of NSE-published EOD premia on a
> 10-day spot-check.
>
> **Mode C — `intraday_fno_paper`:** Options-selling defined-risk
> spreads (iron condors / credit spreads) on Nifty weekly options,
> entered Monday-Wednesday and exited by Thursday close or 50%
> max-profit (whichever first), produce **PF ≥ 1.2 with max-loss
> capped at spread width** over a 2-year backtest.
>
> **Mode D — `pairs_cointegration_research`:** Mean-reverting
> cointegrated pairs among Nifty 200 stocks (selected by ADF p < 0.05
> + half-life 5-30 trading days) with z-score-based entry at ±2.0σ
> and exit at 0σ or stop at ±3.5σ, produce **PF > 1.1 with paired
> annualised Sharpe > 0.4** over a 5-year backtest. (Research-only;
> never auto-promotes to live.)

Each is independently falsifiable at the **backtester gate**. None
advance to paper without backtest success. None advance to live
without paper success AND capital gate clearance.

---

## 1. What dies vs what keeps

### Dies on Friday 2026-06-05 (wind-down)

* `strategies.swing_combined_shorts` enabled flag → `false`
* `strategies.rsi_momentum_intraday` (if still alive) → `false`
* All v2.1-era ensemble / XGBoost code paths → unreachable but not
 deleted (preserve git history; rename modules with `_legacy_v2_1_`
 prefix for clarity)
* `risk.allow_shorts` for cash equity → stays `false` permanently
 (v3.0 charter §1 finding #3)
* All MIS intraday strategies for cash equity → no new variants;
 the cost regime is settled

### Keeps post-wind-down

* `packages/core/charges.py` (post-CHG-01..05 AngelOne calibration)
* `packages/research/backtest_ensemble.py` (the engine; extended,
 not rewritten)
* `packages/brokers/angelone/*` (the connector; mode-routing wraps
 around it, doesn't replace it)
* `packages/monitoring/*` (audit checkpoints, signal-audit CSV, EOD
 reports — instrumentation is mode-agnostic and reused)
* `packages/trader/*` (order placement; extended with mode-routing
 layer)
* All test infrastructure under `tests/` (extended with mode-specific
 fixtures)
* Live `data/trading_agent.db` schema (extended with `mode_tag`
 column on positions / trades; existing rows backfilled
 `mode_tag = 'legacy_v2_1'`)

### Net-new in v4

| New module path | Purpose |
|---|---|
| `packages/strategies/swing_cash/cross_asset_trend_v27.py` | Mode A signal generator |
| `packages/strategies/swing_fno/futures_trend_pullback.py` | Mode B signal generator |
| `packages/strategies/intraday_fno/options_credit_spreads.py` | Mode C signal generator |
| `packages/strategies/research/cointegration_pairs.py` | Mode D signal generator (research) |
| `packages/trader/mode_dispatcher.py` | Mode-flag enforcement layer |
| `packages/trader/paper_broker.py` | Paper-mode order stub (separate from live broker) |
| `packages/research/backtest_fno.py` | F&O backtester extensions |
| `packages/research/instruments/etf_universe.py` | ETF loader |
| `packages/research/instruments/fno_universe.py` | F&O instrument metadata |
| `packages/research/signals/donchian.py` | Reusable Donchian signal |
| `packages/research/signals/volatility_sizer.py` | Vol-target position sizer |
| `packages/research/signals/risk_parity.py` | Risk-parity capital allocator |
| `packages/core/charges_fno.py` | F&O cost model (separate from cash) |
| `data/v4_universe_swing_cash.txt` | Mode A instrument list (versioned per `repo-conventions`) |
| `data/v4_universe_swing_fno.txt` | Mode B instrument list |
| `data/v4_universe_intraday_fno.txt` | Mode C instrument list |
| `tests/unit/test_cross_asset_trend_v27.py` | Mode A tests |
| `tests/unit/test_futures_trend_pullback.py` | Mode B tests |
| `tests/unit/test_options_credit_spreads.py` | Mode C tests |
| `tests/unit/test_cointegration_pairs.py` | Mode D tests |
| `tests/unit/test_mode_dispatcher.py` | Dispatcher contract tests |
| `tests/integration/test_paper_broker_isolation.py` | Paper-mode never leaks to live |
| `tests/unit/test_charges_fno.py` | F&O charges pin tests |

All new test files follow `test-conventions` skill naming.

---

## 2. The mode-flag architecture (the spine)

This section is the **most important section in the charter**.
Getting this wrong forces a rewrite at Phase 4. Getting it right is
the difference between an extensible research lab and a tangled mess.

### 2.1 Config schema (canonical)

Added to `config.yaml` under a new top-level `strategies.modes` block.
The existing `strategies.*` per-strategy blocks become **legacy** and
are removed once the dispatcher cuts over.

```yaml
strategies:
 # ============================================================
 # MODE-FLAG ARCHITECTURE — v4 (pre-committed 2026-06-01)
 # ============================================================
 # The `modes` dict is the SINGLE SOURCE OF TRUTH for what runs.
 # The legacy per-strategy blocks (rsi_momentum, vwap_bounce, etc.)
 # are deprecated and removed in commit that lands the dispatcher.
 # ============================================================
 modes:

 # ----- Mode A: cross-asset trend on cash equity (CNC) -----
 swing_cash_v27:
 enabled: false # default disabled; operator flips after Phase 1 passes
 mode: backtest_only # backtest_only | paper | live
 capital_allocation_pct: 60 # of total deployed capital
 runtime: swing_cnc # determines holding-product type
 backtester_variant: cross_asset_trend_v27
 signal_module: packages.strategies.swing_cash.cross_asset_trend_v27
 cost_model: packages.core.charges:CashCNCCharges
 paper_to_live_threshold:
 capital_inr: 300000
 paper_days_profitable: 180
 paper_pf_min: 1.20
 paper_dd_max_pct: 8.0
 paper_track_d_concurrent_pass: false # not required
 kill_criteria:
 backtest:
 pf_min: 1.20
 cagr_vs_niftybees_min_pct: 2.0
 maxdd_max_pct: 25.0
 paper:
 rolling_30d_dd_max_pct: 8.0
 rolling_90d_net_min_inr: 0
 live:
 # inherits paper criteria; tightens dd
 rolling_30d_dd_max_pct: 6.0
 rolling_90d_net_min_inr: 0

 # ----- Mode B: futures swing (paper-only at retail capital) -----
 swing_fno_paper:
 enabled: false
 mode: paper # forced paper at this capital
 capital_allocation_pct: 20
 runtime: swing_fno_carry
 backtester_variant: futures_trend_pullback_v1
 signal_module: packages.strategies.swing_fno.futures_trend_pullback
 cost_model: packages.core.charges_fno:FnoFuturesCharges
 paper_to_live_threshold:
 capital_inr: 500000
 paper_days_profitable: 180
 paper_pf_min: 1.30
 paper_dd_max_pct: 10.0
 mode_a_concurrent_live: true # only live if Mode A is live
 kill_criteria:
 backtester_validation:
 # F&O engine must be validated before backtest results count
 nse_premium_match_within_sigma: 1.5
 spot_check_days: 10
 backtest:
 pf_min: 1.30
 maxdd_max_pct: 30.0
 paper:
 rolling_30d_dd_max_pct: 10.0
 rolling_90d_net_min_inr: 0

 # ----- Mode C: options spreads intraday (paper-only at retail) -----
 intraday_fno_paper:
 enabled: false
 mode: paper
 capital_allocation_pct: 10
 runtime: intraday_fno_options
 backtester_variant: options_credit_spreads_v1
 signal_module: packages.strategies.intraday_fno.options_credit_spreads
 cost_model: packages.core.charges_fno:FnoOptionsCharges
 paper_to_live_threshold:
 capital_inr: 500000
 paper_days_profitable: 180
 paper_pf_min: 1.20
 paper_dd_max_pct: 8.0 # tighter than B — single bad week can blow up
 mode_a_concurrent_live: true
 kill_criteria:
 backtester_validation:
 nse_premium_match_within_sigma: 1.5
 spot_check_days: 10
 backtest:
 pf_min: 1.20
 maxdd_max_pct: 25.0
 paper:
 rolling_30d_dd_max_pct: 8.0
 rolling_90d_net_min_inr: 0

 # ----- Mode D: cointegration pairs (research only) -----
 pairs_cointegration_research:
 enabled: false
 mode: backtest_only
 capital_allocation_pct: 0
 runtime: swing_cnc
 backtester_variant: cointegration_pairs_v1
 signal_module: packages.strategies.research.cointegration_pairs
 cost_model: packages.core.charges:CashCNCCharges
 paper_to_live_threshold:
 never_auto_promote: true # research-only, hard rule
 reason: |
 Even if backtest passes, Mode D requires running paired
 long-short positions which double the position count and
 double the cost burn. Retail capital cannot sustain this
 unless a 3-stage capital scale-up happens first (operator
 explicit decision at that point, not auto-promote).
 kill_criteria:
 backtest:
 pf_min: 1.10
 sharpe_min: 0.4
 maxdd_max_pct: 20.0

 # ----- Legacy v2.1 modes (wound down 2026-06-05) -----
 swing_combined_shorts_legacy:
 enabled: false
 mode: paper # disabled but kept for diff queries
 frozen_until: never # never re-enable
 reason: V25/V26 PF 0.04/0.01 under AngelOne (CHG-charges); wound down 2026-06-05

# ============================================================
# Global mode-routing knobs
# ============================================================
mode_router:
 # Dispatcher reads capital from this file; refuses live if gate not met
 capital_source: data/self_sufficiency.json
 capital_field: cash_inr

 # Sum of capital_allocation_pct of all enabled+(paper|live) modes
 # must be ≤ this. Backtest_only modes don't count.
 max_capital_allocation_pct: 100

 # Override to ignore capital gate — typing this string is the friction
 override_capital_gate: "" # set to "I accept ruin risk" to override

 # Per-mode paper-broker stub
 paper_broker:
 ledger_path: logs/paper_broker_<mode>_<YYYY-MM-DD>.csv
 starting_cash_inr: 100000 # per mode, reset on enable

 # Position-uniqueness policy across modes
 position_isolation:
 # If both Mode A (long RELIANCE) and Mode D's pair (RELIANCE-ONGC)
 # are active, the dispatcher does NOT net them. Each is a separate
 # row in the DB with its own mode_tag.
 net_across_modes: false
```

### 2.2 Dispatcher contract

The dispatcher (`packages/trader/mode_dispatcher.py`) is a new module
that sits BETWEEN the existing signal generators and the order-placement
layer. Its contract:

```python
class ModeDispatcher:
 """Single source of truth for what runs and where orders go."""

 def __init__(self, config: dict, capital_provider: CapitalProvider):
 self._validate_config(config) # raises on invalid schema
 self._validate_capital_gates(config, capital_provider)
 self._validate_allocation_sum(config) # ≤ 100%
 self._cost_models = self._load_cost_models(config)
 self._signal_modules = self._load_signal_modules(config)

 def active_modes(self) -> list[ModeSpec]:
 """Returns modes with enabled=True. Order: stable."""
 ...

 def route_order(self, signal: Signal) -> RoutingDecision:
 """
 - If mode is backtest_only → REFUSE (raise)
 - If mode is paper → route to PaperBroker
 - If mode is live → route to live broker, with mode_tag on DB row
 - Always tags the order with mode_tag for audit
 - Always uses the mode's configured cost_model
 """
 ...

 def kill_check(self, mode_name: str, window: str) -> KillCheckResult:
 """
 Run the mode's kill_criteria for the given window
 ('backtest', 'paper', 'live').
 Returns (passed=True/False, triggered_criteria=[...]).
 The daemon calls this once per cycle for live/paper modes;
 backtester calls it at backtest end for backtest mode.
 """
 ...

 def disable_mode(self, mode_name: str, reason: str):
 """
 Operator-callable. Writes a `[MODE-DISABLED]` event to
 daemon log, updates config in-memory, persists snapshot,
 emails operator. Mode requires explicit re-enable.
 """
 ...
```

### 2.3 The capital-gate enforcement (the "no, you cannot" layer)

`_validate_capital_gates` is the dispatcher's most important
defensive function:

```python
def _validate_capital_gates(self, config, capital_provider):
 current_capital = capital_provider.cash_inr()
 override = config["mode_router"]["override_capital_gate"]

 for mode_name, mode_spec in config["strategies"]["modes"].items():
 if not mode_spec["enabled"]:
 continue
 if mode_spec["mode"] != "live":
 continue # paper / backtest don't need capital gate

 gate = mode_spec["paper_to_live_threshold"]
 required = gate.get("capital_inr", 0)

 if current_capital < required:
 if override != "I accept ruin risk":
 raise CapitalGateError(
 f"Mode {mode_name} requires capital_inr >= {required}, "
 f"have {current_capital}. Set mode to 'paper' or override."
 )
 # Log the override loudly — it's a permanent audit-log event
 log.critical(
 f"[CAPITAL-GATE-OVERRIDE] mode={mode_name} "
 f"required={required} have={current_capital} "
 f"operator accepted ruin risk explicitly"
 )
```

The override string ("I accept ruin risk") is intentionally typed
verbatim — typing it is the friction that prevents impulse override.

### 2.4 The paper-broker isolation (the "live leakage" prevention)

`PaperBroker` is a separate class that:

1. Implements the same interface as `AngelOneBroker`
2. NEVER calls AngelOne SmartAPI (defensive — no network at all)
3. Writes its own ledger to `logs/paper_broker_<mode>_<date>.csv`
4. Uses last-close + simulated slippage (configurable per mode) for fills
5. Has its OWN tests that assert no broker-api calls happen even when
 misconfigured

Test required:
```python
# tests/integration/test_paper_broker_isolation.py
def test_paper_mode_never_calls_live_broker(monkeypatch):
 """If config says paper, the live broker module should not even
 be importable in the test process."""
 ...
```

This addresses a real risk: the existing daemon has one broker
object that is "paper or live" based on a flag. Multi-mode multiplies
the surface area for a misconfigured flag to leak live orders. Paper
broker as a SEPARATE class (not a flag on the live class) is the
defensive design.

---

## 3. Mode A spec — `swing_cash_v27` (cross-asset trend)

### 3.1 Universe (60-80 instruments)

Stored at `data/v4_universe_swing_cash.txt`, refreshed quarterly.
Seed composition:

| Category | Count | Specific symbols (seed; quarterly refresh) |
|---|---:|---|
| Nifty 50 stocks | 50 | All Nifty 50 components as of refresh date |
| Nifty Next 50 (selected) | 15 | Top 15 by 60-day ADTV from Nifty Next 50, excluding overlap with Nifty 50 |
| Equity-broad ETFs | 4 | NIFTYBEES, JUNIORBEES, BANKBEES, NIFTYIETF |
| Commodity ETFs | 2 | GOLDBEES, SILVERBEES |
| Debt ETFs | 1 | LIQUIDBEES (used as cash sweep, not a trend instrument) |
| Sector ETFs | 3 | ITBEES, PSUBNKBEES, AUTOBEES (or similar) |
| **Total** | **75** | (subject to quarterly recompute by ADTV + ETF AUM) |

Universe refresh script: `tools/refresh_v4_universe.py` (writes to
`data/v4_universe_swing_cash.txt`). Refresh logged in
`docs/changes/changes_done_<date>.md`.

LIQUIDBEES is excluded from signal generation (it's a yield product,
not a trend product) but included in the universe file as the
cash-sweep destination during low-conviction periods.

### 3.2 Signal generators

**Two complementary signals, both must fire for entry:**

#### Signal 1 — Donchian channel breakout (entry)

```
LONG entry when:
 close[today] > max(high[t-N:t]) for N = 55 days (configurable)
 AND close[today] > 200-day SMA (regime filter)
 AND volume[today] >= 1.2 * mean(volume[t-20:t]) (volume confirm)
 AND ATR%(14)[today] <= 5.0% (volatility cap; avoid blow-off tops)
 AND days_since_last_entry_in_same_symbol >= 10 (whipsaw guard)

SHORT entry: DISABLED per v3.0 finding #3 (Indian-equity short side
 structurally -EV at retail; charter §1).

EXIT when:
 close[today] < min(low[t-M:t]) for M = 20 days (Donchian exit)
 OR trailing_stop hit (see §3.4)
 OR time_in_trade > 60 trading days (forced exit — strategy is medium-term)
```

Defaults: N=55 (entry channel), M=20 (exit channel). These are the
standard Turtle / Dunn-Capital Donchian params and are NOT
operator-tunable knobs in V27. Tuning is allowed in V28+ AFTER V27's
result is known and pinned as baseline.

#### Signal 2 — Trend strength filter (gate)

```
Only consider Signal 1's entries when:
 ADX(14)[today] >= 20 (trending environment)
 AND slope_50d_SMA > 0 (medium-term up)

This is a HARD GATE, not a vote. Signal 1 firing alone is not enough.
```

### 3.3 Position sizing — volatility-targeted

Per-instrument position size is computed so that each position's
**daily 1σ risk = 0.5% of equity**:

```
risk_per_trade_inr = equity_inr * 0.005
atr_14_inr = ATR(14)[today] in INR per share
position_size_shares = round(risk_per_trade_inr / atr_14_inr)

# Then round DOWN to broker-acceptable lot (1 share for CNC delivery)
# and cap at:
position_size_inr_max = equity_inr * 0.08 # never > 8% of equity per name
```

This is **not** the v2.1 fixed-fraction sizing. It is the CTA-standard
volatility-targeted sizing. High-ATR instruments (volatile small-caps,
gold during regime shifts) get smaller positions; low-ATR instruments
(blue-chip stocks, debt-like ETFs) get larger positions. Equal-risk,
not equal-cash.

### 3.4 Trailing stop

ATR-based Chandelier stop:

```
For an open long:
 chandelier_stop = max(high since entry) - 3.0 * ATR(14)
 EXIT when close < chandelier_stop

Recomputed daily.
```

The 3.0 multiplier is fixed for V27. The classic CTA range is 2.5-3.5;
3.0 is the conservative midpoint. Tuning deferred to V28+.

### 3.5 Risk-parity capital allocation

Across the **active** positions (those signaled but not yet stopped),
capital is allocated by **inverse-volatility**:

```
For each active candidate i:
 weight_i = (1 / sigma_i) / sum(1 / sigma_j for all j)
 # sigma_i = 20-day rolling std of daily returns

allocation_i_inr = total_capital * weight_i
```

Capped by §3.3's 8%-per-name max. Result: lower-vol instruments (large
caps, ETFs) get larger allocations; higher-vol instruments get smaller.
This makes the portfolio **risk-balanced**, not **cash-balanced** —
the CTA-standard discipline.

### 3.6 Max positions + concentration

| Knob | V27 default |
|---|---:|
| Max concurrent positions | 12 |
| Max positions per sector | 3 |
| Max total long-equity exposure (sum of position notionals) | 100% of capital |
| Min positions for "fully invested" | 8 (below this, the residual goes to LIQUIDBEES) |

### 3.7 Cost model

Uses `packages.core.charges:CashCNCCharges` (post-CHG-01..05
AngelOne rates):

* Brokerage CNC: 0.1% with ₹20 cap and ₹5 minimum per order
* Stamp duty (buy CNC): 0.015%
* STT (sell CNC): 0.1% of sell value
* DP charge (sell CNC): ₹20 per ISIN per day
* Exchange + GST + SEBI per the rate constants in `charges.py`

### 3.8 Benchmark

Every V27 backtest run produces a comparison report with:

1. V27 cumulative equity curve
2. NIFTYBEES buy-and-hold on same window
3. GOLDBEES buy-and-hold on same window
4. 60/40 NIFTYBEES + GOLDBEES rebalanced quarterly
5. CAGR delta vs each benchmark
6. Maximum drawdown delta vs each benchmark
7. Sharpe delta vs each benchmark

If V27 doesn't beat **at least one** of NIFTYBEES / 60-40 by ≥ 2%
CAGR with comparable or lower MaxDD, the variant fails the gate.

### 3.9 Backtester variant manifest

Stored at `logs/backtests/<run_id>/manifest.json`:

```json
{
 "variant": "cross_asset_trend_v27",
 "charter_version": "v4.0",
 "charter_path": "docs/reviews/strategy_charter_v4_2026-06-01.md",
 "universe_file": "data/v4_universe_swing_cash.txt",
 "universe_sha256": "<computed>",
 "params": {
 "donchian_entry_n": 55,
 "donchian_exit_m": 20,
 "regime_filter_sma": 200,
 "volume_confirm_mult": 1.2,
 "atr_cap_pct": 5.0,
 "adx_min": 20,
 "risk_per_trade_pct": 0.5,
 "max_position_pct": 8.0,
 "chandelier_atr_mult": 3.0,
 "max_concurrent_positions": 12,
 "max_per_sector": 3
 },
 "cost_model": "CashCNCCharges:angelone:2026-06-01",
 "window_start": "<set per run>",
 "window_end": "<set per run>",
 "initial_capital_inr": 100000
}
```

### 3.10 Stop criteria (the "V27 is dead" rules)

| # | Backtest stop criterion | Reading |
|---|---|---|
| A1 | PF < 1.10 over 5-year window | V27 has no edge at any size; abandon |
| A2 | PF in [1.10, 1.20) over 5-year window | Borderline; do not advance to paper. Try V28 with one parameter changed (see §3.11). Limit: 3 V-variants total. |
| A3 | PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% | V27 has edge but not enough to justify the cost burn. Do not advance to paper. Treat as "academic interest only". |
| A4 | PF ≥ 1.20 AND CAGR ≥ NIFTYBEES + 2% AND MaxDD ≤ 25% | **PASS** — advance to Phase 2 paper-mode. |
| A5 | MaxDD > 25% with any PF | Stop — drawdown profile is incompatible with capital base. |

### 3.11 V28+ retune budget

If V27 falls in A2 (borderline), one retune attempt is permitted per
the rules:

* V28 may change EXACTLY ONE parameter from §3.9. Operator picks
 which one BEFORE seeing the backtest result.
* V29 may change ANOTHER parameter, but only if V28 also lands in
 A2 (not if V28 worsens — that closes the variant family).
* V30 is the FINAL retune. After V30, Mode A is closed for at least
 90 days (anti-temptation cooldown).

The "limit 3 V-variants total" rule prevents v3.0's anti-pattern
where the operator burns calendar time tuning parameters that don't
have monotonic effects.

---

## 4. Mode B + C spec — F&O paper-mode

### 4.1 Why F&O is harder than cash

The cash backtester is well-tested (V25/V26 ran 600+ days with 190+
trades and the result is internally consistent). F&O introduces SIX
new failure modes the cash engine doesn't have:

1. **Expiry handling.** Weekly options expire Thursday; monthly
 expire last Thursday of month. Positions left open through expiry
 are auto-exercised or auto-square-off depending on moneyness. The
 backtester must model this without ambiguity.
2. **Lot sizes.** Nifty fut = 75 shares; Bank Nifty fut = 25 shares;
 stock futures vary (RELIANCE = 250). Lot size changes quarterly.
 The engine needs current lot tables and historical lot tables.
3. **SPAN+exposure margin.** Margin is not a fixed % of notional; it's
 computed daily by NSE clearing on SPAN methodology. The engine
 needs daily margin requirement to know if a position is even
 affordable. Wrong margin model → wrong position count → wrong P&L.
4. **STT asymmetry.** STT on options sell is 0.05% of premium for
 the seller; STT on exercise (ITM options at expiry) is 0.125% of
 settlement value for the buyer. This dwarfs all other costs near
 expiry. The engine must distinguish per-leg.
5. **Implied volatility / option pricing.** Backtester options
 premiums must come from EITHER (a) NSE-published historical
 premia (preferred — actual market prices) OR (b) Black-Scholes
 with historical IV. Source-of-truth matters; results will differ
 by 5-15% between approaches.
6. **Bid-ask spread + market impact.** Options OI/volume profiles
 are MUCH more uneven than cash equity. ITM options at the
 strike-adjacent ATM have tight spreads; OTM strikes 5+ away have
 huge spreads. Engine must model fill at mid + half-spread + slippage
 by liquidity tier.

The F&O backtester extension (`packages/research/backtest_fno.py`)
must handle all six. It is the **single largest engineering
investment** in v4.

### 4.2 F&O backtester validation gate

Before any Mode B or C backtest result is considered interpretable,
the engine MUST pass this validation:

1. **10-day spot-check on Nifty weekly options.** Pick 10 days in
 the last 6 months at random. For each day, pick 5 strikes around
 ATM. Compare backtester's computed/loaded option premium against
 NSE-published EOD premium.
2. **Pass criterion:** for ≥ 90% of (day, strike) pairs, computed
 premium is within 1.5σ of NSE-published premium (where σ is the
 standard deviation of premium across the day's trading window per
 NSE).
3. **Persist validation report** at
 `docs/reviews/fno_backtester_validation_<date>.md` per
 `repo-conventions`. Without this report, ANY Mode B / C backtest
 result is informational only, not decision-grade.

This gate is non-negotiable. F&O backtests with un-validated engines
have produced years of academic-paper-quality results that are
silently wrong because the IV surface used was synthetic.

### 4.3 Mode B — Futures trend pullback (swing)

**Universe:** Nifty current-month futures + Bank Nifty current-month
futures + top 5 stock futures by OI (RELIANCE, HDFCBANK, INFY, TCS,
ICICIBANK or current quarter's top 5).

**Signal:** Same as Mode A's Donchian + ADX, computed on the
underlying spot (not the futures). Entry in the futures contract.

**Sizing:**
* Margin-aware. Per-trade margin (SPAN+exposure) cannot exceed 20%
 of equity per position.
* Max concurrent: 3 positions (each is large).

**Roll logic:**
* Roll from current-month to next-month on T-2 (Tuesday of expiry
 week) regardless of P&L. Avoid expiry-day liquidity holes.
* Roll costs (bid-ask of both legs + ₹40-ish brokerage) are modeled
 explicitly per trade.

**Stop:** ATR-based as Mode A. Time-stop at 15 trading days.

**Kill criteria:** PF < 1.30 backtest OR MaxDD > 30%.

### 4.4 Mode C — Options credit spreads (intraday)

**Universe:** Nifty weekly options only (Bank Nifty deferred to
Mode C v2 if Mode C v1 shows promise — Bank Nifty OI is uneven and
spreads are wider; not worth modeling first).

**Signal — bear call spread** (sell call, buy higher call):
```
On Monday-Wednesday of expiry week, with VIX-India in [12, 22]:
 Sell 1x ATM-call (current week, Thursday expiry)
 Buy 1x ATM-call + 200pts (defines max loss)
 Hold to:
 - Thursday close (forced expiry close)
 - OR 50% max-profit (cover both legs)
 - OR stop at 2x credit received (cover both legs)
```

**Mirror — bull put spread** (sell put, buy lower put): same logic
mirrored. Direction chosen by spot's position vs 20-DMA:
above → bear call; below → bull put.

**Sizing:**
* 1 spread = 1 lot = 75 (Nifty). Premium credit typically ₹3,000-8,000
 per spread; max loss ₹15,000 per spread (200pt spread × 75).
* Max concurrent spreads: 3.

**Kill criteria:** PF < 1.20 backtest OR a single trade loses more
than 2.5x credit received (= engine modeling error or strategy is
broken).

### 4.5 Why F&O modes are paper-only at ₹120k

| Requirement | At ₹120k capital |
|---|---|
| 1 lot Nifty futures margin | ₹2.8L SPAN+exposure → **not affordable** |
| 1 spread Nifty options (max loss ₹15k) | Affordable but represents 12.5% of equity at risk per spread — 3 spreads = 37.5% concurrent risk → **portfolio-fragile** |
| F&O annual STT + brokerage drag at active trading | ₹15-30k/yr at retail volumes → exceeds entire capital base's expected return |

The paper-mode trigger to live is ₹500k for both Modes B and C
because that is the capital level at which 1 Nifty futures lot
margin (₹2.8L) is < 60% of equity AND options-spread max-loss-per-trade
is < 3% of equity (the textbook risk-per-trade ceiling).

Operator may run Modes B and C in paper-mode at ANY capital — that's
the point of paper — but the capital gate refuses to flip them to
live without ₹500k available.

---

## 5. Mode D spec — Cointegration pairs research

### 5.1 Universe

Nifty 200 stocks. Pair candidates computed daily by:

1. For each (stock_a, stock_b) pair in Nifty 200 (~20,000 pairs):
 - Compute 252-day rolling Augmented Dickey-Fuller test on the
 hedge-ratio spread `log(a) - β * log(b)` where β is OLS slope.
 - Compute half-life of mean reversion via Ornstein-Uhlenbeck fit.
2. Filter: ADF p-value < 0.05 AND 5 ≤ half-life ≤ 30 trading days.
3. Rank by Sharpe of pair's spread returns over last 252 days.
4. Top 20 pairs become the active set, refreshed monthly.

### 5.2 Signal

Per pair (a, b) with hedge ratio β:
```
z_score = (spread[today] - rolling_mean_60d(spread)) / rolling_std_60d(spread)

ENTRY (long pair = long a, short b):
 z_score <= -2.0 AND spread is below rolling mean

ENTRY (short pair = short a, long b):
 z_score >= +2.0 AND spread is above rolling mean

EXIT:
 abs(z_score) <= 0.5
 OR z_score crosses opposite-sign 2σ band (kill)
 OR half_life * 2 trading days elapsed (time-stop)
```

### 5.3 Cost model

Same `CashCNCCharges` as Mode A — but **doubled per trade** (each
pair entry/exit is 2 legs = 4 transactions over the trade lifecycle).
This is the dominant reason pairs trading is cost-eaten alive at
retail.

### 5.4 Kill criteria

| # | Criterion | Reading |
|---|---|---|
| D1 | Top-20 pairs PF < 1.0 in backtest | Cointegration is dead at retail; close research |
| D2 | Top-20 pairs PF in [1.0, 1.1) | Marginal; one variant retry with monthly (not daily) cointegration recompute |
| D3 | Top-20 pairs PF ≥ 1.1 AND Sharpe ≥ 0.4 | Document; do NOT auto-promote (research-only per §2.1) |

If D3, the operator decides separately whether to invest dev time
to add pairs to paper-mode (additional ~2 weeks of router work).

---

## 6. Backtester extensions required

### 6.1 Cash equity (Mode A) — minimal additions

| Extension | New module | Effort |
|---|---|---|
| ETF universe loader | `packages/research/instruments/etf_universe.py` | 2 days |
| Donchian signal | `packages/research/signals/donchian.py` | 1 day |
| ADX signal | `packages/research/signals/adx.py` | 1 day |
| Volatility-targeted sizer | `packages/research/signals/volatility_sizer.py` | 2 days |
| Risk-parity allocator | `packages/research/signals/risk_parity.py` | 3 days |
| NIFTYBEES benchmark in comparison report | extension to existing comparison module | 2 days |
| Universe manifest with SHA | extension to existing run manifest | 1 day |

**Total Mode A: ~12 person-days = ~2 weeks part-time.**

### 6.2 F&O (Modes B, C) — large additions

| Extension | New module | Effort |
|---|---|---|
| F&O EOD bhav copy loader (NSE archive) | `packages/research/instruments/fno_universe.py` | 5 days |
| Historical lot-size + tick-size tables | data files + loader | 3 days |
| Options pricing (Black-Scholes + IV calc) | `packages/research/options_pricing.py` | 5 days |
| NSE-published premium loader (preferred over BS) | included in fno_universe.py | 3 days |
| Expiry calendar + auto-exercise | `packages/research/fno_expiry.py` | 4 days |
| SPAN+exposure margin model (approximation) | `packages/research/fno_margin.py` | 7 days |
| F&O charges model | `packages/core/charges_fno.py` | 4 days |
| F&O backtester orchestrator | `packages/research/backtest_fno.py` | 10 days |
| 10-day NSE premium validation suite | `tests/integration/test_fno_backtester_validation.py` | 5 days |
| Roll logic for futures | included in backtest_fno.py | 3 days |
| Multi-leg options position object | extension to existing position model | 3 days |

**Total F&O: ~52 person-days = ~8-10 weeks part-time** (with realistic
context-switching overhead and unknowns).

This is the single largest dev block in v4. The Phase 3 in the
path-forward assessment §5 is sized for this.

### 6.3 Cointegration (Mode D) — moderate additions

| Extension | New module | Effort |
|---|---|---|
| ADF test per-pair | `packages/research/cointegration.py` | 3 days |
| Half-life via OU fit | included in cointegration.py | 2 days |
| Pair-position coupling in backtester | extension to position model | 3 days |
| Pair selection + ranking | included in cointegration.py | 2 days |

**Total Mode D: ~10 person-days = ~2 weeks part-time.**

### 6.4 Mode dispatcher (cross-cutting)

| Extension | New module | Effort |
|---|---|---|
| ModeDispatcher class | `packages/trader/mode_dispatcher.py` | 5 days |
| PaperBroker class | `packages/trader/paper_broker.py` | 4 days |
| Config schema validator | added to existing config loader | 2 days |
| Capital gate enforcer | included in ModeDispatcher | 2 days |
| Position model `mode_tag` migration | DB migration + tests | 2 days |
| Per-mode kill-check orchestration | included in ModeDispatcher | 3 days |
| Dispatcher unit tests | `tests/unit/test_mode_dispatcher.py` | 4 days |
| Paper-broker isolation integration test | `tests/integration/test_paper_broker_isolation.py` | 2 days |

**Total dispatcher: ~24 person-days = ~4 weeks part-time.**

### 6.5 Aggregate effort

| Track | Effort (person-days) |
|---|---:|
| Mode A backtester extensions | 12 |
| Mode dispatcher + paper-broker (cross-cutting) | 24 |
| Mode D backtester extensions | 10 |
| F&O backtester extensions (Modes B, C) | 52 |
| Test coverage for everything above | included in each row |
| **Total v4 build effort** | **~98 person-days = ~5 calendar months at 20 hrs/wk** |

This roughly aligns with the path-forward §5 phase calendar.

---

## 7. Test plan

Per `test-conventions` skill: every new module gets unit tests, every
cross-module behaviour gets an integration test, every kill-criterion
gets a regression test.

### 7.1 Pin tests (the "this number is real" tests)

For each Mode, a backtest result snapshot is pinned in:

* `tests/integration/test_mode_a_v27_pin.py` — runs V27 on a frozen
 60-day Nifty 100 subset, asserts PF / CAGR / MaxDD match the
 pinned-on-charter-day values within 0.5% tolerance. Regresses if
 backtester behavior changes.
* `tests/integration/test_mode_b_pin.py` — runs Mode B on a frozen
 30-day Nifty futures window.
* `tests/integration/test_mode_c_pin.py` — runs Mode C on a frozen
 8-week options window.
* `tests/integration/test_mode_d_pin.py` — runs Mode D on a frozen
 60-day Nifty 200 subset.

These tests are the **reproducibility contract**. They run in CI;
they fail loudly if the engine's behavior changes silently.

### 7.2 Dispatcher contract tests

`tests/unit/test_mode_dispatcher.py` covers:

* Schema validation rejects malformed configs (missing fields,
 invalid mode types, capital_allocation_pct sum > 100).
* Capital gate refuses `mode: live` if `cash_inr` below threshold.
* Capital gate accepts override only if exact string match.
* `mode: backtest_only` modes are NEVER routed by `route_order`.
* `mode: paper` modes are NEVER routed to the live broker.
* `mode_tag` is correctly stamped on every order's DB row.
* Cost model per mode is correctly loaded and cannot leak across.
* Two modes with overlapping symbols produce TWO DB rows (not
 netted) when `position_isolation.net_across_modes: false`.

### 7.3 Paper-broker isolation tests

`tests/integration/test_paper_broker_isolation.py` covers:

* In a process where only `mode: paper` modes are enabled, the live
 broker module is mock-monkey-patched to fail loudly on any method
 call. Test asserts the daemon completes a full cycle with no
 such failure.
* PaperBroker writes to `logs/paper_broker_<mode>_<date>.csv` and
 NEVER to `logs/trades.csv` (which is live-broker output).
* PaperBroker's ledger reconciles against signal-audit CSV daily.

### 7.4 F&O engine validation suite

`tests/integration/test_fno_backtester_validation.py`:

* Loads 10 days of historical NSE F&O bhav copy.
* Picks 5 strikes around ATM for each day on Nifty weekly.
* Asserts computed/loaded premium is within 1.5σ of EOD-published.
* Runs at `pytest --slow` only (~5 min runtime).

### 7.5 Charges-fno pin tests

`tests/unit/test_charges_fno.py`:

* 22 tests minimum, parallel to `test_charges_angelone_2026_06_01.py`.
* Covers futures intraday vs delivery brokerage.
* Covers options sell-side STT (0.05%) vs buy-side STT (0.125% if
 ITM at expiry).
* NUM-10-style invariant: round-trip total equals sum of legs.

### 7.6 Backwards compatibility

The dispatcher's cutover commit MUST:

1. Backfill `mode_tag = 'legacy_v2_1'` on all existing
 `data/trading_agent.db` `trades` and `positions` rows in a
 migration script.
2. Keep `swing_combined_shorts_legacy` mode in the config with
 `enabled: false` for ≥ 90 days after cutover (so the DB rows
 still resolve to a known mode).
3. Existing tests pass unmodified — any test failure indicates a
 behaviour change the dispatcher is silently masking, which is the
 bug.

---

## 8. Capital scaling gates (the "no, you cannot" rules — restated)

Cross-referenced from path-forward §4.4:

| Mode | Min capital for `mode: live` | Mode A live concurrent required? |
|---|---:|---|
| `swing_cash_v27` | ₹300,000 | n/a (this IS Mode A) |
| `swing_fno_paper` → live | ₹500,000 | Yes, ≥ 6 months profitable |
| `intraday_fno_paper` → live | ₹500,000 | Yes, ≥ 6 months profitable |
| `pairs_cointegration_research` | **never auto-promote** | — |

Capital is read at daemon-startup from `data/self_sufficiency.json`
field `cash_inr`. The dispatcher refuses `mode: live` if the gate
isn't met, unless `mode_router.override_capital_gate` is set to the
exact string `"I accept ruin risk"`.

The override exists because a human override that requires no
ceremony is no override at all. But it logs CRITICAL and emits a
permanent audit-log event that future code-bug-review will surface.

---

## 9. Anti-temptation discipline (per-mode pre-commits)

Cross-referenced from path-forward §9 — restated in technical terms:

1. **Do not edit V27 params mid-paper.** Once Mode A is in paper, the
 params in §3.9 are frozen for the 180-day window. Param edits
 require ending the paper window with whatever data was collected
 and starting a new V-variant with full backtester re-run.

2. **Do not skip the F&O engine validation (§4.2).** No Mode B or C
 backtest result counts unless the validation suite passed within
 the last 30 days.

3. **Do not promote a mode from paper to live mid-window.** The
 180-day paper-profitable window is the discipline; mid-window
 promotion forfeits the validation.

4. **Do not add a 5th mode.** Modes A-D are the v4 commitment. Mode E
 requires a v5 charter, which requires a path-forward refresh.

5. **Do not increase Mode B / C capital allocation above 30% combined.**
 F&O concentration risk is non-linear; the §2.1 allocation caps are
 the pre-commit.

6. **Do not interpret Mode D edge as live-trade signal.** Mode D is
 research-only by charter §2.1 / §5.4. Even D3-class results require
 a separate operator decision to invest dev time for paper-mode.

7. **Do not amend this charter under emotional load** (path-forward §9.8).
 7-day cooling-off period after any kill-criterion fires.

8. **Do not skip the universe refresh quarterly.** Stale universes
 (e.g., Yes Bank in Nifty 50 in 2018 vs out by 2020) produce
 silently wrong backtest results.

9. **Do not run V28+ until V27 result is pinned.** Tuning before the
 baseline is known is the v3.0 anti-pattern this charter inherits
 the prohibition against.

---

## 10. Open questions for operator (gate to Phase 1)

Operator must answer in writing (path-forward §10) before any v4
code lands. These are the unresolved design questions:

| # | Question | Adviser's recommendation |
|---|---|---|
| Q1 | Donchian channel period for V27 entry: 55 (Turtle standard) or 100 (longer-term, fewer trades)? | **55.** 100 has more lag and reduces sample size on a 5-year backtest. Tune in V28+ if V27 borderline. |
| Q2 | Volatility-target risk-per-trade: 0.5% (charter default) or 1.0% (more aggressive)? | **0.5%.** Conservative; can dial up if Mode A passes backtest with margin to spare. |
| Q3 | NIFTYBEES benchmark window: same 5-year window as V27 backtest, or rolling 5-year? | **Same window** for primary; rolling for sensitivity check in the comparison report. |
| Q4 | F&O paper-broker fills: NSE EOD premium (slowest, most realistic) or BS-with-IV (faster, less realistic)? | **NSE EOD for all decision-grade results.** BS-with-IV permissible for dispatcher unit tests only. |
| Q5 | Dispatcher cutover strategy: hard cutover (one commit) or feature-flag rollout (legacy + new dispatcher coexist)? | **Hard cutover after Phase 1 backtest passes.** Feature-flag coexistence doubles surface area for no real safety benefit; the dispatcher's own paper-broker isolation IS the safety. |
| Q6 | Mode D research timeline: build at Phase 1 (with Mode A), Phase 3 (with F&O), or Phase 5 (after Mode A live)? | **Phase 5 or never.** Mode D is lowest priority; if Phase 5 hits, operator decides then. Reserves dev cycles for the higher-EV tracks. |
| Q7 | Live-trading capital source: stick with `data/self_sufficiency.json` `cash_inr` (operator-edited) or read from AngelOne API daily? | **Read from AngelOne API daily** for live modes. Eliminates a class of misconfiguration bug. Falls back to self_sufficiency.json if API unreachable. |
| Q8 | Override-capital-gate string ("I accept ruin risk"): keep verbatim or shorten to "override"? | **Keep verbatim.** The friction IS the point. |
| Q9 | Phase 1 calendar: start Mon 2026-06-08 (immediately after wind-down) or wait 1 week to let wind-down settle? | **Start 06-08.** Wind-down is administrative; no reason to pause dev. |
| Q10 | Cost-of-fight cumulative tracking: where? | **`data/cost_of_fight_<YYYY>.csv`** updated monthly; cumulative against PK3 (₹100k) and PK1 (18mo) gates. Auto-emit reminder at PK3 75% and PK1 12mo. |

---

## 11. Acceptance criteria — when this charter is "done"

Charter is considered fully executed when one of these terminal states
is reached:

| Terminal state | Definition |
|---|---|
| **SUCCESS — Mode A live** | Mode A live for ≥ 180 calendar days with rolling-90d net > 0 and rolling-30d DD < 6%. Charter is "in steady state". |
| **PARTIAL SUCCESS — Mode A paper-stable** | Mode A in paper for ≥ 180 days, profitable, but capital below ₹3L gate. Project produces no income but engine is validated. |
| **TIMEOUT — PK1 fires** | 18 calendar months elapsed without any track clearing live-gate. Liquidate to NIFTYBEES per path-forward §6. |
| **STOP-LOSS — PK3 fires** | Cumulative direct cost > ₹100k without any track in paper-profitable state. Liquidate per path-forward §6. |
| **GRACEFUL EXIT — operator decision** | Operator decides at any quarterly review that the math no longer works for them. Liquidate, archive, learn. |

Charter is then either superseded by a v5 charter (after at least
30 days of post-mortem) or formally closed.

---

## 12. Cross-references

* [`path_forward_assessment_2026-06-01.md`](path_forward_assessment_2026-06-01.md) — **the decision paper this charter implements**
* [`brutal_review_2026-06-01.md`](brutal_review_2026-06-01.md) — Session 1-3 evidence
* [`strategy_reference_review_2026-06-01.md`](strategy_reference_review_2026-06-01.md) — Virtu / Renaissance / CTA / retail strategy critique
* [`../freeze/freeze_v3.0_charter_2026-05-30.md`](../freeze/freeze_v3.0_charter_2026-05-30.md) — v3 swing charter (predecessor; being wound down)
* [`../freeze/wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md) — Friday verdict gate
* [`../freeze/verdict_meeting_packet_2026-06-05.md`](../freeze/verdict_meeting_packet_2026-06-05.md) — Friday meeting packet
* [`../findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md) — CHG-01..05 / NUM-10 evidence
* [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md) — AngelOne PF re-derivation
* `tests/unit/test_charges_angelone_2026_06_01.py` — precedent for the F&O charges pin tests
* `packages/core/charges.py` — post-CHG cash cost model (Mode A inherits)
* `packages/research/backtest_ensemble.py` — engine being extended

---

*Last updated: 2026-06-01 12:40 IST. Charter is pre-committed v1.0
in parallel with `path_forward_assessment_2026-06-01.md`. Activates
2026-06-08 (Mon) post-wind-down, conditional on operator answering
§10 Q1-Q10 in writing first.*
