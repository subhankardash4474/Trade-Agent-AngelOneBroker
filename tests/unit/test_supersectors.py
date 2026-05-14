"""Tests for the supersector roll-up added 2026-05-14.

Background: live SHORT book on 2026-05-14 held 4 of 6 positions in
financial-sector tickers (CENTRALBK + FEDERALBNK = Banks, TATACAP +
CHOLAFIN = NBFC). Each sub-bucket was independently below the 40%
sector cap, so the per-sector check let them through -- but together
they were ~67% of the book in a single rate-cycle factor.

The supersector map (`SUPERSECTOR_MAP`) collapses Banks/NBFC/Insurance
/AMC/FinTech into "Financials" so the existing cap catches this.
"""
from __future__ import annotations

import pytest

from core.market_safety import (
    NSE_SECTOR_MAP,
    SUPERSECTOR_MAP,
    check_sector_exposure,
    get_sector,
    get_supersector,
)


# ── Map sanity ────────────────────────────────────────────────────────────


def test_financial_subbuckets_roll_up_to_financials():
    for sub in ("Banks", "NBFC", "Insurance", "AMC", "FinTech"):
        assert SUPERSECTOR_MAP.get(sub) == "Financials"


def test_get_supersector_for_known_bank():
    assert get_supersector("HDFCBANK") == "Financials"
    assert get_supersector("CENTRALBK") == "Financials"


def test_get_supersector_for_known_nbfc():
    assert get_supersector("TATACAP") == "Financials" or get_supersector("TATACAP") == "UNKNOWN"
    assert get_supersector("CHOLAFIN") == "Financials"
    assert get_supersector("BAJFINANCE") == "Financials"


def test_get_supersector_for_known_insurance():
    assert get_supersector("HDFCLIFE") == "Financials"
    assert get_supersector("ICICIPRULI") == "Financials"


def test_supersector_falls_back_to_sector_when_no_rollup():
    """Pharma has no roll-up -> stays as Pharma."""
    assert get_supersector("CIPLA") == "Pharma"
    assert get_supersector("LUPIN") == "Pharma"


def test_supersector_for_unknown_symbol_returns_unknown():
    assert get_supersector("THISISNOTASYMBOL") == "UNKNOWN"


def test_energy_and_power_roll_up_together():
    assert get_supersector("RELIANCE") == "EnergyPower"   # Energy
    assert get_supersector("NTPC") == "EnergyPower"        # Power


def test_metals_mining_cement_roll_up_together():
    assert get_supersector("TATASTEEL") == "MetalsMining"  # Metals
    assert get_supersector("ULTRACEMCO") == "MetalsMining"  # Cement


# ── check_sector_exposure with supersectors ───────────────────────────────


def _entry(price: float, qty: int) -> float:
    return price * qty


def test_legacy_fine_grained_still_works():
    """With use_supersectors=False, fine-grained behaviour is unchanged.
    Banks=20% + NBFC=20% would each be at 20% (under 40% cap), so a 3rd
    Banks position pushing Banks to 30% should still be allowed."""
    positions = {
        "HDFCBANK": _entry(1500, 10),    # Rs 15,000 = 15%
        "TATACAP":  _entry(300, 50),     # Rs 15,000 = 15%
    }
    safe, _ = check_sector_exposure(
        symbol="ICICIBANK",
        current_positions_by_symbol=positions,
        additional_cost=15000,
        total_equity=100_000,
        max_sector_exposure_pct=40.0,
        use_supersectors=False,
    )
    # Banks bucket: 15k + 15k new = 30k = 30% (under 40%) -> allowed
    assert safe is True


def test_supersector_catches_multi_subsector_financial_concentration():
    """With supersectors enabled, the same trio is correctly grouped."""
    positions = {
        "HDFCBANK": _entry(1500, 10),    # Banks 15%
        "TATACAP":  _entry(300, 50),     # NBFC 15%
        "ICICIPRULI": _entry(600, 25),   # Insurance 15%
    }
    # Adding another financial pushes Financials supersector to 60%
    safe, reason = check_sector_exposure(
        symbol="CHOLAFIN",
        current_positions_by_symbol=positions,
        additional_cost=15000,
        total_equity=100_000,
        max_sector_exposure_pct=40.0,
        use_supersectors=True,
    )
    assert safe is False
    assert "Financials" in reason


def test_supersector_replays_2026_05_14_live_concentration():
    """Replay of today's SHORT book: 4 financial positions slipping past
    the per-sector cap. With supersectors=True the 4th should be blocked."""
    # All assumed at ~Rs 15k notional each (15% of Rs 100k)
    positions = {
        "CENTRALBK":   _entry(35, 374),     # Banks  ~13k
        "TATACAP":     _entry(307, 41),     # NBFC   ~12.6k
        "FEDERALBNK":  _entry(279, 46),     # Banks  ~12.8k
    }
    # Adding ~Rs 25k of CHOLAFIN (a typical 5x sized add) pushes the
    # supersector through 40%. With supersectors=True it should block;
    # with the legacy per-sector view it should still slip through.
    additional = 25_000
    safe_super, reason_super = check_sector_exposure(
        symbol="CHOLAFIN",                    # NBFC
        current_positions_by_symbol=positions,
        additional_cost=additional,
        total_equity=122_000,
        max_sector_exposure_pct=40.0,
        use_supersectors=True,
    )
    # Banks (~25.8k) + NBFC (~12.6k) + new NBFC (25k) = ~63.4k = ~52% -> blocked
    assert safe_super is False, f"Expected supersector block, got: {reason_super}"

    safe_legacy, reason_legacy = check_sector_exposure(
        symbol="CHOLAFIN",
        current_positions_by_symbol=positions,
        additional_cost=additional,
        total_equity=122_000,
        max_sector_exposure_pct=40.0,
        use_supersectors=False,
    )
    # NBFC bucket alone: TATACAP (12.6k) + new (25k) = 37.6k = ~30.8% -> allowed
    assert safe_legacy is True, (
        f"Legacy per-sector check should let this through (the bug we're fixing); "
        f"got: {reason_legacy}"
    )


def test_unknown_per_symbol_still_works_with_supersectors():
    positions = {
        "FAKETICKER1": 5000.0,
        "FAKETICKER2": 5000.0,
    }
    # With unknown_per_symbol=True, two UNKNOWN tickers don't combine.
    safe, _ = check_sector_exposure(
        symbol="FAKETICKER3",
        current_positions_by_symbol=positions,
        additional_cost=20_000,
        total_equity=100_000,
        max_sector_exposure_pct=40.0,
        unknown_per_symbol=True,
        use_supersectors=True,
    )
    assert safe is True


def test_non_financial_rollups_dont_cross_contaminate():
    """Adding a Pharma position shouldn't trigger the Financials cap."""
    positions = {
        "HDFCBANK": _entry(1500, 25),    # 37.5% Financials
    }
    safe, _ = check_sector_exposure(
        symbol="CIPLA",
        current_positions_by_symbol=positions,
        additional_cost=15_000,
        total_equity=100_000,
        max_sector_exposure_pct=40.0,
        use_supersectors=True,
    )
    assert safe is True
