"""
Signal Audit Log
────────────────
Every ensemble signal that bubbles up is logged here — whether it became a
trade or was rejected by a gate. The resulting CSV is a data asset:

  - Power the daily gap-detector ("which gate rejected the most good signals?")
  - Justify config changes with evidence
  - Diagnose regressions after a tuning change
  - Build a replay harness for backtesting gate logic offline

One row per signal event with these columns:

  timestamp, symbol, direction, confidence, regime, price,
  strategy, contributing, outcome, reason, stop_loss, take_profit, quantity

Outcome values:
  ACCEPTED   — signal passed all gates and an order was placed
  REJECTED   — a gate blocked the signal (reason field explains which)
  SHADOW     — shadow mode on; signal would have traded but no order sent
  SKIPPED    — dedupe / already-open / SELL-without-position (informational)

Thread-safe; one file per trading day under logs/.
"""
from __future__ import annotations

import csv
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")

# STATE-11 (audit 2026-05-28): when a CSV append fails (disk full,
# permissions glitch, NFS hiccup), pre-fix the row was silently dropped
# -- the EOD diagnostic then reported "0 signals today" even though
# trades had landed. The retry queue holds up to ``_RETRY_QUEUE_MAX``
# rows in memory and re-attempts a flush at the start of each new
# log() call. Bounded so a sustained outage cannot consume RAM.
_RETRY_QUEUE_MAX = 500

_COLUMNS = [
    "timestamp", "symbol", "direction", "confidence", "regime", "price",
    "strategy", "contributing", "outcome", "reason", "stop_loss",
    "take_profit", "quantity",
]


class SignalAudit:
    """Append-only CSV writer for signal events. Zero external dependencies."""

    def __init__(self, log_dir: str = "logs"):
        self._lock = threading.Lock()
        self._log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._current_date: Optional[str] = None
        self._path: Optional[str] = None
        # STATE-11 (audit 2026-05-28): bounded in-memory retry queue
        # for rows that failed to land on disk (disk-full, perms,
        # transient NFS error). Flushed best-effort on every log()
        # call. Drops the oldest row on overflow so a sustained
        # outage cannot starve the daemon of memory.
        self._retry_queue: Deque[Dict[str, Any]] = deque(maxlen=_RETRY_QUEUE_MAX)
        self._retry_overflow_count: int = 0

    def _path_for_today(self) -> str:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._path = os.path.join(self._log_dir, f"signal_audit_{today}.csv")
            if not os.path.exists(self._path):
                with open(self._path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(_COLUMNS)
        return self._path  # type: ignore[return-value]

    def log(
        self,
        *,
        symbol: str,
        direction: str,
        confidence: float,
        regime: Optional[str],
        price: Optional[float],
        strategy: Optional[str],
        contributing: Optional[Dict[str, float]],
        outcome: str,
        reason: str = "",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        quantity: Optional[int] = None,
    ) -> None:
        """Append a single signal event to today's audit CSV."""
        contrib = ""
        if contributing:
            contrib = ";".join(f"{k}:{v:.2f}" for k, v in contributing.items())
        row: Dict[str, Any] = {
            "timestamp": datetime.now(IST).isoformat(),
            "symbol": symbol,
            "direction": direction,
            "confidence": f"{confidence:.3f}",
            "regime": regime or "",
            "price": f"{price:.2f}" if price else "",
            "strategy": strategy or "",
            "contributing": contrib,
            "outcome": outcome,
            "reason": reason,
            "stop_loss": f"{stop_loss:.2f}" if stop_loss else "",
            "take_profit": f"{take_profit:.2f}" if take_profit else "",
            "quantity": quantity if quantity is not None else "",
        }
        path = self._path_for_today()
        # STATE-11 (audit 2026-05-28): drain any retry-queued rows
        # before writing the new one. Order is preserved (FIFO) so the
        # CSV reads chronologically once the underlying error clears.
        self._drain_retry_queue(path)
        try:
            with self._lock, open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_COLUMNS)
                w.writerow(row)
        except Exception as e:
            # P2 logic-edges (2026-05-17): the OLD ``except: pass`` made
            # disk-full / permission-denied invisible. The EOD diagnostics
            # would silently report "0 signals today" instead of flagging
            # the storage problem. Now we log WARNING so the operator
            # sees the failure in the daemon log.
            #
            # STATE-11 (audit 2026-05-28): also stash the row in a
            # bounded retry queue so the next successful write picks
            # it up. Without this, a 1s NFS hiccup permanently lost
            # the row.
            try:
                from loguru import logger as _logger
                _logger.warning(
                    f"[SIGNAL-AUDIT] CSV write to {path} failed: {e!r}. "
                    f"Row queued for retry on next log() call."
                )
            except Exception:
                pass
            with self._lock:
                if len(self._retry_queue) >= _RETRY_QUEUE_MAX:
                    self._retry_overflow_count += 1
                self._retry_queue.append(row)

    def _drain_retry_queue(self, path: str) -> None:
        """STATE-11 (audit 2026-05-28): best-effort flush of queued rows.

        Called at the top of every ``log()`` call. If a queued append
        fails again, leaves the row in place so the next call retries.
        Acquires the same writer lock as ``log()`` for FIFO semantics.
        """
        with self._lock:
            if not self._retry_queue:
                return
            queued = list(self._retry_queue)
            self._retry_queue.clear()
        succeeded = 0
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_COLUMNS)
                for row in queued:
                    w.writerow(row)
                    succeeded += 1
        except Exception as e:  # noqa: BLE001 - retry must not crash log()
            # Re-queue the rows that didn't land. ``succeeded`` ones
            # are already on disk; the rest go back at the head of the
            # deque so order is preserved.
            with self._lock:
                for row in queued[succeeded:]:
                    if len(self._retry_queue) >= _RETRY_QUEUE_MAX:
                        self._retry_overflow_count += 1
                        continue
                    self._retry_queue.appendleft(row)
            try:
                from loguru import logger as _logger
                _logger.warning(
                    f"[SIGNAL-AUDIT] retry-queue flush partially failed "
                    f"({succeeded}/{len(queued)} rows recovered): {e!r}. "
                    f"Remaining rows re-queued."
                )
            except Exception:
                pass
            return
        if succeeded:
            try:
                from loguru import logger as _logger
                _logger.info(
                    f"[SIGNAL-AUDIT] retry-queue flushed {succeeded} row(s) to {path}"
                )
            except Exception:
                pass

    def summarize_today(self) -> Dict[str, Any]:
        """Quick in-process summary for EOD diagnostics. Returns counts by
        outcome and per-gate rejection breakdown."""
        path = self._path_for_today()
        stats: Dict[str, Any] = {"total": 0, "outcomes": {}, "rejections": {}}
        if not os.path.exists(path):
            return stats
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    stats["total"] += 1
                    out = row.get("outcome", "?")
                    stats["outcomes"][out] = stats["outcomes"].get(out, 0) + 1
                    if out == "REJECTED":
                        gate = row.get("reason", "?").split(":", 1)[0]
                        stats["rejections"][gate] = stats["rejections"].get(gate, 0) + 1
        except Exception as exc:
            # OBS-08 (audit 2026-05-28): pre-fix this returned partial
            # stats with no log on read failure. The EOD summary banner
            # would then quietly under-count signals/rejections. Surface
            # the failure to the caller via a sentinel field so the
            # banner can highlight "stats incomplete".
            from loguru import logger
            logger.warning(
                f"[signal-audit] summarize_today read failed for "
                f"{date_str}: {type(exc).__name__}: {exc!r}. "
                f"Returned stats are partial -- treat as lower bound."
            )
            stats["read_error"] = f"{type(exc).__name__}: {exc!r}"
        return stats
