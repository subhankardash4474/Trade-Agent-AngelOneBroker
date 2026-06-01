# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v31_combined`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | -0.32 | +8.98 | -9.30 |
| Total Return % | -1.32 | +42.31 | -43.63 |
| Max DD % | -11.07 | -15.22 | +4.15 |
| Final equity ₹ | 98,678 | 142,310 | -43,632 |

## V27 trade statistics

- **Trades:** 265
- **Win rate:** 33.2%
- **Profit factor:** 0.96
- **Gross profit (₹):** 41,074
- **Gross loss (₹):** 42,610
- **Avg charges/trade (₹):** 48.49
- **Total charges (₹):** 12,849

## Charter §3.10 stop-criteria verdict

**A1 — PF < 1.10 → **V27 has no edge at any size; abandon.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 232
- donchian_exit: 19
- time_in_trade: 8
- end_of_window_close_out: 6

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| BAJAJ-AUTO | 2023-11-15 → 2024-02-14 | 61 | +2,492 |
| SILVERBEES | 2025-08-29 → 2025-10-20 | 35 | +1,882 |
| ADANIGREEN | 2026-04-17 → 2026-05-29 | 30 | +1,687 |
| SHRIRAMFIN | 2025-10-31 → 2026-01-29 | 61 | +1,595 |
| COALINDIA | 2023-09-05 → 2023-11-22 | 52 | +1,561 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| EICHERMOT | 2025-02-03 → 2025-02-11 | 6 | -561 |
| COALINDIA | 2026-03-12 → 2026-04-10 | 18 | -595 |
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -785 |
| COALINDIA | 2022-04-21 → 2022-04-27 | 4 | -795 |
| TATASTEEL | 2025-03-19 → 2025-04-04 | 11 | -817 |

---
*Generated 2026-06-01 14:51:52 IST by `tools/v27_backtest_2026_06_01.py`.*