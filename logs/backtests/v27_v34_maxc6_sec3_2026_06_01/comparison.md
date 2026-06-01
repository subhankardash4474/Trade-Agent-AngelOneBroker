# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v34_maxc6_sec3`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +1.93 | +8.98 | -7.05 |
| Total Return % | +8.15 | +42.31 | -34.16 |
| Max DD % | -7.80 | -15.22 | +7.42 |
| Final equity ₹ | 108,151 | 142,310 | -34,159 |

## V27 trade statistics

- **Trades:** 183
- **Win rate:** 36.6%
- **Profit factor:** 1.24
- **Gross profit (₹):** 41,339
- **Gross loss (₹):** 33,412
- **Avg charges/trade (₹):** 50.03
- **Total charges (₹):** 9,156

## Charter §3.10 stop-criteria verdict

**A3 — PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% → **Edge exists but doesn't justify cost burn; academic-interest only.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 108
- donchian_exit: 43
- time_in_trade: 26
- end_of_window_close_out: 6

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-06 → 2024-02-06 | 61 | +5,662 |
| ADANIGREEN | 2026-04-08 → 2026-05-29 | 36 | +2,617 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |
| SILVERBEES | 2025-08-29 → 2025-10-21 | 36 | +1,720 |
| GOLDBEES | 2025-01-31 → 2025-05-06 | 61 | +1,298 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| ADANIPORTS | 2026-02-04 → 2026-03-04 | 19 | -585 |
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| ADANIENT | 2023-08-22 → 2023-08-31 | 7 | -605 |
| ADANIENT | 2022-04-26 → 2022-05-10 | 9 | -617 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 15:15:05 IST by `tools/v27_backtest_2026_06_01.py`.*