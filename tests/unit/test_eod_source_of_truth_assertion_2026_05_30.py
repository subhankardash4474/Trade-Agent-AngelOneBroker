"""Unit tests for the EOD source-of-truth assertion.

Per the 2026-05-30 brutal review Session 2 §5 / Session 3 §3, four
sources disagreed on "lifetime cumulative realised P&L":

  * data/self_sufficiency.json  (ledger; was 16d stale at 0.0)
  * checkpoint JSON              (DB-summed; -₹1,212.26)
  * health.json                  (cash only)
  * eod report .md               (per-strategy, not lifetime)

The wind-down decision hinges on which number is canonical. This batch
adds a one-shot reconciliation in ``_maybe_send_eod_summary`` that
compares the ledger against the DB and surfaces drift in the EOD
email + a CRITICAL log line.

Tests pin:

  1. Method exists and is the single integration point.
  2. Tracker disabled -> assertion returns None (no spurious noise on
     dev configs that opt out).
  3. Ledger == DB within tolerance -> ("OK", ledger, db, diff~0).
  4. Ledger != DB beyond ±₹0.01 -> ("DRIFT", ...) AND a CRITICAL log
     emits with both numbers + the suggested rebuild action.
  5. DB unreachable -> ("ERROR", ...) AND a WARNING log (not CRITICAL).
  6. EOD email body includes a "Source-of-truth:" line whenever the
     tracker is enabled.
  7. Drift case in the email body explicitly says "DRIFT" so an
     operator skimming the inbox sees the disagreement immediately.

Cross-references:
  * ``trading_agent._eod_source_of_truth_assertion`` (helper).
  * ``trading_agent._maybe_send_eod_summary`` (caller / email injection).
  * ``packages/core/self_sufficiency.SelfSufficiencyTracker``.
  * ``packages/core/database.Database.load_trades``.
  * ``docs/reviews/brutal_review_2026-05-30.md`` Session 2 §5.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from loguru import logger as loguru_logger

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_agent_stub(
    *,
    ss_enabled: bool,
    ledger_cum: float,
    deployed_iso: str,
    db_pnls: list,
    db_raises: bool = False,
):
    """Construct a minimal duck-typed object that has just enough of
    the TradingAgent surface for ``_eod_source_of_truth_assertion`` to
    run. We avoid full-agent construction (which loads config, opens
    the broker, starts the websocket, etc.) by binding the unbound
    method onto a SimpleNamespace.
    """
    from trading_agent import TradingAgent

    self_sufficiency = SimpleNamespace(
        enabled=ss_enabled,
        _ledger={
            "cumulative_realised_inr": ledger_cum,
            "deployed_on": deployed_iso,
        },
    )

    db = MagicMock()
    if db_raises:
        db.load_trades.side_effect = RuntimeError("db down")
    else:
        db.load_trades.return_value = (
            pd.DataFrame({"pnl": db_pnls}) if db_pnls else pd.DataFrame()
        )

    agent = SimpleNamespace(
        self_sufficiency=self_sufficiency,
        database=db,
    )
    # Bind the unbound method.
    agent._eod_source_of_truth_assertion = (
        TradingAgent._eod_source_of_truth_assertion.__get__(agent)
    )
    return agent


@pytest.fixture
def loguru_records():
    """Capture loguru log records to an in-memory list. The codebase
    uses loguru (not stdlib logging) so pytest's ``caplog`` fixture
    does not see anything; we install a temporary sink that appends
    records to a list and yield it."""
    captured: list = []

    def _sink(message):
        captured.append(message.record)

    sink_id = loguru_logger.add(_sink, level="DEBUG")
    try:
        yield captured
    finally:
        loguru_logger.remove(sink_id)


# ── §5/§3 integration point: helper exists ──────────────────────────────


def test_eod_source_of_truth_helper_exists_on_trading_agent():
    """The integration point MUST live on TradingAgent. If the method
    name is renamed without updating ``_maybe_send_eod_summary``, the
    EOD email silently loses the reconciliation banner — exactly the
    failure mode this assertion is supposed to PREVENT."""
    from trading_agent import TradingAgent

    assert hasattr(TradingAgent, "_eod_source_of_truth_assertion"), (
        "TradingAgent must expose `_eod_source_of_truth_assertion`. "
        "If this fires after a rename, also update the call site in "
        "`_maybe_send_eod_summary` and the email-body injection block."
    )


# ── §5/§3 case: tracker disabled ──────────────────────────────────────


def test_assertion_returns_none_when_tracker_disabled():
    """When the operator turns off ``risk.self_sufficiency.enabled``
    (some dev configs do), the assertion MUST be skipped, not lie about
    a perfect match. Returning None is the contract that the email
    body uses to drop the source-of-truth section entirely."""
    agent = _make_agent_stub(
        ss_enabled=False,
        ledger_cum=0.0,
        deployed_iso="",
        db_pnls=[1.0, -2.0],
    )
    assert agent._eod_source_of_truth_assertion() is None


# ── §5/§3 case: ledger and DB agree ─────────────────────────────────────


def test_assertion_returns_ok_when_ledger_matches_db_within_tolerance():
    """Happy path: ledger and DB agree to within ±₹0.01. Status is
    "OK", numbers come back as supplied, diff rounds to ~0."""
    agent = _make_agent_stub(
        ss_enabled=True,
        ledger_cum=-1212.26,
        deployed_iso="2025-01-01",
        db_pnls=[100.0, -200.0, -1112.26, 0.0],
    )
    result = agent._eod_source_of_truth_assertion()
    assert result is not None
    status, ledger_cum, db_cum, diff = result
    assert status == "OK"
    assert ledger_cum == pytest.approx(-1212.26, abs=1e-6)
    assert db_cum == pytest.approx(-1212.26, abs=1e-6)
    assert abs(diff) <= 0.01


def test_assertion_tolerates_floating_point_dust():
    """Ledger reads from JSON, DB sums float pnls — the two paths
    accumulate fp noise differently. We use ±₹0.01 tolerance so a
    sub-paise discrepancy doesn't false-positive as DRIFT."""
    agent = _make_agent_stub(
        ss_enabled=True,
        ledger_cum=-1212.26,
        deployed_iso="2025-01-01",
        db_pnls=[100.0, -200.0, -1112.265],
    )
    result = agent._eod_source_of_truth_assertion()
    assert result is not None
    status, _, _, diff = result
    # Difference is 0.005, within tolerance.
    assert status == "OK", (
        f"diff={diff} should be within tolerance ±0.01 — fp dust must "
        f"not false-positive as DRIFT."
    )


# ── §5/§3 case: real drift ──────────────────────────────────────────────


def test_assertion_returns_drift_when_ledger_diverges_from_db(loguru_records):
    """Drift case — exactly the failure mode the brutal review caught.
    Ledger says 0.0 (stale), DB says -₹1,212.26. Assertion must return
    ("DRIFT", ...) AND emit a CRITICAL log mentioning both numbers AND
    pointing the operator at the rebuild action."""
    agent = _make_agent_stub(
        ss_enabled=True,
        ledger_cum=0.0,
        deployed_iso="2026-05-14",
        db_pnls=[100.0, -200.0, -1112.26],
    )

    result = agent._eod_source_of_truth_assertion()

    assert result is not None
    status, ledger_cum, db_cum, diff = result
    assert status == "DRIFT"
    assert ledger_cum == 0.0
    assert db_cum == pytest.approx(-1212.26, abs=1e-6)
    assert abs(diff) > 0.01

    critical_lines = [
        r for r in loguru_records if r["level"].name == "CRITICAL"
    ]
    assert critical_lines, (
        "DRIFT must emit a CRITICAL log line via loguru. Without it, "
        "the failure mode is silent on the daemon log."
    )
    msg = "\n".join(r["message"] for r in critical_lines)
    assert "EOD-ASSERT" in msg
    assert "DRIFT" in msg
    assert "rebuild" in msg.lower(), (
        "CRITICAL message must surface the rebuild action so the "
        "operator's first action is visible without reading the docs."
    )


def test_assertion_handles_db_failure_gracefully(loguru_records):
    """DB unreachable -> ("ERROR", ...) AND a WARNING log (not CRITICAL).
    "Can't run the check" is a different failure class from "the check
    ran and found drift" — flooding CRITICAL on a transient DB hiccup
    would teach operators to ignore the alert."""
    agent = _make_agent_stub(
        ss_enabled=True,
        ledger_cum=-100.0,
        deployed_iso="2026-05-14",
        db_pnls=[],
        db_raises=True,
    )

    result = agent._eod_source_of_truth_assertion()

    assert result is not None
    status, _, _, _ = result
    assert status == "ERROR"

    warning_lines = [
        r for r in loguru_records if r["level"].name == "WARNING"
    ]
    assert any("EOD-ASSERT" in r["message"] for r in warning_lines), (
        "DB-error path must emit a WARNING (not CRITICAL) so the "
        "alert level reflects the actual severity."
    )
    critical_lines = [
        r for r in loguru_records if r["level"].name == "CRITICAL"
    ]
    assert not any("EOD-ASSERT" in r["message"] for r in critical_lines), (
        "DB-error path MUST NOT emit CRITICAL — that severity is "
        "reserved for actual DRIFT findings."
    )


def test_assertion_handles_empty_db_as_zero():
    """A fresh deployment with no closed trades yet has DB cum = 0.0.
    Ledger should also be 0.0 in that case; status OK, no spurious
    DRIFT on day 1."""
    agent = _make_agent_stub(
        ss_enabled=True,
        ledger_cum=0.0,
        deployed_iso="2026-06-01",
        db_pnls=[],  # no trades yet
    )
    result = agent._eod_source_of_truth_assertion()
    assert result is not None
    status, ledger_cum, db_cum, _ = result
    assert status == "OK"
    assert ledger_cum == 0.0
    assert db_cum == 0.0


# ── §5/§3 integration: EOD email body wires the result ─────────────────


def test_eod_summary_invokes_source_of_truth_assertion():
    """Structural guard: ``_maybe_send_eod_summary`` must CALL the
    assertion. If a future refactor splits the method or moves the
    integration into a helper, this test would catch the missed
    invocation."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Locate _maybe_send_eod_summary body
    marker = "def _maybe_send_eod_summary"
    idx = src.find(marker)
    assert idx > 0, "Must find _maybe_send_eod_summary in trading_agent.py."
    # Look at the next ~250 lines (~6KB).
    body = src[idx:idx + 8000]
    assert "_eod_source_of_truth_assertion" in body, (
        "_maybe_send_eod_summary must call _eod_source_of_truth_assertion. "
        "Without the call, the EOD email skips the reconciliation and "
        "the brutal-review Session 2 §5 fix is silently disabled."
    )


def test_eod_email_body_renders_source_of_truth_block():
    """The email body must include either a 'Source-of-truth: ledger == DB'
    happy line OR a 'Source-of-truth: DRIFT' alarm line whenever the
    tracker is enabled. We check the structural conditional here so a
    future formatter rewrite can't silently drop the banner."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    marker = "def _maybe_send_eod_summary"
    idx = src.find(marker)
    assert idx > 0
    body = src[idx:idx + 8000]
    # The block must include both branches (OK + DRIFT) so a future
    # operator sees both lines documented.
    assert "Source-of-truth: ledger == DB" in body, (
        "EOD report must render the OK case as 'Source-of-truth: "
        "ledger == DB == Rs {amount}  [OK]'."
    )
    assert "DRIFT" in body, (
        "EOD report must call the failure case 'DRIFT' verbatim so an "
        "operator skimming the inbox spots the disagreement on sight."
    )
    # Banner must be injected INTO the report f-string.
    assert "{sot_block}" in body, (
        "The sot_block local must be injected into the report f-string. "
        "If this fires, the helper runs but the result never reaches "
        "the email."
    )
