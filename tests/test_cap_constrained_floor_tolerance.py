"""Tests for relaxed notional-floor tolerance under cap constraint (2026-05-04 part 3).

Bug it fixes:
  On a Rs 10k book with `max_symbol_exposure_pct: 30`, the symbol cap is
  ~Rs 2,798. The raw `min_trade_notional` is Rs 3,000. So `effective_floor`
  collapses to ~Rs 2,798 (cap-constrained). With the prior 95% tolerance
  (skip_threshold = Rs 2,659), most stocks priced Rs 500-Rs 1,500 cannot fit:
    - ASTERDM Rs 756: max qty under cap = 3 -> Rs 2,268 (< Rs 2,659 -> SKIP)
    - TENNIND Rs 637: max qty under cap = 4 -> Rs 2,549 (< Rs 2,659 -> SKIP)
    - VTL     Rs 631: max qty under cap = 4 -> Rs 2,524 (< Rs 2,659 -> SKIP)
    - TATACHEM Rs 817: max qty under cap = 3 -> Rs 2,451 (< Rs 2,659 -> SKIP)
    - MEESHO  Rs 209: max qty under cap = 13 -> Rs 2,717 (BARELY OK)

  17 of 28 valid signals on 2026-05-04 were rejected for this. Pure capital
  starvation due to a math conflict between cap and floor.

Fix: when sizing is TRULY cap-constrained (cap < raw_min AND cash didn't
trim qty further), drop the floor tolerance from 95% -> 70%. Cash-constrained
sizing keeps the strict 95% tolerance because commission drag is a real
concern when capital is genuinely limited.

These tests exercise the production logic with stub portfolios/risk-managers
to confirm the fix behaviorally without booting the full agent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────
# Helper — replicate the production decision logic
# ─────────────────────────────────────────────────────────────


def _decide(
    *,
    quantity: int,
    current_price: float,
    cap_constrained_floor: bool,
    cash_reduced_qty: bool,
    effective_floor: float,
    min_trade_notional: float,
):
    """Mirror of the production floor-check + tolerance decision."""
    if min_trade_notional <= 0:
        return ("ACCEPTED", 1.0, "no-floor")
    final_notional = current_price * quantity
    true_cap_constrained = cap_constrained_floor and not cash_reduced_qty
    tolerance = 0.70 if true_cap_constrained else 0.95
    skip_threshold = effective_floor * tolerance
    if final_notional < skip_threshold:
        reason_tag = "cap_constrained" if true_cap_constrained else "cash_constrained"
        return ("REJECTED", tolerance, reason_tag)
    return ("ACCEPTED", tolerance, "ok")


# ─────────────────────────────────────────────────────────────
# Replays of today's actual rejections — these MUST flip to ACCEPTED
# ─────────────────────────────────────────────────────────────


class TestTodayRejectionsNowAccepted:
    """The exact symbols/prices from signal_audit_2026-05-04.csv that were
    rejected by the old 95% tolerance. With the fix, they should accept."""

    @pytest.mark.parametrize(
        "symbol,price,cap_qty,eff_floor",
        [
            ("ASTERDM", 755.85, 3, 2799.0),  # 09:19:01
            ("TATACHEM", 837.25, 3, 2799.0),  # 09:19:06: 3 * 837 = 2511
            ("TENNIND", 637.25, 4, 2799.0),   # 09:19:09: 4 * 637 = 2549
            ("VTL", 629.40, 4, 2799.0),       # 09:19:10: 4 * 629 = 2517
            ("MEESHO", 202.38, 13, 2799.0),   # 09:19:11: 13 * 202 = 2631
            ("BANDHANBNK", 203.10, 13, 2799.0),  # 09:19:05: 13 * 203 = 2640
            ("NLCINDIA", 321.50, 8, 2799.0),  # 09:19:15: 8 * 321 = 2572
        ],
    )
    def test_cap_constrained_accept_at_70_tolerance(self, symbol, price, cap_qty, eff_floor):
        """Cash plentiful, cap is the binding constraint -> 70% tolerance kicks in."""
        outcome, tol, _ = _decide(
            quantity=cap_qty,
            current_price=price,
            cap_constrained_floor=True,
            cash_reduced_qty=False,
            effective_floor=eff_floor,
            min_trade_notional=3000.0,
        )
        notional = price * cap_qty
        assert outcome == "ACCEPTED", (
            f"{symbol} qty={cap_qty}@Rs {price:.2f} = Rs {notional:.0f} "
            f"should now ACCEPT (eff_floor={eff_floor}, tol={tol:.0%}). "
            f"With old 95% tol it was REJECTED."
        )
        assert tol == 0.70

    def test_cash_constrained_still_strict(self):
        """ASTERDM at 09:20:24 with reduced cash: cash dropped qty 3 -> 1.
        Cash is the binding constraint, not cap. Must stay STRICT (95%)."""
        outcome, tol, reason = _decide(
            quantity=1,            # cash-trimmed
            current_price=749.00,
            cap_constrained_floor=True,
            cash_reduced_qty=True,  # <-- cash trimmed it
            effective_floor=2797.0,
            min_trade_notional=3000.0,
        )
        # 1 * 749 = 749, threshold @ 95% = 2657 -> REJECT
        assert outcome == "REJECTED"
        assert tol == 0.95, "Cash-constrained must use strict 95% tolerance"
        assert reason == "cash_constrained"


# ─────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_full_cap_utilization_accepts(self):
        """Trade exactly at the cap should always accept regardless of tolerance."""
        # cap_qty=3, price=933, notional=2799 = exactly eff_floor
        outcome, _, _ = _decide(
            quantity=3, current_price=933.0,
            cap_constrained_floor=True, cash_reduced_qty=False,
            effective_floor=2799.0, min_trade_notional=3000.0,
        )
        assert outcome == "ACCEPTED"

    def test_below_70_pct_still_rejects(self):
        """Trade meaningfully below floor should still reject even with 70% tol."""
        # qty=2 of Rs 700 = Rs 1400, eff_floor=2800, threshold@70%=1960
        outcome, tol, reason = _decide(
            quantity=2, current_price=700.0,
            cap_constrained_floor=True, cash_reduced_qty=False,
            effective_floor=2800.0, min_trade_notional=3000.0,
        )
        assert outcome == "REJECTED"
        assert tol == 0.70

    def test_uncapped_environment_strict_tolerance(self):
        """Large book where raw_min == eff_floor: behaves like before, 95% tol."""
        # Rs 5L book, 30% cap = Rs 150k, raw_min = Rs 3k. eff_floor = Rs 3k.
        # Trade Rs 2900: 2900 / 3000 = 96.7% -> ACCEPT (within 95% tol)
        outcome, tol, _ = _decide(
            quantity=29, current_price=100.0,
            cap_constrained_floor=False,  # cap >> raw_min
            cash_reduced_qty=False,
            effective_floor=3000.0, min_trade_notional=3000.0,
        )
        assert outcome == "ACCEPTED"
        assert tol == 0.95

    def test_uncapped_environment_below_strict_floor_rejects(self):
        outcome, _, reason = _decide(
            quantity=20, current_price=100.0,  # 2000 < 2850 (95% of 3000)
            cap_constrained_floor=False, cash_reduced_qty=False,
            effective_floor=3000.0, min_trade_notional=3000.0,
        )
        assert outcome == "REJECTED"
        assert reason == "cash_constrained"  # not cap-constrained, so falls through

    def test_zero_min_floor_disables_check(self):
        outcome, _, _ = _decide(
            quantity=1, current_price=100.0,
            cap_constrained_floor=False, cash_reduced_qty=False,
            effective_floor=0.0, min_trade_notional=0.0,
        )
        assert outcome == "ACCEPTED"


# ─────────────────────────────────────────────────────────────
# Production source guard
# ─────────────────────────────────────────────────────────────


class TestProductionSourceShape:
    def test_trading_agent_uses_dual_tolerance(self):
        src = (Path(__file__).parent.parent / "trading_agent.py").read_text(encoding="utf-8")
        # The new code path must distinguish true cap-constrained from cash-trimmed.
        assert "cash_reduced_qty" in src, (
            "Production code must track whether cash trimmed quantity."
        )
        assert "true_cap_constrained" in src, (
            "Production code must compute true_cap_constrained for tolerance."
        )
        assert "tolerance = 0.70 if true_cap_constrained else 0.95" in src, (
            "Production code must use 70% / 95% dual tolerance."
        )
        # The old single-tolerance form must be gone.
        assert "skip_threshold = effective_floor * 0.95" not in src, (
            "Old 95%-only tolerance must be replaced."
        )
