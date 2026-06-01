# V27 first-cut backtest comparison

> **Variant:** `cross_asset_trend_v27_v33_maxc4`  
> **Charter:** [docs/reviews/strategy_charter_v4_2026-06-01.md](../../../docs/reviews/strategy_charter_v4_2026-06-01.md)  
> **Window:** 2022-04-21 → 2026-05-29 (4.1 years)  
> **Initial capital:** ₹100,000  
> **Cost model:** `CashCNCCharges:angelone:2026-06-01`  

## Headline

| Metric | V27 | NIFTYBEES (buy-and-hold) | Δ |
|---|---:|---:|---:|
| CAGR % | +2.32 | +8.98 | -6.66 |
| Total Return % | +9.87 | +42.31 | -32.44 |
| Max DD % | -7.84 | -15.22 | +7.38 |
| Final equity ₹ | 109,866 | 142,310 | -32,444 |

## V27 trade statistics

- **Trades:** 130
- **Win rate:** 36.9%
- **Profit factor:** 1.36
- **Gross profit (₹):** 36,343
- **Gross loss (₹):** 26,626
- **Avg charges/trade (₹):** 53.48
- **Total charges (₹):** 6,952

## Charter §3.10 stop-criteria verdict

**A3 — PF ≥ 1.20 BUT CAGR < NIFTYBEES + 2% → **Edge exists but doesn't justify cost burn; academic-interest only.****

## Caveats (charter §3 deferred items for V28+)

- FIRST-CUT — chandelier stops recompute daily (correct per §3.4)
- Sector cap (§3.6: max 3 per sector) NOT enforced; max_concurrent=12 only
- NIFTYBEES quarterly-rebalance benchmark not yet wired; buy-and-hold only
- TATAMOTORS.NS + LTIM.NS dropped due to yfinance corp-action data gaps
- Trade fills at TODAY'S CLOSE (charter implies next-bar-open; deferred)

## Exit-reason breakdown

- chandelier_stop: 74
- donchian_exit: 33
- time_in_trade: 19
- end_of_window_close_out: 4

## Top 5 winners + bottom 5 losers

### Top 5 winners

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| IOC | 2023-11-06 → 2024-02-06 | 61 | +6,171 |
| COALINDIA | 2023-11-09 → 2024-02-09 | 61 | +2,463 |
| SILVERBEES | 2025-08-29 → 2025-10-21 | 36 | +2,000 |
| M&M | 2022-04-22 → 2022-07-19 | 61 | +1,802 |
| GOLDBEES | 2025-01-31 → 2025-05-06 | 61 | +1,361 |

### Bottom 5 losers

| Symbol | Entry → Exit | Bars | PnL net ₹ |
|---|---|---:|---:|
| HCLTECH | 2025-01-10 → 2025-01-14 | 2 | -594 |
| COALINDIA | 2026-03-12 → 2026-04-16 | 21 | -615 |
| POWERGRID | 2022-05-09 → 2022-05-23 | 10 | -624 |
| HCLTECH | 2022-11-24 → 2022-12-09 | 11 | -666 |
| COALINDIA | 2022-04-21 → 2022-04-29 | 6 | -972 |

---
*Generated 2026-06-01 15:01:20 IST by `tools/v27_backtest_2026_06_01.py`.*