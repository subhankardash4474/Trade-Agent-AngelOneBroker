"""Tests for the 2026-05-14 self-sufficiency tracker.

The tracker must:
  1. Round-trip a clean ledger (load -> mutate -> persist -> reload).
  2. Compute correct GREEN/YELLOW/RED states from cumulative realised P&L.
  3. Be tolerant to a missing or corrupt ledger file.
  4. Be silent when disabled.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def tmp_ledger(tmp_path):
    return str(tmp_path / "self_suff.json")


def _mk_tracker(ledger_path: str, **overrides):
    from core.self_sufficiency import SelfSufficiencyTracker

    cfg = {
        "risk": {
            "self_sufficiency": {
                "enabled": True,
                "monthly_fixed_cost_inr": 4500.0,
                "trading_days_per_month": 20,
                "ledger_path": ledger_path,
                "red_floor_inr": 5000.0,
                **overrides,
            }
        }
    }
    return SelfSufficiencyTracker.from_config(cfg)


def test_first_run_seeds_ledger(tmp_ledger):
    """No file on disk → tracker creates one with deployed_on=today."""
    assert not Path(tmp_ledger).exists()
    t = _mk_tracker(tmp_ledger)
    assert Path(tmp_ledger).exists()
    with open(tmp_ledger) as f:
        led = json.load(f)
    assert led["cumulative_realised_inr"] == 0.0
    assert led["deployed_on"] == date.today().isoformat()


def test_record_persists_atomically(tmp_ledger):
    t = _mk_tracker(tmp_ledger)
    t.record_realised_pnl(100.0)
    t.record_realised_pnl(-50.0)
    # Reload from disk to confirm persistence
    t2 = _mk_tracker(tmp_ledger)
    s = t2.status()
    assert s.cumulative_realised_inr == pytest.approx(50.0)


def test_state_green_when_profitable(tmp_ledger):
    t = _mk_tracker(tmp_ledger)
    t.record_realised_pnl(1000.0)
    s = t.status()
    assert s.state == "GREEN"
    assert "profitable" in s.note.lower()


def test_state_yellow_within_floor(tmp_ledger):
    """Cumulative loss < red_floor → YELLOW (behind, not bleeding)."""
    t = _mk_tracker(tmp_ledger, red_floor_inr=5000.0)
    t.record_realised_pnl(-1000.0)
    s = t.status()
    assert s.state == "YELLOW"
    assert s.cumulative_realised_inr == pytest.approx(-1000.0)


def test_state_red_below_floor(tmp_ledger):
    """Cumulative loss >= red_floor → RED (recommend halting LIVE)."""
    t = _mk_tracker(tmp_ledger, red_floor_inr=500.0)
    t.record_realised_pnl(-1000.0)
    s = t.status()
    assert s.state == "RED"
    assert "BLEEDING" in s.note


def test_disabled_returns_unknown(tmp_ledger):
    t = _mk_tracker(tmp_ledger)
    t.enabled = False
    s = t.status()
    assert s.state == "UNKNOWN"


def test_corrupt_ledger_does_not_crash(tmp_ledger):
    Path(tmp_ledger).write_text("{this is not json")
    t = _mk_tracker(tmp_ledger)
    s = t.status()
    # Tracker should still return a valid status (just zero realised)
    assert s.cumulative_realised_inr == 0.0


def test_daily_breakeven_calculation(tmp_ledger):
    t = _mk_tracker(tmp_ledger, monthly_fixed_cost_inr=4000.0, trading_days_per_month=20)
    assert t.daily_breakeven_inr == pytest.approx(200.0)


def test_to_dict_has_all_audit_fields(tmp_ledger):
    """The audit checkpoint surface needs a stable schema."""
    t = _mk_tracker(tmp_ledger)
    d = t.to_dict()
    for key in ("enabled", "state", "cumulative_realised_inr",
                "monthly_fixed_cost_inr", "daily_breakeven_inr",
                "days_since_deployment", "cost_burned_to_date_inr",
                "coverage_pct", "note"):
        assert key in d, f"missing audit field: {key}"


def test_replays_2026_05_14_loss(tmp_ledger):
    """End-to-end replay of today's trades via record_realised_pnl().

    Each trade enters with its NET pnl (already commission-adjusted, as
    the trading agent passes it). After all 5 closes today, ledger
    should show -Rs 592.14 cumulative.
    """
    t = _mk_tracker(tmp_ledger)
    today_trades = [
        ("PCBL",       138.23),
        ("ABLBL",     -186.43),
        ("CHOLAFIN",  -196.23),
        ("JSWENERGY", -192.75),
        ("OBEROIRLTY",-154.96),
    ]
    for _, pnl in today_trades:
        t.record_realised_pnl(pnl)
    s = t.status()
    assert s.cumulative_realised_inr == pytest.approx(-592.14)
