# V35 ↔ V32 sanity check

> V35 = Donchian-55/20 through the NEW engine (`swing_backtester`)
> V32 = Donchian-55/20 through the V27 standalone tool (already published)
> Both with max_concurrent_positions=6, identical params.
> Tolerance: CAGR ±0.10%, PF ±0.05, MaxDD ±0.50%

| Metric | V35 (new engine) | V32 (published) | Δ | Tolerance | Pass |
|---|---:|---:|---:|---:|:---:|
| cagr_pct | 2.84 | 2.84 | +0.000 | ±0.1 | ✓ |
| profit_factor | 1.36 | 1.36 | +0.000 | ±0.05 | ✓ |
| max_dd_pct | -7.8 | -7.8 | +0.000 | ±0.5 | ✓ |

**Verdict: PASS — engine extraction is correct, proceed with V36–V40.**
