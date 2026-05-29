# Phase B prep — Hourly Blackout Candidates

_Generated 2026-05-11 09:13:48 from `data\trading_agent.db` (89 closed trades across 7 active hours)_

## Purpose

Identify hours-of-day where new entries have **structurally negative edge** and should be blacklisted in Phase B (alongside quarter-Kelly sizing). Two gates must be passed for an hour to make the candidate list:

1. **PF gate**: profit factor < `0.8` (i.e., gross losses > 0.8 × gross wins -- structural bleed)
2. **Sample gate**: at least `10` trades in that hour (kills noise-driven false positives)

Both gates required. An hour with PF 0.4 from 2 trades is **not** a blackout candidate; statistically it's a coin flip dressed up as data.

## Per-hour breakdown

| Hour (IST) | Trades | WR% | Gross Win | Gross Loss | PF | Net PnL | Avg/trade | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 09:00-09:59 | 22 | 64% | Rs 749 | Rs 860 | 0.87 | Rs -111 | Rs -5.0 | ok |
| 10:00-10:59 | 11 | 82% | Rs 449 | Rs 73 | 6.12 | Rs +376 | Rs +34.1 | ok |
| 11:00-11:59 | 16 | 69% | Rs 240 | Rs 167 | 1.44 | Rs +73 | Rs +4.6 | ok |
| 12:00-12:59 | 17 | 35% | Rs 319 | Rs 354 | 0.90 | Rs -35 | Rs -2.1 | ok |
| 13:00-13:59 | 14 | 71% | Rs 241 | Rs 220 | 1.10 | Rs +21 | Rs +1.5 | ok |
| 14:00-14:59 | 7 | 43% | Rs 30 | Rs 190 | 0.16 | Rs -160 | Rs -22.9 | low-PF n<gate |
| 15:00-15:59 | 2 | 0% | Rs 0 | Rs 28 | 0.00 | Rs -28 | Rs -13.9 | low-PF n<gate |

## Candidates (action items for Phase B)

_No hour meets BOTH gates. Either the edge is uniform across the trading day, or we have insufficient samples to call any hour structurally bad. Re-run after the trade table grows by another ~30 trades (typically one good week of activity)._

## Inconclusive: low PF, insufficient samples

- 14:00-14:59 IST: PF 0.16, only 7 trades (need >= 10). Suggests potential bleed but cannot conclude. Watch list for re-analysis next week.
- 15:00-15:59 IST: PF 0.00, only 2 trades (need >= 10). Suggests potential bleed but cannot conclude. Watch list for re-analysis next week.

## What-if: blacklist all candidate hours retroactively

_No candidates -- no what-if to compute._

## Next steps

1. **Wait for Phase A validation to PASS** (5-day rolling PF >= 1.5). Do NOT deploy blackouts on an unvalidated strategy mix.
2. Once Phase A passes: re-run this script. If candidates have stabilized (same hours flagged with more data), proceed.
3. Walk-forward backtest with the proposed blackouts enabled. Use `tools/run_battery.py --train-window-days 60 --holdout-window-days 30`.
4. If holdout PF improves: add the blackouts to `config.yaml` under `risk.entry_blackout_hours` (key TBD) + ship behind a feature flag.
5. Re-validate live for 5 trading days (Phase A re-check).

_To regenerate: `python tools/analyze_hourly_blackouts.py`_
