"""V4 reusable signal generators + sizing/allocation utilities (charter §1,
with pod-boundary correction filed in
`docs/reviews/strategy_charter_v4_operator_responses_2026-06-01.md`).

Lives under `packages/core/` (not `packages/research/` as the charter
originally listed) because `packages/strategies/` modules need to call
these primitives at runtime, and the pod-boundary rule
(`tests/unit/test_pod_boundaries.py`) only permits
`strategies -> core` (not `strategies -> research`). The asymmetry is
intentional: `research` is upstream of `strategies` at audit time.

Modules:
    donchian: Donchian channel breakout + exit signals (Turtle/Dunn-Capital).
    volatility_sizer: vol-target position sizing (0.5% per trade).
    risk_parity: inverse-volatility capital allocator across active positions.

These modules are deliberately framework-agnostic: each function takes a
DataFrame (or a dict of DataFrames) and returns numbers. The strategy
modules and the orchestrator (`backtest_ensemble.py` / `trading_agent.py`)
compose them.
"""
