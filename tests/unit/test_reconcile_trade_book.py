"""Unit tests for tools/reconcile_trade_book.py.

The reconciliation logic has two pure-function pieces we can test
without a live broker connection:

  1. ``_expand_db_trade_to_legs``: a closed DB trade row -> 2 broker legs
     (entry leg + exit leg, with the exit side being the opposite of the
     entry).

  2. ``_aggregate`` + ``reconcile.reconcile()`` taking pre-computed leg
     lists from each source.

For full reconcile() coverage we monkeypatch ``_load_db_legs`` and
``_broker_legs_for_day`` so the test does not need SQLite or AngelOne.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import reconcile_trade_book as rtb  # noqa: E402


class TestExpandDbTradeToLegs:
    def test_long_trade_expands_to_buy_then_sell(self):
        row = {"symbol": "YESBANK-EQ", "side": "BUY", "quantity": 16}
        legs = rtb._expand_db_trade_to_legs(row)
        assert legs == [
            ("YESBANK-EQ", "BUY",  16),
            ("YESBANK-EQ", "SELL", 16),
        ]

    def test_short_trade_expands_to_sell_then_buy(self):
        row = {"symbol": "HCLTECH", "side": "SELL", "quantity": 16}
        legs = rtb._expand_db_trade_to_legs(row)
        assert legs == [
            ("HCLTECH", "SELL", 16),
            ("HCLTECH", "BUY",  16),
        ]

    def test_malformed_row_returns_empty(self):
        assert rtb._expand_db_trade_to_legs({}) == []
        assert rtb._expand_db_trade_to_legs({"symbol": "X", "side": "BUY", "quantity": 0}) == []
        assert rtb._expand_db_trade_to_legs({"symbol": "X", "side": "HOLD", "quantity": 1}) == []

    def test_case_insensitive_side(self):
        row = {"symbol": "X", "side": "buy", "quantity": 5}
        legs = rtb._expand_db_trade_to_legs(row)
        assert legs == [("X", "BUY", 5), ("X", "SELL", 5)]


class TestAggregate:
    def test_buckets_by_symbol_and_side(self):
        legs = [
            ("YESBANK-EQ", "BUY",  1),
            ("YESBANK-EQ", "SELL", 1),
            ("HCLTECH",    "SELL", 16),
            ("HCLTECH",    "BUY",  16),
            ("YESBANK-EQ", "BUY",  1),  # second BUY of same symbol
        ]
        agg = rtb._aggregate(legs)
        assert agg[("YESBANK-EQ", "BUY")]  == {"count": 2, "qty": 2}
        assert agg[("YESBANK-EQ", "SELL")] == {"count": 1, "qty": 1}
        assert agg[("HCLTECH",    "BUY")]  == {"count": 1, "qty": 16}
        assert agg[("HCLTECH",    "SELL")] == {"count": 1, "qty": 16}


class TestReconcile:
    """End-to-end reconcile with both sources monkeypatched."""

    def test_perfect_match_returns_ok(self, monkeypatch, tmp_path):
        legs = [
            ("YESBANK-EQ", "BUY",  1),
            ("YESBANK-EQ", "SELL", 1),
        ]
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: list(legs))
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: list(legs))

        report = rtb.reconcile(
            db_path=tmp_path / "fake.db",
            config_path=tmp_path / "fake.yaml",
            day_iso="2026-05-13",
            ignore_symbols=set(),
        )
        assert report["ok"] is True
        assert report["mismatches"] == 0
        assert report["db_legs"] == 2
        assert report["broker_legs"] == 2
        assert all(b["ok"] for b in report["buckets"])

    def test_missing_broker_leg_flagged(self, monkeypatch, tmp_path):
        """DB shows a round-trip the broker never recorded -- canonical
        P0 mismatch (we made up a trade that didn't actually happen)."""
        db_legs     = [("X", "BUY", 5), ("X", "SELL", 5)]
        broker_legs = [("X", "BUY", 5)]  # missing the SELL
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: db_legs)
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: broker_legs)

        report = rtb.reconcile(
            db_path=tmp_path / "x.db", config_path=tmp_path / "x.yaml",
            day_iso="2026-05-13", ignore_symbols=set(),
        )
        assert report["ok"] is False
        assert report["mismatches"] == 1
        # The SELL bucket is the one that doesn't match
        sells = [b for b in report["buckets"] if b["side"] == "SELL"]
        assert len(sells) == 1
        assert sells[0]["db_qty"] == 5
        assert sells[0]["broker_qty"] == 0
        assert sells[0]["ok"] is False

    def test_extra_broker_leg_flagged(self, monkeypatch, tmp_path):
        """Broker recorded a fill our daemon never logged -- canonical
        P0 mismatch (we placed an order someone else / a stuck retry
        executed)."""
        db_legs     = []
        broker_legs = [("X", "BUY", 3)]
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: db_legs)
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: broker_legs)

        report = rtb.reconcile(
            db_path=tmp_path / "x.db", config_path=tmp_path / "x.yaml",
            day_iso="2026-05-13", ignore_symbols=set(),
        )
        assert report["ok"] is False
        assert report["mismatches"] == 1

    def test_qty_mismatch_flagged(self, monkeypatch, tmp_path):
        """Partial fill scenarios: broker filled 3 of a 5-share order
        but DB recorded the full 5 (bug)."""
        db_legs     = [("X", "BUY", 5), ("X", "SELL", 5)]
        broker_legs = [("X", "BUY", 3), ("X", "SELL", 5)]
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: db_legs)
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: broker_legs)

        report = rtb.reconcile(
            db_path=tmp_path / "x.db", config_path=tmp_path / "x.yaml",
            day_iso="2026-05-13", ignore_symbols=set(),
        )
        assert report["mismatches"] == 1

    def test_ignore_symbols_silences_open_position(self, monkeypatch, tmp_path):
        """For an open position, the broker has the entry leg but our DB
        doesn't have a closed-trade row yet -- expected mid-day. Using
        --ignore-symbols X should silence this."""
        db_legs     = []
        broker_legs = [("HCLTECH", "SELL", 16)]
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: db_legs)
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: broker_legs)

        report = rtb.reconcile(
            db_path=tmp_path / "x.db", config_path=tmp_path / "x.yaml",
            day_iso="2026-05-13", ignore_symbols={"HCLTECH"},
        )
        # The HCLTECH bucket should not appear in the buckets at all,
        # and the mismatch count should be 0.
        assert all(b["symbol"] != "HCLTECH" for b in report["buckets"])
        assert report["mismatches"] == 0
        assert report["ok"] is True

    def test_empty_day_returns_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rtb, "_load_db_legs",        lambda *a, **k: [])
        monkeypatch.setattr(rtb, "_broker_legs_for_day", lambda *a, **k: [])
        report = rtb.reconcile(
            db_path=tmp_path / "x.db", config_path=tmp_path / "x.yaml",
            day_iso="2026-05-13", ignore_symbols=set(),
        )
        assert report["ok"] is True
        assert report["buckets"] == []
        assert report["db_legs"] == 0
        assert report["broker_legs"] == 0


class TestFormatReport:
    """The human-readable formatter is used by the CLI; sanity-check it
    doesn't crash on edge inputs and includes the verdict line."""

    def test_format_ok_report(self):
        report = {
            "date": "2026-05-13",
            "db_legs": 2,
            "broker_legs": 2,
            "buckets": [
                {"symbol": "YESBANK-EQ", "side": "BUY",  "db_count": 1,
                 "db_qty": 1, "broker_count": 1, "broker_qty": 1, "ok": True},
                {"symbol": "YESBANK-EQ", "side": "SELL", "db_count": 1,
                 "db_qty": 1, "broker_count": 1, "broker_qty": 1, "ok": True},
            ],
            "mismatches": 0,
            "ok": True,
        }
        out = rtb._format_report(report)
        assert "VERDICT: OK" in out
        assert "YESBANK-EQ" in out

    def test_format_mismatch_report(self):
        report = {
            "date": "2026-05-13",
            "db_legs": 1, "broker_legs": 0,
            "buckets": [
                {"symbol": "X", "side": "BUY", "db_count": 1, "db_qty": 5,
                 "broker_count": 0, "broker_qty": 0, "ok": False},
            ],
            "mismatches": 1, "ok": False,
        }
        out = rtb._format_report(report)
        assert "MISMATCH" in out
        assert "INVESTIGATE" in out

    def test_format_empty_report(self):
        report = {
            "date": "2026-05-13",
            "db_legs": 0, "broker_legs": 0,
            "buckets": [], "mismatches": 0, "ok": True,
        }
        out = rtb._format_report(report)
        assert "no trades to reconcile" in out
