"""Canonical launcher for the backtest battery.

Why this wrapper exists
-----------------------
After the Phase 1 packages/ restructure, the modules under packages/ are
NOT auto-discoverable on the default sys.path. As a result:

    python -m research.battery         # FAILS: ModuleNotFoundError: research
    python packages/research/battery.py # WORKS but cosmetically ugly

This file is the friendly-named, canonical invocation:

    python tools/run_battery.py --days 90 --workers 4 ...

It bootstraps `packages/` onto sys.path and delegates to battery.main(),
preserving the full argparse surface (--resume, --workers, --run-id,
--train-window-days, --holdout-window-days, etc.).

For one-off direct-path invocations, the underlying script still works
unchanged -- this is purely an ergonomics layer, not a replacement.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap: prepend packages/ so `research`, `core`, `strategies` etc.
# resolve as top-level packages. Mirrors the prelude inside battery.py
# itself so behavior is identical whether invoked via this wrapper or
# the direct path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from research.battery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
