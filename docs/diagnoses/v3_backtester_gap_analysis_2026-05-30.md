# v3.0 Backtester Capability Gap Analysis (Phase A1)

**Phase:** A1 — backtester capability gap analysis (per `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §6).
**Started:** 2026-05-30 ~09:10 IST. **Author:** trading agent.
**Frozen surface check:** trader VM untouched (museum mode, per charter §6.1). ALL changes catalogued below land on backtester VM only when Phase A2 begins.

This document is the OUTPUT of Phase A1. It catalogues, for each of the 8 v3 requirements, what the backtester supports today, what it does not, where the gap lives in the source, and how much effort A2 will need.

The single most important property of this audit: it is **read-only with respect to the trader VM**. No code changes happen here. A2 implements the gaps and lands them as small commits with regression tests; A3-A5 follow.

---

## 0. Method

For each requirement:

* Read the relevant source files (`packages/research/backtest_ensemble.py`, `packages/research/battery.py`, `packages/core/charges.py`, `packages/core/data_handler.py`, plus selected tests).
* Trace the data path end-to-end (config → BacktestConfig → engine.run → portfolio → charges).
* Classify status as one of:
  * **SUPPORTED** — backtester already does this; v3 needs config-only or zero changes.
  * **PARTIAL** — backtester supports a subset; targeted change needed.
  * **GAP** — feature does not exist; new code path required.
* Pin code references at file:line so A2 implementers (and reviewers) land on the right place without re-deriving.
* Estimate engineering effort assuming a single backtester change with a unit-test guard.

---

## 1. Daily candle frame as primary

**Status: SUPPORTED.**

* `EnsembleBacktester._INTERVAL_ALIASES` already includes `"1d": "1d"` (`packages/research/backtest_ensemble.py:194-198`). The interval string passes through to `DataHandler.download_historical_for_backtest`.
* `YahooFinanceDataSource._YF_INTERVAL_MAP` includes `"1d": "1d"` (`packages/core/data_handler.py:344-347`). Yahoo daily data is rate-limit-friendly (no per-day chunking limits, ~5 years available).
* `battery.py` interval pass-through preserves `"1d"` correctly. The special-case mapping at `packages/research/battery.py:1411-1413` rewrites only the intraday aliases (`"5m"/"15m"/"30m"/"1m"` → `"min"` suffix); `"1d"` flows through untouched.
* Bar OHLC iteration (`backtest_ensemble.py:430-440`) and intra-bar SL/TP detection (`_detect_intrabar_exit` at `:866-924`) are candle-frame-agnostic — they work on whatever bar the engine receives.

**Sub-gaps that need attention but are not engine-changes:**

* `BacktestConfig.apply_dead_hour: bool = True` (default, `:80`) and `DEAD_HOUR_BLOCKS = [(12, 0, 13, 0)]` (`:66`). Dead-hour gating is intraday-specific and meaningless for daily bars. v3 variants must set `backtest_gates.apply_dead_hour: false` in their config override (single-line variant entry; no engine change).
* `losses_per_stock` resets per IST day (`:425-428`), and `max_losses_per_stock` defaults to 2 (`BacktestConfig.max_losses_per_stock`, `:79`). On daily bars, "max losses per stock per day" tautologically caps to 1 trade per stock per day (one bar). v3 variants must raise this to a multi-trade cap or disable. Config-only change.
* The dead-hour cache (memoised by `(hh, mm)` at `:932-947`) is harmless on daily bars (one cache key, hits every event after the first) — no work needed.

**Effort: 0h to engine. ~30min to v3 variant configs in A4 (set `apply_dead_hour: false`, `max_losses_per_stock: 99`).**

---

## 2. CNC product (no 15:15 flat-out, no MIS leverage)

**Status: SUPPORTED.**

* `BacktestConfig.product_type: str = "INTRADAY"` (`backtest_ensemble.py:83`). The dataclass field exists; v3 variants override to `"DELIVERY"`.
* The harness already plumbs `product_type` from config (`battery.py:483`: `cfg.get("execution", {}).get("product_type", "INTRADAY")`).
* `EnsembleBacktester` passes `product_type` to `Portfolio` (`:265`) and to `RiskManager.is_trade_worth_taking` (`:649`). Both honour it via `core.charges.compute_round_trip(..., product=product_type)`.
* **No 15:15 flat-out logic exists in the backtester.** Confirmed by `rg`-equivalent grep on `packages/research/`: no matches for `15:15` / `flat_intraday` / `close_intraday`. All such logic lives in `trading_agent.py` (10 hits at trader-side lines 1264, 3513, 3872, 3942, 3974, 6074, 6674 — all in the daemon's session-end path). The backtester naturally allows multi-day holds because nothing closes positions at any per-day boundary. (See §3 for the corollary.)
* `mis_short_margin_pct` (`:90`) is INTRADAY-specific and irrelevant for CNC delivery — `Portfolio.open_position` already guards this: tested in `tests/unit/test_audit_2026_05_28_misc.py:205-220` (`test_short_open_delivery_locks_full_notional_even_with_margin`).

**Sub-gaps that need attention:**

* CNC short positions must be impossible at the broker level (delivery shorts are intraday-MIS-only on NSE; you cannot hold an overnight short on cash equity). The backtester currently allows `SELL` ensemble signals to open short positions even with `product_type="DELIVERY"`. v3 variants will set `risk.allow_shorts: false` (the existing flag from `BacktestConfig.allow_shorts`, `:102`); the engine already rejects shorts when this is False (`:591-596`). **Already supported via existing flag — config-only change.**

**Effort: 0h to engine. v3 variants set `execution.product_type: DELIVERY` and `risk.allow_shorts: false` in their config overrides.**

---

## 3. Multi-day position holds (overnight rollover)

**Status: SUPPORTED.**

* The event loop iterates `_merge_bars` chronologically (`:339-359`); positions persist across day boundaries because nothing in the loop closes them at the rollover.
* The B-01 day-rollover block (`:421-428`) only resets the `losses_per_stock` counter; it does NOT touch `portfolio.positions`.
* The end-of-backtest flatten at `:683-710` is a one-shot at the FINAL bar of each symbol — not a daily flush.
* SL/TP intra-bar detection (`_detect_intrabar_exit`) operates on whatever bar arrives; on daily bars, it asks "did this day's high/low cross my level". Correct behaviour for swing.

**Sub-gaps:**

* Overnight gaps. On a daily bar, if the previous-day SL is breached at the next-day OPEN (gap-down through SL on a long), `_detect_intrabar_exit` already handles this: `min(open_p, sl)` if `open_p < sl` for longs (`:904-906`), `max(open_p, sl)` for shorts. Gap-fill priced realistically — adverse to the strategy. Good.
* Holding period accounting. `holding_minutes` is computed from entry/exit timestamps and uses bar timestamps (`:466, :515, :677, :706`). On daily bars, holding_minutes will be in 1440-min multiples (one trading day = 24h apart on calendar terms but only ~6h of market time). This is consistent but the units may surprise. **Not a bug.** A2 should add a `holding_days` field for swing readability — single line in `_trade_to_dict`.

**Effort: ~30min for `holding_days` field + variant docs noting the day-counter behaviour.**

---

## 4. Next-day-open entry fills

**Status: GAP. This is the largest A2 deliverable.**

* Currently, on a non-HOLD ensemble signal, the engine fills at the SAME bar's close + slippage:
  ```
  entry_price = self._apply_slippage(close, agg.signal.name, exit=False)
  portfolio.open_position(symbol=symbol, side=..., price=entry_price, ...)
  ```
  (`backtest_ensemble.py:625-678`).
* On 5-min bars this is acceptable (next 5-min bar is 5 min later; the difference between "this bar's close" and "next bar's open" is sub-perceptible). On daily bars, this means **signal generated from day N's close fills at day N's close** — which is unrealistic for a real-world swing strategy that decides at EOD and places orders for the next session.
* v3's two strategies (Trend Pullback, 20d-High Breakout) both explicitly specify "Entry: next day open" (`docs/freeze/freeze_v3.0_charter_2026-05-30.md` §2). A backtest that fills at day-N close systematically over-states entry-side timing edge.

**Required change (A2):**

1. Add `BacktestConfig.fill_mode: Literal["close_plus_slippage", "next_bar_open"] = "close_plus_slippage"` (preserves existing v2.1 behaviour as default).
2. In the entry execution path (`backtest_ensemble.py:625`):
   * If `fill_mode == "next_bar_open"`, look up the symbol's df row at index `i+1` (the bar after the signal bar). The current event loop yields `(ts, symbol, bar, df_slice)` from `_merge_bars`; we'd extend `_merge_bars` to also yield the lookahead bar (or more cleanly, pass the full per-symbol df and current index into the event tuple).
   * Edge case: signal on the FINAL bar of a symbol (no `i+1`). Drop the signal silently and increment a new gate-stat counter `gate_stats.no_next_bar` so it's visible. Mirror the existing `_bump_equity` pattern.
   * Slippage applied to the next bar's open price, not the signal bar's close.
3. Strategy-side `TradeSignal` already carries no fill price — strategies just emit BUY/SELL; the engine owns the fill. No strategy-side change needed.
4. Unit test: hand-construct a 3-bar synthetic df where bar 1 emits a BUY signal (at close 100), bar 2 opens at 102 (gap-up). Assert entry fill = 102 + slippage in `next_bar_open` mode and = 100 + slippage in legacy mode. Verify that legacy v2.1 variants are byte-identical when `fill_mode` is omitted (default preserves behaviour).
5. Documentation in `BacktestConfig.fill_mode` docstring noting v3 swing variants must set this; legacy 5-min variants leave it default.

**Effort: ~3-4h** (engine change ~1.5h, unit tests ~1h, end-to-end smoke on a known v2.1 variant to confirm byte-identical legacy results ~1h, comments + docstring + battery-config plumbing ~30min).

**Risk:** the `_merge_bars` change to expose the next-bar lookup requires care — the heap-merge across symbols means we cannot trivially "peek" without breaking the chronological-event invariant. The cleanest approach is to NOT mutate `_merge_bars` at all and instead pass `market_data[symbol]` + index `i` into the event payload, and let the entry path do the `df.iloc[i+1]` lookup directly. Per-symbol df is already in scope (`market_data` is captured in the closure).

---

## 5. CNC charges (delivery brokerage + DP / CDSL)

**Status: SUPPORTED. (One small misconception in the v3 charter to resolve in A2 docs.)**

* `core.charges.compute_round_trip(..., product="DELIVERY")` (`packages/core/charges.py:150-218`) and `compute_one_leg(..., product="DELIVERY")` (`:221-254`) are fully implemented:
  * Brokerage: 0% for DELIVERY (Zerodha-style; `:101` `BROKERAGE_DELIVERY_PCT = 0.0`).
  * STT: 0.1% on BOTH buy and sell legs for DELIVERY (`:105` `STT_DELIVERY = 0.001`), versus 0.025% on SELL only for INTRADAY.
  * DP charges: ₹13.5 per SELL ORDER + 18% GST = ₹15.93 (`:113-114`, `:204-206`).
  * Stamp duty, exchange txn, SEBI fee, GST: shared across both products.
* `Portfolio.commission_pct` is dead since the move to per-leg charges (`packages/research/backtest.py:40-60` warns the operator if it's set). The only thing that actually computes charges is `core.charges`, gated on `product_type`.
* Unit tests in `tests/unit/test_audit_2026_05_28_misc.py:1554-1559` (`test_round_trip_total_equals_sum_of_legs_delivery`) verify the round-trip identity for DELIVERY. Coverage exists.

**Misconception to correct in A2 / charter footnote:**

The charter (and the advisor plan that informed it) refers to "CDSL ₹13.5/ISIN/day". This is **incorrect terminology**. Real Zerodha (and most NSE retail brokers) charge DP at ₹13.5 + GST **per SELL order**, NOT per day. There is a separate annual demat A/C maintenance charge (~₹300/year), which `charges.py` does NOT model — and which is not relevant at swing-trade resolution because it's a flat fixed cost not tied to trades. `compute_one_leg` correctly applies DP only to `product="DELIVERY"` AND `side="SELL"` (`:250-251`). **No engine change. A2 should add a one-line clarification comment to the v3 charter or a charges.py docstring noting "per SELL, not per day".**

**Cost impact preview for v3 sizing:**

| Trade | INTRADAY (5-min, MIS) | DELIVERY (daily, CNC) |
|---|---|---|
| ₹5,000 round-trip | ~₹22 | ~₹26 |
| ₹50,000 round-trip | ~₹35 | ~₹85 |

Per-trade absolute cost is similar at small notional, slightly higher for CNC at large notional (STT dominates). The advisor's commission-drag math (76-146% of |monthly PnL| for v2.1 MIS, 5-15% for v3 CNC) is correct: it derives from the **trade frequency drop**, not from per-trade cost. v3 will do 8-15 trades/month vs v2.1's 30-80, so even at slightly higher per-trade cost, the monthly drag falls 5-10×.

**Effort: 0h to engine. ~15min for the per-day vs per-SELL clarification footnote in the charter (or, alternatively, leave the charter unchanged and put the footnote in the A2 commit's PR description).**

---

## 6. 30-stock universe with valid_from / valid_to

**Status: PARTIAL. Survivorship bias is a documented but non-zero risk.**

* `battery.py --universe-file` (`:1238-1243`) accepts a JSON of shape `{"universe": ["RELIANCE", ...]}`. Switching to a Nifty 30 universe = creating a new fixture file (`tests/fixtures/v3_universe_top30.json` per A4 plan).
* Universe fixtures **do not currently support `valid_from`/`valid_to` per stock.** The current `tools/_freeze_battery_v2_universe.py` produces a flat list with no time validity.
* The advisor charter (§10.5 R2) flags this as Risk 2: "If you pick top 30 by 60-day average traded value as of today, you're conditioning on stocks that are currently large/liquid. A backtest 180 days ago should use the universe as it was 180 days ago."

**Two paths in A2:**

* **Path A (snapshot, recommended for v3):** Snapshot top-30 by 60d ADTV as of the start of the 180-day backtest window (~2025-12-01 IST), persist to `data/v3_universe_top30.txt` + a sidecar `data/v3_universe_top30.json` with shape `{"universe": [...], "snapshot_date": "2025-12-01", "snapshot_method": "60d_adtv_at_window_start"}`. Document the survivorship bias in the gap-analysis output and accept it for Phase A. For Nifty 30 / Nifty 50 the index turnover is slow (3-4 changes/year), so the bias on a 180d window is bounded.
* **Path B (per-day universe, deferred):** Add `valid_from`/`valid_to` to the fixture schema and have the event loop skip a symbol's bar if `bar_ts not in [valid_from, valid_to]`. Estimated 4-6h. **Not in scope for Phase A2. Logged as a v3 follow-up.**

**Effort for Phase A2: ~1h (Path A snapshot + bias documentation).**

---

## 7. 180-day window with daily candles

**Status: SUPPORTED.**

* Yahoo Finance daily data has no per-day chunking limit (`data_handler.py:177` chunks daily by 30-day windows; for daily interval the chunking is ample).
* Volume math: 30 stocks × 180 trading days ≈ 5,400 bars. Compared to v2.1's slot #3 (232 stocks × 60 days × 5-min ≈ 1.07M bars), v3 is ~200× smaller.
* Estimated runtime per variant: a few minutes wall-clock on the 2-vCPU backtester VM (vs 14h+ for the 5-min equivalent). The full 5-variant sweep (V20-V24 per charter §A4) should complete in <1h.
* Battery `--days` arg passes through to `download_historical_for_backtest` unchanged; `--days 180` works today.

**Effort: 0h.**

---

## 8. Walk-forward train/holdout split (Bug K)

**Status: SUPPORTED. Bug K is FULLY CLOSED with regression tests.**

* `--train-window-days` and `--holdout-window-days` flags exist and are documented (`battery.py:1257-1273`).
* **Bug K (slice-after-cache-save) is FIXED in code.** The slice block at `battery.py:1439-1467` runs INSIDE the `if market_data is None:` fresh-run branch and BEFORE `_save_market_data_cache` at `:1469`. Workers reload the pre-sliced cache; the holdout flag is no longer silently dropped in the parallel path.
* **Regression tests exist.** `tests/unit/test_battery_walk_forward_slice.py` has three test classes:
  * `TestBugKSliceOrderingSource` — AST guard that asserts `slice_lineno < save_cache_lineno` inside `main()`. Future refactors that re-order these calls will fail this test.
  * `TestBugKEndToEndRoundTrip` — saves a sliced cache and reads it back, asserts byte-counts match.
  * Plus a guard that the slice loop is nested under `if market_data is None:` so it doesn't double-slice on resume.
* Resume path explicitly warns and ignores slice flags (`battery.py:1478-1487`) — the cached market_data already reflects the original slice.

**Effort: 0h. Bug K is closed. v3 will use the existing flags directly: A5 will run V22 (or whichever combined variant lands best) with `--train-window-days 120`, then re-run with `--holdout-window-days 60` (per charter §6 A5).**

---

## 9. Summary table

| Req | Description | Status | A2 effort | Notes |
|---|---|---|---:|---|
| 1 | Daily candle frame | SUPPORTED | 0h | Variant configs set `apply_dead_hour: false`, `max_losses_per_stock: 99` |
| 2 | CNC product, no 15:15 flat-out | SUPPORTED | 0h | Variant configs set `execution.product_type: DELIVERY`, `risk.allow_shorts: false` |
| 3 | Multi-day position holds | SUPPORTED | ~30min | Add `holding_days` field for readability (cosmetic) |
| 4 | **Next-day-open entry fills** | **GAP** | **~3-4h** | New `fill_mode: "next_bar_open"` config + engine change + unit test |
| 5 | CNC charges (DP/STT/etc) | SUPPORTED | ~15min | Doc clarification: DP is per-SELL, not per-day |
| 6 | 30-stock universe | PARTIAL | ~1h | Path A snapshot + bias doc; per-day universe deferred to follow-up |
| 7 | 180-day window with daily | SUPPORTED | 0h | Yahoo daily is rate-limit-friendly; 5,400 bars total |
| 8 | Walk-forward train/holdout | SUPPORTED | 0h | Bug K closed with three regression test classes |

**Total Phase A2 engineering: ~5-6h** (one focused work block, not a multi-day spread). The single largest item is Requirement 4 (next-day-open fill mode). Everything else is small (universe snapshot, holding_days field) or zero (config-only).

---

## 10. Phase A2 deliverables (what A2 will land)

In dependency order, each as a separate small commit with a regression test:

1. **A2-1: `fill_mode: "next_bar_open"` engine support.** New `BacktestConfig.fill_mode` field (default preserves v2.1 behaviour). Engine entry path looks up `df.iloc[i+1]` for the symbol when `fill_mode == "next_bar_open"`. Edge case: signal on final bar drops the signal and increments `gate_stats.no_next_bar`. Unit tests: hand-computed 3-bar synthetic with gap-up open, plus a byte-identical-legacy-v2.1 smoke. **~3-4h.**

2. **A2-2: `holding_days` field on trade records.** Cosmetic readability for swing trades. `_trade_to_dict` adds `"holding_days": (exit_time.date() - entry_time.date()).days`. Unit test: a 5-day-hold trade records `holding_days: 5`. **~30min.**

3. **A2-3: v3 universe snapshot fixture.** Compute top-30 by 60d ADTV as of 2025-12-01 (start of v3 180d backtest window). Persist to `data/v3_universe_top30.json` with `{"universe": [...], "snapshot_date": "2025-12-01", "snapshot_method": "60d_adtv_at_window_start"}`. Document survivorship bias in this gap-analysis doc's appendix. **~1h.**

4. **A2-4: variant config plumbing.** Three small `battery.py` `_bt_config` lines to read the new flags from config, plus the Phase A4 V20-V24 variant entries. Lands together with A4. **~30min, deferred to A4.**

5. **A2-5: charges.py docstring footnote** clarifying that DP charges are "per SELL order, not per day" (resolving the charter terminology). **~5min.**

A2 commits land on `main` directly (per charter — backtester is NOT frozen surface). Trader VM remains untouched.

---

## 11. Risks (non-obvious, surfaced before A2 begins)

Per charter §10.5, two risks deserve named attention before A2 code lands:

* **R1: backtester bugs that surface only at the daily timeframe.** v2.1 found 5+ backtester bugs in the 5-min path (Bug E, F, G, H, K). The daily path has been exercised much less. Likely surface area: holding_minutes / holding_days arithmetic on multi-day holds, the `losses_per_stock` reset semantics on daily bars, regime classification with no Nifty/VIX bars on daily resolution (existing B-02 known divergence becomes more visible). **Mitigation:** the A2 unit tests should explicitly cover daily-bar fixtures, not borrow from 5-min fixtures. Budget 1-2 surprise bugs.

* **R2: survivorship bias in the universe snapshot.** Path A (snapshot at window start) is bounded for Nifty-30 over 180d but non-zero. Acknowledge this in the v3 deliverable; A5 walk-forward results should be read with the bias caveat in mind. Path B (per-day universe) is the audit-correct version and is logged as a follow-up for v3.1+ if the live phase ever happens.

---

## 12. Cross-references

* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` — v3 charter v1.1, Phase A1-A5 plan.
* `docs/freeze/wind_down_criteria_2026-06-05.md` — verdict gate; this charter activates if T1+T2+T3 of v2.1 land in the wind-down branch.
* `docs/findings/findings_log_2026-05-27.md` §9 — Bug K disclosure (now closed).
* `docs/reviews/friday_review_2026-05-29.md` §4 — Bug K reframing of slot #3.
* `tests/unit/test_battery_walk_forward_slice.py` — Bug K regression suite.
* `tests/unit/test_audit_2026_05_28_misc.py:1554-1559` — DELIVERY round-trip charge identity.
* `packages/research/backtest_ensemble.py:194-198, 625-678, 866-924` — primary engine touch-points for A2-1 (fill_mode) and A2-2 (holding_days).
* `packages/research/battery.py:1238-1243, 1439-1467` — universe-file flag and Bug K-fix call site.

---

## 13. Phase A1 closure

* All 8 v3 requirements audited. ✅
* 5 SUPPORTED, 1 PARTIAL (universe), 1 GAP (next-day-open fill), 1 SUPPORTED-with-cosmetic-add (multi-day holds + `holding_days`).
* Total Phase A2 effort estimate: ~5-6h, dominated by the next-day-open fill mode change.
* Trader VM: untouched. Museum mode preserved per charter §6.1.

**Phase A1 → A2 hand-off:** A2 may start with A2-1 (fill_mode engine support) immediately. A2 ordering is preferable as listed because A2-1 is the largest item and on the critical path for A4 variant runs; A2-2/3/5 can run in parallel by an interleaved committer.

— END OF A1 DELIVERABLE —
