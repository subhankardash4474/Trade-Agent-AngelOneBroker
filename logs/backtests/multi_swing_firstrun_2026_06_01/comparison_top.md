# Multi-strategy swing backtest — V35–V40 comparison

> **Engine:** `packages/research/swing_backtester.py` (Engine B)  
> **Runner:** `tools/multi_swing_backtest_2026_06_01.py`  
> **Window:** 2021-06-02 → 2026-06-01  
> **Capital:** ₹100,000  
> **Universe:** V4 cross-asset (75 instruments, see `data/v4_universe_swing_cash.txt`)  
> **Cost model:** AngelOne CNC DELIVERY (`packages/core/charges.py`)  
> **Benchmark:** NIFTYBEES buy-and-hold: CAGR +12.72%, MaxDD -15.23%

## Variant comparison

| Variant | CAGR % | vs Bench | PF | MaxDD % | Trades | WinRate | Avg ₹/trade | §3.10 |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| V35_donchian55_20 | +2.84 | -9.88 | 1.36 | -7.80 | 180 | 37.8% | ₹51 | A3 |
| V36_mean_reversion_swing | -0.25 | -12.97 | 0.65 | -2.64 | 13 | 38.5% | ₹55 | A1 |
| V37_pullback_to_sma50 | -1.91 | -14.63 | 0.85 | -11.08 | 424 | 26.4% | ₹48 | A1 |
| V38_weekly_breakout | +4.75 | -7.97 | 2.02 | -8.35 | 81 | 39.5% | ₹52 | A3 |
| V39_macd_swing | -2.12 | -14.84 | 0.85 | -17.35 | 469 | 31.8% | ₹50 | A1 |
| V40_dual_momentum_relstrength | +3.83 | -8.89 | 1.30 | -8.17 | 254 | 53.9% | ₹61 | A3 |

## Charter §3.10 verdict legend

- **A1** — PF < 1.10. No edge at any size; abandon.
- **A2** — PF ∈ [1.10, 1.20). Borderline; defer to retune.
- **A3** — PF ≥ 1.20 BUT CAGR < benchmark + 2%. Informational only.
- **A4** — PF ≥ 1.20 AND CAGR ≥ benchmark + 2% AND |MaxDD| ≤ 25%. **PASS** → paper-mode candidate.
- **A5** — MaxDD > 25%. Stop; incompatible with capital base.

## Exit-reason breakdown by variant

| Variant | top exit reasons (count) |
|---|---|
| V35 | chandelier_stop=105, donchian_exit=42, time_in_trade=27, end_of_window_close_out=6 |
| V36 | stop_loss=5, time_in_trade=4, rsi_overbought=4 |
| V37 | sma50_breach=264, time_in_trade=69, stop_loss=43, profit_take=42, end_of_window_close_out=6 |
| V38 | stop_loss=38, time_in_trade=27, weekly_donchian_exit=10, end_of_window_close_out=6 |
| V39 | macd_bearish_cross=416, stop_loss=40, time_in_trade=7, end_of_window_close_out=6 |
| V40 | month_end_rebalance=174, stop_loss=77, end_of_window_close_out=3 |

---
*Generated 2026-06-01 15:58:03 IST.*