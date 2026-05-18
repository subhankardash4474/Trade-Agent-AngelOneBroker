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


# ── Audit Issue #2 (2026-05-18): boot alerts must land at level=critical ─────
#
# Before this fix the inline ``send_alert`` calls in the boot reconcile blocks
# raised AttributeError (alert_manager doesn't exist yet) and were silently
# swallowed by a bare ``except``. Plus the call was missing ``level=`` so even
# if the AttributeError were fixed the alert would route via the default INFO
# channel instead of the critical channel an operator wakes up for. The new
# contract is: reconcile blocks BUFFER alert dicts into
# ``self._pending_boot_alerts``; ``_flush_pending_boot_alerts`` then drains
# them through ``self.alert_manager`` right after AlertManager is constructed.


def test_flush_pending_boot_alerts_routes_at_critical_level():
    """Buffered alerts are dispatched with the level the queuer chose,
    not the AlertManager default. A mismatch finding must surface as
    ``critical`` even though the buffer dict is opaque to the dispatcher."""
    from trading_agent import TradingAgent
    from unittest.mock import MagicMock

    agent = TradingAgent.__new__(TradingAgent)
    agent.alert_manager = MagicMock()
    agent._pending_boot_alerts = [
        {
            "title": "[CRITICAL] TCS broker/DB mismatch",
            "message": "db=BUY qty=100 vs broker=SELL qty=100",
            "level": "critical",
        },
    ]
    agent._flush_pending_boot_alerts()
    agent.alert_manager.send_alert.assert_called_once_with(
        title="[CRITICAL] TCS broker/DB mismatch",
        message="db=BUY qty=100 vs broker=SELL qty=100",
        level="critical",
    )
    # The buffer is drained so a second flush is a no-op (idempotent).
    assert agent._pending_boot_alerts == []
    agent._flush_pending_boot_alerts()
    assert agent.alert_manager.send_alert.call_count == 1


def test_flush_pending_boot_alerts_tolerates_missing_buffer():
    """If a code path constructs an agent without populating
    ``_pending_boot_alerts`` (legacy stubs, test harnesses), the flush
    must be a graceful no-op rather than raising AttributeError."""
    from trading_agent import TradingAgent
    from unittest.mock import MagicMock

    agent = TradingAgent.__new__(TradingAgent)
    agent.alert_manager = MagicMock()
    agent._flush_pending_boot_alerts()
    agent.alert_manager.send_alert.assert_not_called()


def test_flush_pending_boot_alerts_continues_past_individual_failures():
    """If one send_alert raises (e.g. transient SMTP outage), the next
    alert in the buffer still gets attempted. The CRITICAL log fired at
    queue time is the primary forensic signal so a dispatch failure
    must not lose the rest of the buffer."""
    from trading_agent import TradingAgent
    from unittest.mock import MagicMock

    agent = TradingAgent.__new__(TradingAgent)
    agent.alert_manager = MagicMock()
    agent.alert_manager.send_alert.side_effect = [
        RuntimeError("SMTP timeout"),
        None,
    ]
    agent._pending_boot_alerts = [
        {"title": "A1", "message": "m1", "level": "critical"},
        {"title": "A2", "message": "m2", "level": "critical"},
    ]
    agent._flush_pending_boot_alerts()
    assert agent.alert_manager.send_alert.call_count == 2


def test_init_buffers_alerts_before_alertmanager_is_constructed():
    """Structural regression guard for Audit Issue #2: the boot reconcile
    blocks in ``TradingAgent.__init__`` must NOT call
    ``self.alert_manager.send_alert(...)`` directly -- AlertManager
    doesn't exist yet at that point and the call would raise
    AttributeError. They must append to ``self._pending_boot_alerts``
    instead, and ``_flush_pending_boot_alerts`` must be invoked AFTER
    AlertManager is constructed.

    Re-introducing an inline send_alert in the reconcile block (the bug
    we just fixed) would silently swallow the alert again. This guard
    fails loudly if anyone does that."""
    import inspect
    import re
    from trading_agent import TradingAgent

    src = inspect.getsource(TradingAgent.__init__)
    # Locate the AlertManager-construction line; everything before it
    # is the "pre-AlertManager" zone where inline send_alert is unsafe.
    am_match = re.search(r"self\.alert_manager\s*=\s*AlertManager\(", src)
    assert am_match, (
        "AlertManager construction line moved or was removed from "
        "TradingAgent.__init__ -- the audit guard below can't locate "
        "the boundary between pre- and post-AlertManager init phases. "
        "Update the regex if the construction call was renamed."
    )
    pre_am_src = src[: am_match.start()]
    assert "self.alert_manager.send_alert" not in pre_am_src, (
        "Audit Issue #2 regression: TradingAgent.__init__ now contains "
        "an inline ``self.alert_manager.send_alert(...)`` call BEFORE "
        "``self.alert_manager = AlertManager(...)``. AlertManager doesn't "
        "exist yet at that point -- the call would AttributeError and be "
        "swallowed by the surrounding bare-except. Append to "
        "``self._pending_boot_alerts`` instead and let "
        "``_flush_pending_boot_alerts`` dispatch after AlertManager is "
        "constructed."
    )
    # And the dispatcher must actually be invoked post-construction.
    post_am_src = src[am_match.start():]
    assert "_flush_pending_boot_alerts" in post_am_src, (
        "Audit Issue #2 regression: ``_flush_pending_boot_alerts()`` is "
        "no longer called from TradingAgent.__init__ AFTER AlertManager "
        "is constructed. Any alerts queued during boot reconcile will "
        "stay buffered and never reach the operator."
    )
