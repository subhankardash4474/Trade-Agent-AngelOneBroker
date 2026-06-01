"""Canonical launcher for the Engine B swing battery (cloud / VM path).

Mirrors `tools/run_battery.py`: a thin wrapper that bootstraps
`packages/` onto sys.path and delegates to the heavy module's `main()`.

Why this exists
---------------
The backtester VM's queue scheduler (`tools/run_battery_queue.py`)
launches battery jobs via a docker run that invokes a Python module
under `/app/tools/`. Existing pattern for Engine A is
`python tools/run_battery.py ...`; this is the symmetric entry
for Engine B (multi-strategy swing_backtester / V35-V40).

Usage (called by run_battery_queue.py with engine=swing job entries):
    python tools/run_swing_battery.py --variants V38 \\
        --strategy-params-file data/sweep_params/v38_n25_m12_2026-06-01.json \\
        --start 2025-01-01 --end 2026-05-30 \\
        --run-id swing_walkforward_v38_oos_<utc_ts>

Direct invocation (laptop test runs):
    python tools/run_swing_battery.py --variants V35 V38 --days 180

For the higher-level multi-variant orchestration with prettier CLI,
`tools/multi_swing_backtest_2026_06_01.py` is the laptop-friendly
sibling — keep that for ad-hoc local runs. This script is the
cloud-deployable entry that the queue scheduler can invoke directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from research.swing_battery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
