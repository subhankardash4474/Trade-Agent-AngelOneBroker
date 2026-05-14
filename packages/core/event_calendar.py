"""
Event Blackout Calendar (2026-05-14)

Skip new entries on stocks that have a known market-moving event within
the configured horizon -- earnings, dividend ex-date, AGM, board meeting,
buyback record date, etc.

Industry rationale: results-day moves are 5-15% on average and direction
is unpredictable from technicals alone. A trend-follower entering the
afternoon before results is taking a coin-flip with a thesis-window of
hours, not days. This module is the deterministic bouncer.

Calendar source: a simple CSV (or YAML) at `data/event_calendar.csv`
with one row per (symbol, event_date, event_type). The file is reloaded
once per trading session by `EventCalendar.maybe_reload()`.

CSV format (header required):
    symbol,event_date,event_type,notes
    HDFCBANK,2026-05-15,results,Q4_FY26
    TCS,2026-05-22,results,
    RELIANCE,2026-05-20,dividend,ex-date

Empty file = no blackouts (permissive/legacy behaviour). The agent will
log a warning if the file is missing entirely so the operator knows the
feature is silently inert.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pytz
from loguru import logger


IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class Event:
    symbol: str
    event_date: date
    event_type: str
    notes: str = ""

    def to_display(self) -> str:
        suffix = f" ({self.notes})" if self.notes else ""
        return f"{self.event_type}@{self.event_date.isoformat()}{suffix}"


class EventCalendar:
    """File-backed earnings / event calendar.

    Designed to fail-open: if the file is missing or malformed, the
    calendar is empty and `is_blackout()` returns False, so the agent's
    legacy behaviour is preserved.

    Reload semantics: cheap (CSV with <500 rows expected). The agent
    calls `maybe_reload()` once per session start; tests can call
    `force_reload()` directly.
    """

    def __init__(self, path: str, blackout_days_before: int = 1,
                 blackout_days_after: int = 0,
                 event_types: Optional[List[str]] = None):
        """
        Args:
            path: Path to the CSV calendar file.
            blackout_days_before: Skip entries this many trading days
                before the event (default 1 = block the day before).
            blackout_days_after: Skip entries this many trading days
                after the event (default 0 = trade as normal post-event).
            event_types: Whitelist of event types to honour. None = all.
                Common types: ``results``, ``dividend``, ``agm``,
                ``board_meeting``, ``buyback``, ``stock_split``.
        """
        self.path = path
        self.blackout_days_before = max(0, int(blackout_days_before))
        self.blackout_days_after = max(0, int(blackout_days_after))
        self.event_types = (
            {t.lower() for t in event_types} if event_types else None
        )
        self._events_by_symbol: Dict[str, List[Event]] = {}
        self._mtime: float = 0.0
        self._loaded_once: bool = False

    # ── Loading ──────────────────────────────────────────────────────

    def force_reload(self) -> int:
        """Re-read the file. Returns number of events loaded."""
        events: Dict[str, List[Event]] = {}
        if not os.path.exists(self.path):
            if not self._loaded_once:
                logger.warning(
                    f"[EVENT-CAL] Calendar file not found: {self.path}. "
                    f"Blackouts disabled (no file -> permissive). To enable, "
                    f"create the CSV with header: symbol,event_date,event_type,notes"
                )
            self._events_by_symbol = events
            self._mtime = 0.0
            self._loaded_once = True
            return 0

        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                count = 0
                for row in reader:
                    sym = (row.get("symbol") or "").strip().upper()
                    raw_date = (row.get("event_date") or "").strip()
                    etype = (row.get("event_type") or "").strip().lower()
                    notes = (row.get("notes") or "").strip()
                    if not sym or not raw_date or not etype:
                        continue
                    try:
                        ev_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                    except ValueError:
                        logger.warning(
                            f"[EVENT-CAL] bad event_date '{raw_date}' for {sym}; skipping"
                        )
                        continue
                    if self.event_types is not None and etype not in self.event_types:
                        continue
                    events.setdefault(sym, []).append(
                        Event(symbol=sym, event_date=ev_date, event_type=etype, notes=notes)
                    )
                    count += 1
            self._events_by_symbol = events
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                self._mtime = 0.0
            self._loaded_once = True
            logger.info(
                f"[EVENT-CAL] Loaded {count} events for "
                f"{len(events)} symbols from {self.path}"
            )
            return count
        except Exception as e:
            logger.error(f"[EVENT-CAL] Failed to load {self.path}: {e}")
            self._events_by_symbol = {}
            self._loaded_once = True
            return 0

    def maybe_reload(self) -> None:
        """Reload if the file's mtime has changed since last load."""
        if not os.path.exists(self.path):
            if not self._loaded_once:
                self.force_reload()
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if not self._loaded_once or mtime > self._mtime:
            self.force_reload()

    # ── Query API ────────────────────────────────────────────────────

    def upcoming_events(self, symbol: str, today: Optional[date] = None) -> List[Event]:
        """All events for `symbol` with event_date >= today, sorted ascending."""
        today = today or datetime.now(IST).date()
        events = self._events_by_symbol.get(symbol.upper(), [])
        return sorted([e for e in events if e.event_date >= today], key=lambda e: e.event_date)

    def is_blackout(
        self, symbol: str, today: Optional[date] = None
    ) -> Tuple[bool, Optional[Event]]:
        """Return (True, event) if entering ``symbol`` today is blacked out.

        Blackout window = [event - blackout_days_before, event + blackout_days_after]
        (inclusive on both ends). Days are *calendar* days for simplicity --
        weekends naturally fall away because no trading happens then.

        Returns (False, None) when:
          * the calendar is empty
          * the symbol has no upcoming events
          * the nearest event is outside the window
        """
        today = today or datetime.now(IST).date()
        events = self._events_by_symbol.get(symbol.upper(), [])
        if not events:
            return False, None
        for ev in events:
            delta_days = (ev.event_date - today).days
            in_pre  = (0 <= delta_days <= self.blackout_days_before)
            in_post = (-self.blackout_days_after <= delta_days < 0)
            if in_pre or in_post:
                return True, ev
        return False, None

    # ── Introspection (for audit/checkpoint) ─────────────────────────

    def summary(self) -> Dict[str, int]:
        return {
            "symbols_with_events": len(self._events_by_symbol),
            "total_events": sum(len(v) for v in self._events_by_symbol.values()),
            "blackout_days_before": self.blackout_days_before,
            "blackout_days_after": self.blackout_days_after,
        }
