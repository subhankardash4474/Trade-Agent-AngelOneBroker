# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v28_entry100`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +0.13 | +8.98 | -8.85 |
| Total Return % | +0.53 | +42.31 | -41.78 |
| Max DD % | -12.07 | -15.22 | +3.15 |
| Final equity ₹ | 100,531 | 142,310 | -41,779 |

## V27 trade statistics

- **Trades:** 294
- **Win rate:** 35.4%
- **Profit factor:** 1.01
- **Gross profit (₹):** 49,485
- **Gross loss (₹):** 49,168
- **Avg charges/trade (₹):** 46.31
- **Total charges (₹):** 13,614

## Charter §3.10 stop-criteria verdict

**A1 — PF < 1.10 → **V27 has no edge at any size; abandon.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 196
- donchian_exit: 66
- time_in_trade: 26
- end_of_window_close_out: 6

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-07 → 2024-02-07 | 61 | +4,799 |
| SIEMENS | 2024-03-26 → 2024-06-27 | 61 | +3,151 |
| BAJAJ-AUTO | 2023-11-15 → 2024-02-14 | 61 | +2,492 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |
| ADANIGREEN | 2026-04-17 → 2026-05-29 | 30 | +1,687 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| ADANIENT | 2022-09-15 → 2022-10-03 | 12 | -635 |
| COALINDIA | 2024-06-03 → 2024-06-04 | 1 | -679 |
| TATASTEEL | 2025-03-19 → 2025-04-04 | 11 | -779 |
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -785 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 14:38:34 IST by `tools/v27_backtest_2026_06_01.py`.*