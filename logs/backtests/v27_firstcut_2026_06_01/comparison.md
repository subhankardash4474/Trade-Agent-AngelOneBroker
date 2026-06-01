# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_firstcut`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +1.25 | +8.98 | -7.73 |
| Total Return % | +5.23 | +42.31 | -37.08 |
| Max DD % | -10.24 | -15.22 | +4.98 |
| Final equity ₹ | 105,229 | 142,310 | -37,081 |

## V27 trade statistics

- **Trades:** 314
- **Win rate:** 36.9%
- **Profit factor:** 1.1
- **Gross profit (₹):** 53,707
- **Gross loss (₹):** 48,764
- **Avg charges/trade (₹):** 46.27
- **Total charges (₹):** 14,530

## Charter §3.10 stop-criteria verdict

**A2 — PF ∈ [1.10, 1.20) → **Borderline; defer to V28 with ONE param change.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 201
- donchian_exit: 74
- time_in_trade: 31
- end_of_window_close_out: 8

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-06 → 2024-02-06 | 61 | +5,407 |
| ADANIGREEN | 2026-04-08 → 2026-05-29 | 36 | +2,617 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |
| SILVERBEES | 2025-08-29 → 2025-10-21 | 36 | +1,720 |
| COALINDIA | 2023-09-05 → 2023-12-06 | 61 | +1,598 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| WIPRO | 2025-12-22 → 2026-01-19 | 19 | -610 |
| ADANIENT | 2022-04-26 → 2022-05-10 | 9 | -617 |
| GAIL | 2024-03-05 → 2024-03-13 | 5 | -629 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 13:03:09 IST by `tools/v27_backtest_2026_06_01.py`.*