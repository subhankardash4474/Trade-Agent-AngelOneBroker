# Phase 15 hit-and-trial sweep summary

- Window: 2021-06-02 → 2026-06-01
- Capital: ₹100,000
- Engine: max_concurrent=6, sector_cap=None
- Excluded from signals: NIFTYBEES (passive core reserve)

Sorted by CAGR (descending):

| variant | module | overrides | CAGR % | PF | MaxDD % | trades | WR % |
|---|---|---|---:|---:|---:|---:|---:|
| V40_decile15 | dual_momentum_relstrength_v1 | `{"exit_tolerance_pct": 0.05, "top_decile_pct": 0.15}` | +9.95 | 2.53 | -8.22 | 107 | 38.3 |
| V40_decile25 | dual_momentum_relstrength_v1 | `{"exit_tolerance_pct": 0.05, "top_decile_pct": 0.25}` | +6.58 | 2.48 | -7.56 | 95 | 46.3 |
| V38_n20_sma60 | weekly_breakout_v1 | `{"weekly_entry_n": 20, "weekly_exit_m": 10, "weekly_sma_regime": 60}` | +5.64 | 2.58 | -8.38 | 71 | 42.3 |
| V38_n25_m10 | weekly_breakout_v1 | `{"weekly_entry_n": 25, "weekly_exit_m": 10}` | +5.31 | 2.18 | -8.36 | 79 | 41.8 |
| V38_n30_m10 | weekly_breakout_v1 | `{"weekly_entry_n": 30, "weekly_exit_m": 10}` | +4.76 | 2.35 | -4.72 | 71 | 45.1 |
| V38_n20_sma20 | weekly_breakout_v1 | `{"weekly_entry_n": 20, "weekly_exit_m": 10, "weekly_sma_regime": 20}` | +4.75 | 2.02 | -8.35 | 81 | 39.5 |
| V40_decile30 | dual_momentum_relstrength_v1 | `{"exit_tolerance_pct": 0.05, "top_decile_pct": 0.3}` | +4.65 | 2.03 | -6.41 | 97 | 43.3 |
| V38_n35_m10 | weekly_breakout_v1 | `{"weekly_entry_n": 35, "weekly_exit_m": 10}` | +4.40 | 2.20 | -4.82 | 71 | 43.7 |
| V38_n40_m10 | weekly_breakout_v1 | `{"weekly_entry_n": 40, "weekly_exit_m": 10}` | +4.23 | 2.08 | -4.83 | 73 | 42.5 |
