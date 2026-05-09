"""
Project-root conftest.py -- bootstraps sys.path for the new packages/ layout.

Phase 1 (2026-05-09) moved core/, strategies/, brokers/, monitoring/, training/
under packages/. To preserve unchanged import paths (`from core.X import Y`),
we prepend packages/ to sys.path at pytest collection time.

A matching prelude is added to every entry-point script (run_daemon.py,
stop_daemon.py, etc.) by tools/_phase1_move.py at execution.

Phase 2 will replace this hack with `pip install -e .` reading from
pyproject.toml's package-dir config. For laptop-mode, this is sufficient
and zero-config from the user's perspective.
"""
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent / "packages"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
