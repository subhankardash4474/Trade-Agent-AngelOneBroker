"""Packages root.

Each immediate subdirectory of this package corresponds to one deployable
unit (a "pod") or one shared library, per the cloud architecture decided
2026-05-09. See `docs/cloud_pod_architecture.md`.

  Shared libraries (imported by pods, never deployed alone):
    core        - DB, charges, portfolio, secrets, alerts, brokers, ensemble, ...
    strategies  - all strategy implementations
    brokers     - AngelOne / paper broker abstraction (currently top-level for
                  backwards compatibility; will move under core in a later phase)
    monitoring  - alert manager (everything else moved out)
    training    - model training pipelines

  Deployable pods (each gets its own Dockerfile in deploy/docker/):
    trader      - POD 1: live trading. Imports core + strategies.
    research    - POD 2: backtesting + diagnostics + training. Imports core + strategies + training.
    ui          - POD 3: read-only dashboard. Imports core only.

The trader pod must NEVER import research or ui modules. The research pod must
NEVER import trader or ui modules. These boundaries are enforced by
tests/unit/test_pod_boundaries.py once Phase 1 has executed.
"""
