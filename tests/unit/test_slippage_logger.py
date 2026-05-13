"""Unit tests for tools/_slippage_logger.py.

Covers:
  - Sign convention (trader-perspective adversity: positive = adverse,
    negative = favourable, for both BUY and SELL).
  - Append behaviour: header is written on first write, never re-written
    on subsequent appends.
  - Summary aggregation: per-side means + favourable/adverse counts.
  - Today's Stage 2.1 fills round-trip the math exactly as the
    post-mortem doc claims (BUY -4.5 bps, SELL +9.0 bps, mean +2.25 bps).
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytz

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import _slippage_logger as sl  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def tmp_log(tmp_path):
    return tmp_path / "slippage_log.csv"


class TestSignConvention:
    """Adversity-from-trader-perspective.

    BUY filled HIGHER than LTP -> we paid more  -> adverse    -> POSITIVE bps
    BUY filled LOWER  than LTP -> we paid less  -> favourable -> NEGATIVE bps
    SELL filled LOWER  than LTP -> we got less  -> adverse    -> POSITIVE bps
    SELL filled HIGHER than LTP -> we got more  -> favourable -> NEGATIVE bps
    """

    def test_buy_favourable(self):
        # 22.21 -> 22.20: paid 1 paise below mid
        assert sl.compute_slippage_bps("BUY", 22.21, 22.20) == pytest.approx(-4.5, abs=0.05)

    def test_buy_adverse(self):
        # 22.21 -> 22.25: paid 4 paise above mid
        assert sl.compute_slippage_bps("BUY", 22.21, 22.25) > 0
        assert sl.compute_slippage_bps("BUY", 22.21, 22.25) == pytest.approx(18.0, abs=0.1)

    def test_sell_adverse(self):
        # 22.21 -> 22.19: received 2 paise below mid
        assert sl.compute_slippage_bps("SELL", 22.21, 22.19) == pytest.approx(9.0, abs=0.05)

    def test_sell_favourable(self):
        # 22.21 -> 22.25: received 4 paise above mid
        assert sl.compute_slippage_bps("SELL", 22.21, 22.25) == pytest.approx(-18.0, abs=0.1)

    def test_zero_ltp_returns_zero(self):
        """Guard against division-by-zero when broker LTP feed glitches."""
        assert sl.compute_slippage_bps("BUY", 0.0, 22.20) == 0.0

    def test_exact_match_returns_zero(self):
        assert sl.compute_slippage_bps("BUY", 22.20, 22.20) == 0.0


class TestAppendFill:
    def test_first_append_writes_header(self, tmp_log):
        sl.append_fill(
            symbol="YESBANK-EQ", side="BUY",
            limit_price=22.25, ltp_at_decision=22.21,
            filled_price=22.20, quantity=1,
            source="stage21", path=tmp_log,
        )
        rows = tmp_log.read_text(encoding="utf-8").splitlines()
        assert rows[0].startswith("timestamp_ist,symbol,side,limit_price")
        assert "YESBANK-EQ" in rows[1]
        assert "BUY" in rows[1]

    def test_second_append_does_not_duplicate_header(self, tmp_log):
        for side, fill in (("BUY", 22.20), ("SELL", 22.19)):
            sl.append_fill(
                symbol="YESBANK-EQ", side=side,
                limit_price=22.25 if side == "BUY" else 22.17,
                ltp_at_decision=22.21,
                filled_price=fill, quantity=1,
                source="stage21", path=tmp_log,
            )
        rows = tmp_log.read_text(encoding="utf-8").splitlines()
        # Exactly one header + two data rows
        assert len(rows) == 3
        assert rows.count(rows[0]) == 1

    def test_csv_is_parseable_by_dictreader(self, tmp_log):
        sl.append_fill(
            symbol="HDFCBANK-EQ", side="BUY",
            limit_price=1620.00, ltp_at_decision=1618.50,
            filled_price=1619.00, quantity=1,
            source="daemon_live", path=tmp_log,
        )
        with tmp_log.open("r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "HDFCBANK-EQ"
        assert rows[0]["source"] == "daemon_live"
        # Slippage value should round-trip exactly as written
        assert float(rows[0]["slippage_bps"]) == pytest.approx(
            sl.compute_slippage_bps("BUY", 1618.50, 1619.00), abs=0.05
        )

    def test_explicit_timestamp_preserved(self, tmp_log):
        ts = IST.localize(datetime(2026, 5, 13, 10, 1, 42))
        sl.append_fill(
            symbol="YESBANK-EQ", side="BUY",
            limit_price=22.25, ltp_at_decision=22.21,
            filled_price=22.20, quantity=1,
            source="stage21",
            timestamp_ist=ts, path=tmp_log,
        )
        body = tmp_log.read_text(encoding="utf-8")
        assert "2026-05-13T10:01:42+05:30" in body


class TestSummarize:
    def test_empty_file_returns_count_zero(self, tmp_log):
        s = sl.summarize(path=tmp_log)
        assert s == {"count": 0}

    def test_stage21_seed_summary(self, tmp_log):
        """The exact two fills from the Stage 2.1 post-mortem."""
        sl.append_fill(
            symbol="YESBANK-EQ", side="BUY",
            limit_price=22.25, ltp_at_decision=22.21,
            filled_price=22.20, quantity=1,
            source="stage21", path=tmp_log,
        )
        sl.append_fill(
            symbol="YESBANK-EQ", side="SELL",
            limit_price=22.17, ltp_at_decision=22.21,
            filled_price=22.19, quantity=1,
            source="stage21", path=tmp_log,
        )
        s = sl.summarize(path=tmp_log)
        assert s["count"] == 2
        assert s["buy_count"] == 1
        assert s["sell_count"] == 1
        assert s["mean_buy_bps"]  == pytest.approx(-4.5, abs=0.05)
        assert s["mean_sell_bps"] == pytest.approx(+9.0, abs=0.05)
        assert s["mean_all_bps"]  == pytest.approx(+2.25, abs=0.05)
        assert s["favourable_count"] == 1
        assert s["adverse_count"]    == 1
