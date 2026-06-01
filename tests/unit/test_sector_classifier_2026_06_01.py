"""Pin tests for the sector classifier (Phase 11, charter §3.6).

Verifies the V4 Mode A universe (75 instruments per
data/v4_universe_swing_cash.txt) is fully covered with no
"unknown" assignments — a future symbol added to the universe
without a corresponding sector mapping would silently bypass the
sector cap, which is unsafe.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.instruments.etf_universe import load_v4_swing_cash_universe
from core.instruments.sector_classifier import (
    SECTORS,
    sector_for,
    sectors_in_universe,
)


class TestSectorMappingCoverage:
    """Every symbol in the V4 swing-cash universe MUST have a sector
    assignment. An unmapped symbol would silently bypass the
    sector cap (sector_for returns 'unknown' and the cap check
    treats 'unknown' as a bucket-of-one — but that's brittle).
    """

    def test_all_universe_symbols_have_sector(self):
        universe = load_v4_swing_cash_universe(
            exclude_cash_sweep=False, yfinance_suffix=False
        )
        unmapped = [s for s in universe if sector_for(s) == "unknown"]
        assert not unmapped, (
            f"Universe symbols without sector mapping: {unmapped}. "
            f"Add them to packages/core/instruments/sector_classifier.py."
        )

    def test_sectors_in_universe_groups_correctly(self):
        universe = load_v4_swing_cash_universe(
            exclude_cash_sweep=False, yfinance_suffix=False
        )
        groups = sectors_in_universe(universe)
        # No unknown bucket should appear
        assert "unknown" not in groups
        # Sum of sector members == universe size
        total = sum(len(v) for v in groups.values())
        assert total == len(universe), (
            f"Sector grouping lost {len(universe) - total} symbols"
        )


class TestSectorAssignments:
    """Spot-check key assignments to catch typos."""

    def test_adani_family_in_own_bucket(self):
        # V32 attribution flagged Adani concentration as risk; the
        # whole family must collapse into one bucket so the cap
        # actually constrains the joint exposure.
        for sym in ("ADANIENT", "ADANIPORTS", "ADANIGREEN", "ADANIPOWER"):
            assert sector_for(sym) == "adani_group", (
                f"{sym} not in adani_group bucket"
            )

    def test_banks_in_financials(self):
        for sym in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"):
            assert sector_for(sym) == "financials"

    def test_it_majors_in_it(self):
        for sym in ("INFY", "TCS", "HCLTECH", "WIPRO", "TECHM"):
            assert sector_for(sym) == "it"

    def test_metals_separate_from_cement(self):
        # GRASIM is cement-led (Aditya Birla), not metals
        assert sector_for("GRASIM") == "cement"
        assert sector_for("TATASTEEL") == "metals"
        assert sector_for("JSWSTEEL") == "metals"

    def test_etfs_each_get_distinct_bucket(self):
        # NIFTYBEES + JUNIORBEES are both broad-market → same bucket
        # so the cap treats "broad ETF exposure" as one concentration.
        assert sector_for("NIFTYBEES") == sector_for("JUNIORBEES")
        # SILVERBEES + GOLDBEES are different commodities → DIFFERENT buckets
        assert sector_for("SILVERBEES") != sector_for("GOLDBEES")
        # Sector ETFs separate from individual stocks in the same sector
        assert sector_for("ITBEES") != sector_for("INFY")
        assert sector_for("BANKBEES") != sector_for("HDFCBANK")

    def test_unknown_symbol_returns_unknown(self):
        assert sector_for("DOES_NOT_EXIST") == "unknown"
        assert sector_for("XYZ123") == "unknown"

    def test_case_insensitive_lookup(self):
        assert sector_for("hdfcbank") == sector_for("HDFCBANK")
        assert sector_for("RelIanCe") == "energy"
