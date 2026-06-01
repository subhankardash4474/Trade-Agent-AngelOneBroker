# Multi-strategy swing backtest — V35–V40 comparison

> **Engine:** `packages/research/swing_backtester.py` (Engine B)  
> **Runner:** `tools/multi_swing_backtest_2026_06_01.py`  
> **Window:** 2021-06-02 → 2026-06-01  
> **Capital:** ₹100,000  
> **Universe:** V4 cross-asset (75 instruments, see `data/v4_universe_swing_cash.txt`)  
> **Cost model:** AngelOne CNC DELIVERY (`packages/core/charges.py`)  
> **Benchmark:** NIFTYBEES buy-and-hold: CAGR +8.98%, MaxDD -15.22%

## Variant comparison

| Variant | CAGR % | vs Bench | PF | MaxDD % | Trades | WinRate | Avg ₹/trade | §3.10 |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| V38_weekly_breakout | +5.45 | -3.53 | 2.22 | -8.34 | 79 | 43.0% | ₹53 | A3 |

## Charter §3.10 verdict legend

- **A1** — PF < 1.10. No edge at any size; abandon.
- **A2** — PF ∈ [1.10, 1.20). Borderline; defer to retune.
- **A3** — PF ≥ 1.20 BUT CAGR < benchmark + 2%. Informational only.
- **A4** — PF ≥ 1.20 AND CAGR ≥ benchmark + 2% AND |MaxDD| ≤ 25%. **PASS** → paper-mode candidate.
- **A5** — MaxDD > 25%. Stop; incompatible with capital base.

## Exit-reason breakdown by variant

| Variant | top exit reasons (count) |
|---|---|
| V38 | stop_loss=40, time_in_trade=29, end_of_window_close_out=6, weekly_donchian_exit=4 |

---
*Generated 2026-06-01 16:22:21 IST.*