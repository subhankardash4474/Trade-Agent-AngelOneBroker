# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v32_maxc6`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +2.84 | +8.98 | -6.14 |
| Total Return % | +12.18 | +42.31 | -30.13 |
| Max DD % | -7.80 | -15.22 | +7.42 |
| Final equity ₹ | 112,185 | 142,310 | -30,125 |

## V27 trade statistics

- **Trades:** 180
- **Win rate:** 37.8%
- **Profit factor:** 1.36
- **Gross profit (₹):** 44,831
- **Gross loss (₹):** 32,876
- **Avg charges/trade (₹):** 50.60
- **Total charges (₹):** 9,109

## Charter §3.10 stop-criteria verdict

**A3 — PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% → **Edge exists but doesn't justify cost burn; academic-interest only.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 105
- donchian_exit: 42
- time_in_trade: 27
- end_of_window_close_out: 6

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-06 → 2024-02-06 | 61 | +5,662 |
| ADANIGREEN | 2026-04-08 → 2026-05-29 | 36 | +3,057 |
| COALINDIA | 2023-11-09 → 2024-02-09 | 61 | +2,201 |
| SILVERBEES | 2025-08-29 → 2025-10-21 | 36 | +1,813 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| ADANIPORTS | 2026-02-04 → 2026-03-04 | 19 | -585 |
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| ADANIENT | 2023-08-22 → 2023-08-31 | 7 | -605 |
| ADANIENT | 2022-04-26 → 2022-05-10 | 9 | -617 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 14:58:48 IST by `tools/v27_backtest_2026_06_01.py`.*