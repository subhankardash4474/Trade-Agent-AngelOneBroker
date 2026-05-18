"""Regression #3 (2026-05-18): tighten ``reconcile_positions_with_broker``.

Background. The original P2 restart-cluster reconciler classified every
non-zero ``broker.netqty`` as ``status: "ok"`` without checking that the
broker's side and quantity actually matched the DB position. Two real
failure modes silently slipped through:

  1. Half-fill DB never observed:
     broker netqty 50, DB quantity 100. Without validation, the agent
     happily uses DB qty=100 in subsequent sizing / exit calcs while
     the broker only has 50 to flatten -- exits leak quantity.

  2. Flipped side:
     broker netqty -100 (SHORT), DB side=BUY (LONG). Without validation,
     the agent thinks it's long; an exit-by-signal would BUY again,
     doubling the broker's short position instead of flattening it.

The fix surfaces these as ``status: "mismatch"`` with a CRITICAL log and
detailed reason fields the caller can use to block the symbol for the
session.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from core.execution import ExecutionEngine


def _engine_with_positions(rows):
    """Build a minimal ExecutionEngine wired to a fake broker positionBook."""
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.mode = "live"
    eng._api = MagicMock()
    eng._api.position.return_value = {"data": rows}
    eng._pending_orders = {}
    eng._order_log = []
    return eng


def test_matching_side_and_qty_reports_ok():
    eng = _engine_with_positions([
        {"tradingsymbol": "RELIANCE", "netqty": "100"},
    ])
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    assert report["RELIANCE"]["status"] == "ok"
    assert report["RELIANCE"]["broker_netqty"] == 100


def test_matching_short_side_reports_ok():
    eng = _engine_with_positions([
        {"tradingsymbol": "RELIANCE", "netqty": "-100"},
    ])
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "SELL", "quantity": 100},
    })
    assert report["RELIANCE"]["status"] == "ok"
    assert report["RELIANCE"]["broker_netqty"] == -100


def test_broker_flat_db_open_reports_orphan():
    eng = _engine_with_positions([])
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    assert report["RELIANCE"]["status"] == "orphan"


def test_flipped_side_reports_mismatch():
    """DB says LONG 100, broker says SHORT 100. The old reconciler
    rubber-stamped this as 'ok' (any non-zero qty was OK). Fix surfaces it."""
    eng = _engine_with_positions([
        {"tradingsymbol": "RELIANCE", "netqty": "-100"},
    ])
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    entry = report["RELIANCE"]
    assert entry["status"] == "mismatch"
    assert entry["db_side"] == "BUY"
    assert entry["broker_side"] == "SELL"
    assert any("side" in r for r in entry["reasons"])


def test_qty_mismatch_reports_mismatch():
    """DB says 100, broker says 50. Half-fill DB never observed."""
    eng = _engine_with_positions([
        {"tradingsymbol": "RELIANCE", "netqty": "50"},
    ])
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    entry = report["RELIANCE"]
    assert entry["status"] == "mismatch"
    assert entry["db_quantity"] == 100
    assert entry["broker_quantity"] == 50
    assert any("qty" in r for r in entry["reasons"])


def test_both_side_and_qty_mismatch():
    """Worst case: flipped side AND different qty. Both reasons reported."""
    eng = _engine_with_positions([
        {"tradingsymbol": "TCS", "netqty": "-50"},
    ])
    report = eng.reconcile_positions_with_broker({
        "TCS": {"side": "BUY", "quantity": 100},
    })
    entry = report["TCS"]
    assert entry["status"] == "mismatch"
    assert len(entry["reasons"]) == 2


def test_paper_mode_returns_skipped():
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.mode = "paper"
    eng._api = None
    eng._pending_orders = {}
    eng._order_log = []
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    assert report["RELIANCE"]["status"] == "skipped"


def test_api_error_returns_skipped():
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.mode = "live"
    eng._api = MagicMock()
    eng._api.position.side_effect = RuntimeError("AngelOne 500")
    eng._pending_orders = {}
    eng._order_log = []
    report = eng.reconcile_positions_with_broker({
        "RELIANCE": {"side": "BUY", "quantity": 100},
    })
    assert report["RELIANCE"]["status"] == "skipped"


def test_mismatch_logs_critical():
    """The CRITICAL log is the primary operator-facing signal even before
    the caller acts on the report. Pin it by attaching a transient loguru
    sink (the codebase uses loguru, which bypasses pytest's caplog and
    capsys -- a custom sink is the supported pattern)."""
    from loguru import logger as loguru_logger

    captured = []
    handler_id = loguru_logger.add(
        lambda msg: captured.append(str(msg)),
        level="CRITICAL",
    )
    try:
        eng = _engine_with_positions([
            {"tradingsymbol": "TCS", "netqty": "-100"},
        ])
        eng.reconcile_positions_with_broker({
            "TCS": {"side": "BUY", "quantity": 100},
        })
    finally:
        loguru_logger.remove(handler_id)

    combined = "".join(captured)
    assert "MISMATCH" in combined
    assert "TCS" in combined
