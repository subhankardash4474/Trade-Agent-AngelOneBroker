"""Tests for the intraday regime overlay added 2026-05-14.

Background: the existing `classify_regime` reads Nifty's position vs the
200-EMA and India VIX -- both slow-moving signals. Mid-session flash
crashes and relief rallies are invisible at that resolution. The
intraday overlay reads ``nifty_intraday_pct`` (60-min momentum) and
``vix_intraday_delta`` (vs morning open) for a faster classification.
"""
from __future__ import annotations

import pytest

from core.regime import classify_intraday_regime


def test_unknown_when_context_missing():
    assert classify_intraday_regime(None) == "unknown"
    assert classify_intraday_regime({}) == "unknown"


def test_unknown_when_both_signals_missing():
    """Even with other context fields, intraday is unknown until populated."""
    ctx = {"india_vix": 18.0, "nifty_trend": -1}
    assert classify_intraday_regime(ctx) == "unknown"


def test_neutral_when_signals_calm():
    ctx = {"nifty_intraday_pct": 0.1, "vix_intraday_delta": 0.2}
    assert classify_intraday_regime(ctx) == "neutral"


def test_risk_off_on_nifty_drop():
    ctx = {"nifty_intraday_pct": -0.7, "vix_intraday_delta": 0.0}
    assert classify_intraday_regime(ctx) == "risk_off"


def test_risk_off_on_vix_spike():
    ctx = {"nifty_intraday_pct": 0.0, "vix_intraday_delta": 2.0}
    assert classify_intraday_regime(ctx) == "risk_off"


def test_risk_off_threshold_inclusive():
    """Edge case: -0.5% Nifty exactly should already be risk_off."""
    ctx = {"nifty_intraday_pct": -0.5, "vix_intraday_delta": 0.0}
    assert classify_intraday_regime(ctx) == "risk_off"


def test_risk_on_requires_both_conditions():
    """Up move alone isn't enough -- VIX must be calm too."""
    ctx = {"nifty_intraday_pct": 0.6, "vix_intraday_delta": 1.0}
    # Vix delta 1.0 > 0.5 -> not risk_on; nifty up but vix expanding -> neutral
    assert classify_intraday_regime(ctx) == "neutral"


def test_risk_on_when_clean_rally():
    ctx = {"nifty_intraday_pct": 0.7, "vix_intraday_delta": -0.5}
    assert classify_intraday_regime(ctx) == "risk_on"


def test_risk_off_takes_precedence_over_risk_on():
    """If nifty is up but VIX is also up sharply, we err risk_off."""
    ctx = {"nifty_intraday_pct": 0.6, "vix_intraday_delta": 2.5}
    # vix_delta 2.5 >= 1.5 triggers risk_off regardless of nifty
    assert classify_intraday_regime(ctx) == "risk_off"


def test_partial_signals_return_unknown_p2_audit_fix():
    """P2 logic-edges (2026-05-17): the OLD code imputed a missing input
    to 0.0, so a +2% Nifty rally without VIX data would be labelled
    risk_on as if VIX were calm. Now we require BOTH inputs; missing
    either returns "unknown" so callers route to the permissive (no
    overlay) path instead of pretending a data gap is a calm reading.
    """
    # VIX spike but no nifty data -> unknown (cannot confirm risk_off)
    assert classify_intraday_regime({"vix_intraday_delta": 2.0}) == "unknown"
    # Nifty rally but no vix data -> unknown (cannot confirm risk_on)
    assert classify_intraday_regime({"nifty_intraday_pct": 0.7}) == "unknown"
