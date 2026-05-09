"""
Regression tests for tools/audit_checkpoint.py.

Cover the three things that have to keep working:

1. ``run_and_save`` writes both .md and .json files in the right location and
   never raises even when DB / log access fails.
2. ``_section_log_anomalies`` correctly classifies errors / warnings /
   tracebacks within the rolling window.
3. ``_section_risk_state`` parses both the legacy ``Cash=?…`` heartbeat
   format and the current ``Cash=₹…`` format.
4. ``_build_delta`` correctly diffs P&L, trade count, position count and
   error count between two consecutive snapshots.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytz

from tools import audit_checkpoint as ac

IST = pytz.timezone("Asia/Kolkata")


def _make_db(path: Path) -> None:
    """Build a minimal DB schema with a few open positions and trades."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE open_positions (
            symbol TEXT, side TEXT, entry_price REAL, quantity INTEGER,
            stop_loss REAL, take_profit REAL, strategy TEXT, regime TEXT,
            contributing_strategies TEXT, entry_time TEXT
        );
        CREATE TABLE trades (
            symbol TEXT, side TEXT, entry_price REAL, exit_price REAL,
            quantity INTEGER, pnl REAL, exit_reason TEXT,
            entry_time TEXT, exit_time TEXT
        );
        CREATE TABLE equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, equity REAL, cash REAL
        );
        """
    )
    c.execute(
        "INSERT INTO open_positions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("MANAPPURAM", "SELL", 311.10, 25, 317.34, 306.55,
         "mean_reversion", "bear_high_vol", '{"mean_reversion": 0.6}',
         "2026-05-06T11:00:00"),
    )
    c.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
        ("DABUR", "SELL", 460.05, 463.38, 17, -64.93, "stop_loss",
         "2026-05-06T11:00:00", "2026-05-06T11:50:00"),
    )
    c.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
        ("MAHABANK", "SELL", 83.86, 83.35, 94, 39.60, "signal",
         "2026-05-06T10:00:00", "2026-05-06T11:30:00"),
    )
    c.execute(
        "INSERT INTO equity_curve (timestamp, equity, cash) VALUES (?, ?, ?)",
        ("2026-05-06T11:58:30", 31750.86, 16334.61),
    )
    c.commit()
    c.close()


class TestAuditCheckpoint(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Redirect AUDIT_ROOT and LOG_DIR onto the temp directory so the test
        # never touches real production files.
        self._patch_audit = patch.object(ac, "AUDIT_ROOT", self.tmp / "audit")
        self._patch_log = patch.object(ac, "LOG_DIR", self.tmp)
        self._patch_audit.start()
        self._patch_log.start()
        # Realistic anchor inside the trading day.
        self.now = IST.localize(datetime(2026, 5, 6, 12, 0, 0))

    def tearDown(self) -> None:
        self._patch_audit.stop()
        self._patch_log.stop()
        self._tmp.cleanup()

    # ── Section: log anomalies ─────────────────────────────────────────
    def test_log_anomalies_window_filtering(self) -> None:
        log = self.tmp / "trading_agent_2026-05-06.log"
        log.write_text(
            "\n".join([
                "2026-05-06 10:30:00 | INFO     | [HEARTBEAT] inside",
                "2026-05-06 11:30:00 | ERROR    | something bad happened",
                "2026-05-06 11:31:00 | WARNING  | first warning",
                "2026-05-06 11:32:00 | WARNING  | first warning",  # repeat
                "2026-05-06 11:35:00 | INFO     | Traceback (most recent call last):",
                "2026-05-06 09:00:00 | ERROR    | OUTSIDE window",
            ]),
            encoding="utf-8",
        )
        lines = ac._read_log_lines(log)
        out = ac._section_log_anomalies(
            lines,
            since=self.now - timedelta(hours=1),
            until=self.now,
        )
        self.assertEqual(out["error_count"], 1)
        self.assertEqual(out["warning_count"], 2)
        self.assertEqual(out["traceback_count"], 1)
        # The duplicate warning should be grouped.
        self.assertEqual(out["warnings_top_groups"][0]["count"], 2)

    # ── Section: heartbeat parsing (both glyph variants) ───────────────
    def test_risk_state_parses_question_mark_glyph(self) -> None:
        """Older logs render ₹ as '?' through ANSI/encoding stripping."""
        line = (
            "2026-05-06 11:30:00 | INFO     | [HEARTBEAT] 11:30 | "
            "Cycle=10 | Positions=3 ['A','B','C'] | Cash=?16,334 | "
            "DayPnL=?-518 | Trades=7 | ConsecLoss=0 | "
            "Cooldowns=['DABUR'] | Blacklisted=[]"
        )
        out = ac._section_risk_state(
            [line],
            since=self.now - timedelta(hours=1),
            until=self.now,
        )
        self.assertTrue(out["heartbeat_seen"])
        self.assertEqual(out["cycle"], 10)
        self.assertEqual(out["positions"], 3)
        self.assertEqual(out["trades_today"], 7)
        self.assertEqual(out["cooldowns"], ["DABUR"])
        self.assertNotIn("raw", out, "regex must match — should not fall through to raw")

    def test_risk_state_parses_rupee_glyph(self) -> None:
        line = (
            "2026-05-06 11:45:00 | INFO     | [HEARTBEAT] 11:45 | "
            "Cycle=15 | Positions=2 ['X','Y'] | Cash=₹8,574 | "
            "DayPnL=₹-432 | Trades=8 | ConsecLoss=2 | "
            "Cooldowns=[] | Blacklisted=['IDEA']"
        )
        out = ac._section_risk_state(
            [line],
            since=self.now - timedelta(hours=1),
            until=self.now,
        )
        self.assertEqual(out["cycle"], 15)
        self.assertEqual(out["consec_loss"], 2)
        self.assertEqual(out["blacklisted"], ["IDEA"])

    # ── Section: positions + DB round-trip detection ───────────────────
    def test_positions_round_trip_ok(self) -> None:
        db = self.tmp / "trading_agent.db"
        _make_db(db)
        out = ac._section_positions(str(db))
        self.assertEqual(out["open_count"], 1)
        self.assertTrue(out["round_trip_ok"])
        self.assertEqual(out["positions"][0]["symbol"], "MANAPPURAM")

    def test_positions_round_trip_detects_corruption(self) -> None:
        db = self.tmp / "trading_agent.db"
        _make_db(db)
        # Inject a non-deserializable contributing_strategies blob.
        c = sqlite3.connect(db)
        c.execute(
            "INSERT INTO open_positions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("BROKEN", "BUY", 100.0, 1, 99.0, 102.0, "x", "bull",
             '{"k": not-json}', "2026-05-06T11:00:00"),
        )
        c.commit()
        c.close()
        out = ac._section_positions(str(db))
        self.assertFalse(out["round_trip_ok"])
        self.assertTrue(any(e.get("symbol") == "BROKEN" for e in out["round_trip_errors"]))

    # ── End-to-end: run_and_save writes both files and is non-fatal ───
    def test_run_and_save_writes_both_files(self) -> None:
        db = self.tmp / "trading_agent.db"
        _make_db(db)
        md, js = ac.run_and_save(
            db_path=str(db), daemon_pid=None, now=self.now, window_minutes=60,
        )
        self.assertTrue(md.exists() and js.exists())
        self.assertEqual(md.suffix, ".md")
        self.assertEqual(js.suffix, ".json")
        body = md.read_text(encoding="utf-8")
        self.assertIn("Audit Checkpoint", body)
        self.assertIn("MANAPPURAM", body)
        # Verdict line must be present.
        self.assertIn("**Verdict:**", body)

    def test_run_and_save_with_missing_db_does_not_raise(self) -> None:
        # Deliberately point at a path that doesn't exist.
        md, js = ac.run_and_save(
            db_path=str(self.tmp / "nope.db"),
            daemon_pid=None, now=self.now, window_minutes=60,
        )
        self.assertTrue(md.exists() and js.exists())
        data = json.loads(js.read_text(encoding="utf-8"))
        # The positions section should record an error rather than crashing.
        self.assertTrue(
            "error" in data["positions"] or data["positions"]["open_count"] == 0
        )

    # ── Delta computation ──────────────────────────────────────────────
    def test_build_delta_computes_correct_changes(self) -> None:
        db = self.tmp / "trading_agent.db"
        _make_db(db)
        # First snapshot at 11:00.
        ac.run_and_save(
            db_path=str(db), daemon_pid=None,
            now=IST.localize(datetime(2026, 5, 6, 11, 0, 0)),
            window_minutes=60,
        )
        # Add another closed trade (a winner).
        c = sqlite3.connect(db)
        c.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
            ("NBCC", "SELL", 95.63, 94.47, 82, 86.84, "take_profit",
             "2026-05-06T11:30:00", "2026-05-06T11:55:00"),
        )
        c.commit()
        c.close()
        # Second snapshot at 12:00.
        md, js = ac.run_and_save(
            db_path=str(db), daemon_pid=None, now=self.now, window_minutes=60,
        )
        body = md.read_text(encoding="utf-8")
        self.assertIn("Delta vs previous checkpoint", body)
        self.assertIn("Δ realised P&L", body)
        # P&L delta = +86.84 from the new winner trade.
        self.assertIn("+86.84", body)
        self.assertIn("Δ closed trades: +1", body)


if __name__ == "__main__":
    unittest.main()
