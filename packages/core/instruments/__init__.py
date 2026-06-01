"""V4 instrument loaders (charter §1, with pod-boundary correction
filed in `docs/reviews/strategy_charter_v4_operator_responses_2026-06-01.md`).

Lives under `packages/core/` (not `packages/research/` as the charter
originally listed) because `packages/strategies/` modules need to import
universe loaders at runtime, and the pod-boundary rule
(`tests/unit/test_pod_boundaries.py`) only permits
`strategies -> core` (not `strategies -> research`).

Modules:
    etf_universe: loaders for `data/v4_universe_*.txt` files (Mode A).
    fno_universe: F&O instrument metadata (Mode B/C; not yet built — gated
        on charter §4.2 backtester validation).
"""
