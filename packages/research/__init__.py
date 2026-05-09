"""POD 2: research -- backtesting, diagnostics, and ML training.

Imports allowed:  core, strategies, training
Imports FORBIDDEN: trader, ui

Entry point (target):  python -m research

Currently a SKELETON. Modules to move here in Phase 1.5:
  packages/research/backtest.py            <- backtest.py
  packages/research/backtest_ensemble.py   <- backtest_ensemble.py
  packages/research/battery.py             <- tools/overnight_backtest_battery.py
  packages/research/diagnostic.py          <- tools/profit_diagnostic.py
  packages/research/analyze_day.py         <- analyze_day.py

Cloud research pod (Phase 2+) writes outputs to S3 under:
  diagnostics/, backtests/, proposals/, trades-export/

The laptop pulls those via tools/sync_from_cloud.py.
"""
