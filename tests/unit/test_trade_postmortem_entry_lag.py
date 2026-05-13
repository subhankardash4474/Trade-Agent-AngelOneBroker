"""Unit tests for the entry-lag diagnostic in ``tools/trade_postmortem.py``.

The diagnostic answers: "how long was a same-side signal in the system
before we actually entered?" The non-obvious bits are:

  * The signal_audit logs an ACCEPTED row at *fill confirmation* time,
    which is a few hundred milliseconds **after** ``entry_time`` in the
    trades table. A naive ``timestamp < entry_dt`` filter excludes the
    trade's own signal -> we have to anchor on the ACCEPTED row, not on
    entry_dt.
  * Multiple ACCEPTED rows can exist for the same (symbol, direction) on
    a single day when the system re-enters after an exit (e.g. CYIENT
    09:32 + 13:50 on 2026-05-08). Each entry should anchor to the
    *closest* ACCEPTED row, not the first.
  * A position restored on daemon restart can have a trade row with no
    matching ACCEPTED audit entry -- the helper should fall back to
    ``entry_dt`` as the entry marker rather than refusing to compute a
    lag.

These tests guard those properties against regressions.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.trade_postmortem import (  # noqa: E402
    compute_entry_lag,
    LATE_ENTRY_THRESHOLD_MIN,
)

IST = pytz.timezone("Asia/Kolkata")


def _row(ts: str, symbol: str, direction: str, outcome: str,
         reason: str = "") -> dict:
    return {
        "timestamp": pd.Timestamp(ts).tz_convert(IST)
                     if pd.Timestamp(ts).tzinfo
                     else pd.Timestamp(ts).tz_localize(IST),
        "symbol": symbol,
        "direction": direction,
        "outcome": outcome,
        "reason": reason,
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_instant_fill_returns_zero_lag():
    """A single ACCEPTED row at the same second as entry -> lag == 0."""
    df = _df([_row("2026-05-08T09:32:07.151+05:30", "TITAGARH", "SELL",
                   "ACCEPTED", "filled_filled")])
    entry = datetime(2026, 5, 8, 9, 32, 6, 194136)
    res = compute_entry_lag("TITAGARH", "SELL", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(0.0, abs=0.05)
    assert res["signals_seen_count"] == 1
    assert res["rejected_count"] == 0


def test_rejection_trail_produces_late_entry():
    """Two REJECTED signals before the ACCEPTED fill -> lag = time from
    first REJECTED to ACCEPTED, well above the LATE-ENTRY threshold."""
    df = _df([
        _row("2026-05-06T09:20:05+05:30", "USHAMART", "SELL",
             "REJECTED", "cooldown"),
        _row("2026-05-06T10:30:00+05:30", "USHAMART", "SELL",
             "REJECTED", "min_profit_to_charges_ratio"),
        _row("2026-05-06T12:04:00+05:30", "USHAMART", "SELL",
             "ACCEPTED", "filled_filled"),
    ])
    entry = datetime(2026, 5, 6, 12, 3, 58)
    res = compute_entry_lag("USHAMART", "SELL", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(163.92, abs=0.5)
    assert res["lag_min"] > LATE_ENTRY_THRESHOLD_MIN
    assert res["signals_seen_count"] == 3
    assert res["rejected_count"] == 2


def test_re_entry_anchors_to_closest_accepted():
    """Same-day re-entry (CYIENT 09:32 BUY exit, then 13:30 + 13:50 SELL
    fills) must anchor each entry to its own ACCEPTED row, not the first
    of the day. The 13:50 entry should see a 20-min lag from the 13:30
    signal, not a 4-hour lag from market open."""
    df = _df([
        _row("2026-05-08T13:30:05+05:30", "CYIENT", "SELL",
             "ACCEPTED", "filled_filled"),
        _row("2026-05-08T13:50:07+05:30", "CYIENT", "SELL",
             "ACCEPTED", "filled_filled"),
    ])
    entry = datetime(2026, 5, 8, 13, 50, 5)
    res = compute_entry_lag("CYIENT", "SELL", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(20.0, abs=0.1)
    assert res["signals_seen_count"] == 2
    assert res["rejected_count"] == 0


def test_restored_position_no_accepted_falls_back_to_entry_dt():
    """Daemon restart restores an open position from DB. No ACCEPTED
    audit row exists for this entry. The helper should fall back to
    entry_dt as the marker rather than returning None -- we still want
    to report any prior REJECTED signals as lag context."""
    df = _df([
        _row("2026-05-09T09:30:00+05:30", "RELIANCE", "BUY",
             "REJECTED", "ensemble_confidence_below_threshold"),
    ])
    entry = datetime(2026, 5, 9, 10, 15, 0)
    res = compute_entry_lag("RELIANCE", "BUY", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(45.0, abs=0.5)
    assert res["signals_seen_count"] == 1
    assert res["rejected_count"] == 1


def test_no_audit_returns_none():
    """Defensive: missing audit data -> None, never raises."""
    entry = datetime(2026, 5, 9, 10, 0, 0)
    assert compute_entry_lag("XYZ", "BUY", entry, None) is None
    assert compute_entry_lag("XYZ", "BUY", entry, pd.DataFrame()) is None


def test_symbol_isolation():
    """Different symbols / directions must not bleed into each other.
    A SELL signal for TCS at 09:15 must not contribute to an INFY BUY
    entry's lag at 10:00."""
    df = _df([
        _row("2026-05-09T09:15:00+05:30", "TCS", "SELL",
             "REJECTED", "cooldown"),
        _row("2026-05-09T10:00:05+05:30", "INFY", "BUY",
             "ACCEPTED", "filled_filled"),
    ])
    entry = datetime(2026, 5, 9, 10, 0, 4)
    res = compute_entry_lag("INFY", "BUY", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(0.0, abs=0.05)
    assert res["signals_seen_count"] == 1


def test_direction_isolation():
    """BUY signal for a symbol should not anchor a SELL entry on the
    same symbol (would happen if a stock reversed mid-day)."""
    df = _df([
        _row("2026-05-09T09:15:00+05:30", "RELIANCE", "BUY",
             "REJECTED", "cooldown"),
        _row("2026-05-09T13:00:05+05:30", "RELIANCE", "SELL",
             "ACCEPTED", "filled_filled"),
    ])
    entry = datetime(2026, 5, 9, 13, 0, 4)
    res = compute_entry_lag("RELIANCE", "SELL", entry, df)
    assert res is not None
    assert res["lag_min"] == pytest.approx(0.0, abs=0.05)
    assert res["signals_seen_count"] == 1
