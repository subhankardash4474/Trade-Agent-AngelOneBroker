"""Regression tests for `_run_scan` preserving open positions (2026-05-04).

Bug discovered on the 2026-05-04 mid-day restart:
  - Agent had 2 open SHORTs (RAILTEL, NIVABUPA) restored from DB.
  - On boot, `self.instruments` was [] (the watchlist hadn't been
    populated yet — it gets populated inside `_run_scan` itself).
  - The first call to `_run_scan` then ran the merge loop:
        for inst in self.instruments:   # <- empty!
            if inst["symbol"] in held_symbols and ...:
                merged.append(inst)
    Since `self.instruments` was empty, **no held position was added**
    even though held_symbols={'RAILTEL','NIVABUPA'}.
  - Net effect: the open SHORTs were silently dropped from the watchlist;
    strategies never re-evaluated them, EXIT signals never fired, and the
    brand-new exit fast-path could not protect those positions.

Fix: iterate `held_symbols` directly. If an instrument dict already exists
(use it to preserve broker tokens), otherwise construct a minimal
{"symbol": sym, "token": ""} entry — Yahoo / paper modes don't need a token.

These tests stub the scanner and simulate `_run_scan`'s merge logic
directly without booting the full agent, to keep them fast and isolated.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────
# Helper: replicate the merge logic from trading_agent._run_scan
# ─────────────────────────────────────────────────────────────


def _merge_scan_with_held(scanned, current_instruments, held_symbols):
    """Mirror of the production merge logic in `TradingAgent._run_scan`.

    Kept as a standalone function so we can test the algorithm in isolation
    without booting the full TradingAgent.
    """
    scanned_symbols = {s["symbol"] for s in scanned}
    existing_by_symbol = {inst["symbol"]: inst for inst in current_instruments}

    merged = list(scanned)
    for symbol in sorted(held_symbols):
        if symbol in scanned_symbols:
            continue
        inst = existing_by_symbol.get(symbol)
        if inst is None:
            inst = {"symbol": symbol, "token": ""}
        merged.append(inst)
    return merged


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────


class TestMergeOnFreshBoot:
    """Held positions must survive a scan even when self.instruments is []."""

    def test_held_positions_added_on_fresh_boot(self):
        """Reproduces the actual 2026-05-04 bug: empty instruments list."""
        scanned = [
            {"symbol": "VEDL", "token": "v1"},
            {"symbol": "ASTERDM", "token": "a1"},
        ]
        held = {"RAILTEL", "NIVABUPA"}
        merged = _merge_scan_with_held(scanned, current_instruments=[], held_symbols=held)

        symbols = [m["symbol"] for m in merged]
        assert "RAILTEL" in symbols, "Held position RAILTEL must survive scan merge"
        assert "NIVABUPA" in symbols, "Held position NIVABUPA must survive scan merge"
        assert "VEDL" in symbols
        assert "ASTERDM" in symbols
        assert len(merged) == 4

    def test_held_position_gets_synthetic_instrument_entry(self):
        """When no prior dict exists, a minimal one is constructed."""
        merged = _merge_scan_with_held(
            scanned=[],
            current_instruments=[],
            held_symbols={"RAILTEL"},
        )
        assert len(merged) == 1
        assert merged[0]["symbol"] == "RAILTEL"
        assert merged[0]["token"] == ""

    def test_no_held_positions_no_merge(self):
        """When portfolio is flat, scan results pass through unchanged."""
        scanned = [{"symbol": "VEDL", "token": "v1"}]
        merged = _merge_scan_with_held(scanned, current_instruments=[], held_symbols=set())
        assert len(merged) == 1
        assert merged[0]["symbol"] == "VEDL"


class TestMergeAfterFirstScan:
    """After the first scan self.instruments is populated; preserve tokens."""

    def test_existing_instrument_dict_preferred_over_synthetic(self):
        """If we already have the rich instrument dict, use it (preserves token)."""
        existing = [
            {"symbol": "RAILTEL", "token": "rich-token-from-broker", "exchange": "NSE"},
        ]
        scanned = [{"symbol": "VEDL", "token": "v1"}]
        merged = _merge_scan_with_held(
            scanned, current_instruments=existing, held_symbols={"RAILTEL"}
        )

        railtel_entry = next(m for m in merged if m["symbol"] == "RAILTEL")
        assert railtel_entry["token"] == "rich-token-from-broker"
        assert railtel_entry.get("exchange") == "NSE", \
            "rich instrument data must be preserved across rescans"

    def test_held_symbol_already_in_scan_results_not_duplicated(self):
        """If the held symbol is also in scan output, don't add it twice."""
        scanned = [
            {"symbol": "RAILTEL", "token": "fresh-token"},
            {"symbol": "VEDL", "token": "v1"},
        ]
        existing = [
            {"symbol": "RAILTEL", "token": "old-token"},
        ]
        merged = _merge_scan_with_held(
            scanned, current_instruments=existing, held_symbols={"RAILTEL"}
        )

        railtel_count = sum(1 for m in merged if m["symbol"] == "RAILTEL")
        assert railtel_count == 1, "Held symbol must appear exactly once"
        # Scan output wins — that's intentional (fresh token).
        railtel_entry = next(m for m in merged if m["symbol"] == "RAILTEL")
        assert railtel_entry["token"] == "fresh-token"


class TestMergeMatchesProductionCode:
    """Sanity check: the helper above truly mirrors production logic."""

    def test_helper_matches_run_scan_implementation(self):
        """Read trading_agent.py and confirm the merge logic uses
        held_symbols-driven iteration (not self.instruments-driven).

        This is a structural test — it fails loudly if someone reverts
        the fix back to the buggy `for inst in self.instruments` loop.
        """
        agent_src = Path(__file__).parent.parent / "trading_agent.py"
        src = agent_src.read_text(encoding="utf-8")

        # The fixed version iterates held_symbols directly.
        assert "for symbol in sorted(held_symbols):" in src, (
            "_run_scan must iterate held_symbols directly, not self.instruments. "
            "Without this, DB-restored positions silently drop off the watchlist "
            "on a fresh boot."
        )
        # The buggy pattern must NOT exist in _run_scan.
        # (The pattern can legitimately appear elsewhere; we check the
        # specific buggy form within an inst["symbol"] held_symbols filter.)
        assert "for inst in self.instruments:\n                    if inst[\"symbol\"] in held_symbols" not in src, (
            "buggy iteration pattern reintroduced — see comment in "
            "test_scan_preserves_held_positions.py"
        )
