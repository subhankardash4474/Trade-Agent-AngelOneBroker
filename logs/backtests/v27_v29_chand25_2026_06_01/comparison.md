# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v29_chand25`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | -1.46 | +8.98 | -10.44 |
| Total Return % | -5.86 | +42.31 | -48.17 |
| Max DD % | -11.33 | -15.22 | +3.89 |
| Final equity ₹ | 94,135 | 142,310 | -48,175 |

## V27 trade statistics

- **Trades:** 371
- **Win rate:** 33.2%
- **Profit factor:** 0.88
- **Gross profit (₹):** 46,243
- **Gross loss (₹):** 52,391
- **Avg charges/trade (₹):** 46.02
- **Total charges (₹):** 17,073

## Charter §3.10 stop-criteria verdict

**A1 — PF < 1.10 → **V27 has no edge at any size; abandon.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 320
- donchian_exit: 32
- time_in_trade: 11
- end_of_window_close_out: 8

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| ADANIGREEN | 2026-04-08 → 2026-05-29 | 36 | +2,617 |
| BAJAJ-AUTO | 2023-11-15 → 2024-02-14 | 61 | +2,492 |
| SILVERBEES | 2025-08-29 → 2025-10-20 | 35 | +1,812 |
| COALINDIA | 2023-09-05 → 2023-11-22 | 52 | +1,486 |
| SHRIRAMFIN | 2025-10-31 → 2026-01-29 | 61 | +1,324 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| COALINDIA | 2026-03-12 → 2026-04-10 | 18 | -557 |
| EICHERMOT | 2025-02-03 → 2025-02-11 | 6 | -561 |
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| TATASTEEL | 2025-03-19 → 2025-04-04 | 11 | -629 |
| COALINDIA | 2022-04-21 → 2022-04-27 | 4 | -795 |

---
*Generated 2026-06-01 14:43:55 IST by `tools/v27_backtest_2026_06_01.py`.*