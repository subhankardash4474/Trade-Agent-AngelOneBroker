# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v30_maxc8`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +1.87 | +8.98 | -7.11 |
| Total Return % | +7.90 | +42.31 | -34.41 |
| Max DD % | -8.55 | -15.22 | +6.67 |
| Final equity ₹ | 107,904 | 142,310 | -34,407 |

## V27 trade statistics

- **Trades:** 239
- **Win rate:** 36.8%
- **Profit factor:** 1.19
- **Gross profit (₹):** 48,405
- **Gross loss (₹):** 40,790
- **Avg charges/trade (₹):** 48.36
- **Total charges (₹):** 11,559

## Charter §3.10 stop-criteria verdict

**A2 — PF ∈ [1.10, 1.20) → **Borderline; defer to V28 with ONE param change.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 152
- donchian_exit: 50
- time_in_trade: 29
- end_of_window_close_out: 8

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-06 → 2024-02-06 | 61 | +5,662 |
| ADANIGREEN | 2026-04-08 → 2026-05-29 | 36 | +2,617 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |
| COALINDIA | 2023-09-05 → 2023-12-06 | 61 | +1,789 |
| SILVERBEES | 2025-08-29 → 2025-10-21 | 36 | +1,782 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| SHRIRAMFIN | 2026-02-09 → 2026-03-19 | 27 | -611 |
| ADANIENT | 2022-04-26 → 2022-05-10 | 9 | -617 |
| WIPRO | 2025-12-22 → 2026-01-19 | 19 | -666 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 14:47:31 IST by `tools/v27_backtest_2026_06_01.py`.*