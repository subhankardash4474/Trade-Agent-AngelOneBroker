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
from datetime import datetime
from typing import Any, Dict, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")

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
            try:
                from loguru import logger as _logger
                _logger.warning(
                    f"[SIGNAL-AUDIT] CSV write to {path} failed: {e!r}. "
                    f"This row will be missing from EOD diagnostics."
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
        except Exception:
            pass
        return stats
