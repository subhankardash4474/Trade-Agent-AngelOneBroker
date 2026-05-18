"""P2 restart-cluster (2026-05-17) -- LIVE-MODE SAFETY: persist intraday
runtime state that the OLD code reset on every process restart.

## Why this exists

Three intraday state buckets live in `TradingAgent` memory and reset to
empty on every restart:

  * ``_strategy_state``   per-strategy circuit-breaker (consec_losses,
                          daily_pnl, suspended, suspended_reason, trades)
  * ``_recent_opens``     sliding window of (timestamp, symbol) used by
                          the global open-rate cap.
  * ``_consec_tp_today``  symbol -> consecutive TP wins, used for the
                          trend-continuation widened-TP path.

A mid-session container restart used to wipe all three. Concrete
consequences:

  * A strategy that had been suspended at 11:00 IST after 3 consecutive
    losses re-engaged on restart at 11:30 and could lose more.
  * The open-rate window forgot the last N opens, so a restart could
    burst 2x the configured opens-per-minute.

## Design

A single JSON file ``data/runtime_state.json`` holds all three. Atomic
write (temp + ``os.replace``). Stale entries are dropped on load using
the same rules the runtime enforces:

  * ``_strategy_state``: dropped if snapshot's IST date != today.
  * ``_recent_opens``: only entries within the window TTL kept.
  * ``_consec_tp_today``: dropped if snapshot's IST date != today.

Stdlib-only; mirrors ``cooldown_persistence`` exactly so the operator
mental model stays consistent across persistence modules.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DATA_DIR = Path("data")
SNAPSHOT_FILENAME = "runtime_state.json"
SCHEMA_VERSION = 1


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=".runtime_state.", suffix=".tmp"
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.astimezone(IST).isoformat()


def _parse_iso(s: object) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


def save_runtime_state(
    strategy_state: Dict[str, Dict[str, Any]],
    recent_opens: Deque[Tuple[datetime, str]],
    consec_tp_today: Dict[str, int],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> None:
    """Atomically persist the three intraday maps to ``runtime_state.json``.

    Failure is logged WARNING and swallowed -- a missed write must never
    block the trading loop."""
    path = data_dir / SNAPSHOT_FILENAME
    payload = {
        "version": SCHEMA_VERSION,
        "saved_at": _iso(datetime.now(IST)),
        "strategy_state": {
            str(s): {
                "consec_losses": int(v.get("consec_losses", 0) or 0),
                "daily_pnl": float(v.get("daily_pnl", 0.0) or 0.0),
                "suspended": bool(v.get("suspended", False)),
                "suspended_reason": str(v.get("suspended_reason", "") or ""),
                "trades": int(v.get("trades", 0) or 0),
            }
            for s, v in strategy_state.items()
        },
        "recent_opens": [
            [_iso(ts), str(sym)] for ts, sym in recent_opens
        ],
        "consec_tp_today": {
            str(sym): int(c) for sym, c in consec_tp_today.items()
        },
    }
    try:
        _atomic_write_json(path, payload)
    except Exception as exc:  # noqa: BLE001 - persistence must not raise
        logger.warning(f"[RUNTIME-PERSIST] save failed: {exc!r}")


def load_runtime_state(
    *,
    open_rate_window: timedelta,
    data_dir: Path = DEFAULT_DATA_DIR,
    now: Optional[datetime] = None,
) -> Tuple[
    Dict[str, Dict[str, Any]],
    List[Tuple[datetime, str]],
    Dict[str, int],
]:
    """Restore the three maps, filtering stale entries.

    Returns three empty containers when no snapshot exists or the file
    is unreadable. ``recent_opens`` is returned as a list (caller can
    push into its own deque).

    Filtering:
      * ``strategy_state``: dropped wholesale if snapshot was written
        on a different IST calendar day (per-strategy breaker is a
        daily-reset feature, must not survive a day boundary).
      * ``recent_opens``: entries older than ``open_rate_window``
        dropped (same TTL the runtime enforces).
      * ``consec_tp_today``: dropped if snapshot's IST date != today.
    """
    path = data_dir / SNAPSHOT_FILENAME
    if not path.exists():
        return {}, [], {}

    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Regression #6 (2026-05-18): suspended-strategy state is a
        # SAFETY artefact. Silently dropping to empty meant a strategy
        # that hit its circuit-breaker pre-restart could be re-engaged
        # by the next mutation writing a fresh empty file. Promote to
        # CRITICAL so the operator gets a loud signal that protective
        # state was lost. Daemon still continues -- empty state is a
        # safe (over-permissive) default for runtime state specifically,
        # because the per-strategy breaker will re-arm on the next
        # losing trade. The signal here matters for forensics.
        logger.critical(
            f"[RUNTIME-PERSIST] CORRUPT snapshot at {path}: {exc!r}. "
            "Strategy-suspension / open-rate / TP-streak state has been LOST. "
            "Manual recovery required -- see runbook."
        )
        return {}, [], {}

    # Regression #5 (2026-05-18): enforce SCHEMA_VERSION on load.
    # Same rationale as cooldown_persistence: a forward-version snapshot
    # written by a newer build than the current reader must be REFUSED
    # rather than silently mis-parsed under the old layout.
    snapshot_version_raw = payload.get("version", 1)
    try:
        snapshot_version_int = int(snapshot_version_raw)
    except (TypeError, ValueError):
        logger.critical(
            f"[RUNTIME-PERSIST] UNPARSEABLE schema version {snapshot_version_raw!r} "
            f"at {path}. REFUSING to load."
        )
        return {}, [], {}
    if snapshot_version_int > SCHEMA_VERSION:
        logger.critical(
            f"[RUNTIME-PERSIST] FUTURE schema v{snapshot_version_int} at {path} "
            f"(this build expects v<={SCHEMA_VERSION}). REFUSING to load."
        )
        return {}, [], {}
    if snapshot_version_int < 1:
        logger.critical(
            f"[RUNTIME-PERSIST] INVALID schema v{snapshot_version_int} at {path}. "
            "REFUSING to load."
        )
        return {}, [], {}

    if now is None:
        now = datetime.now(IST)
    saved_at = _parse_iso(payload.get("saved_at"))
    same_day = saved_at is not None and saved_at.date() == now.date()

    strategy_state: Dict[str, Dict[str, Any]] = {}
    if same_day:
        for s, v in (payload.get("strategy_state") or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                strategy_state[str(s)] = {
                    "consec_losses": int(v.get("consec_losses", 0) or 0),
                    "daily_pnl": float(v.get("daily_pnl", 0.0) or 0.0),
                    "suspended": bool(v.get("suspended", False)),
                    "suspended_reason": str(v.get("suspended_reason", "") or ""),
                    "trades": int(v.get("trades", 0) or 0),
                }
            except (TypeError, ValueError):
                continue

    recent_opens: List[Tuple[datetime, str]] = []
    for item in (payload.get("recent_opens") or []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        ts = _parse_iso(item[0])
        if ts is None:
            continue
        if (now - ts) >= open_rate_window:
            continue
        recent_opens.append((ts, str(item[1])))

    consec_tp_today: Dict[str, int] = {}
    if same_day:
        for sym, c in (payload.get("consec_tp_today") or {}).items():
            try:
                consec_tp_today[str(sym)] = int(c)
            except (TypeError, ValueError):
                continue

    if strategy_state or recent_opens or consec_tp_today:
        suspended_names = [s for s, v in strategy_state.items() if v.get("suspended")]
        logger.info(
            f"[RUNTIME-PERSIST] restored "
            f"strategy_state[{len(strategy_state)} strats, "
            f"suspended={suspended_names}] "
            f"recent_opens[{len(recent_opens)}] "
            f"consec_tp_today[{len(consec_tp_today)}]"
        )
    return strategy_state, recent_opens, consec_tp_today


__all__ = [
    "DEFAULT_DATA_DIR",
    "SNAPSHOT_FILENAME",
    "SCHEMA_VERSION",
    "save_runtime_state",
    "load_runtime_state",
]
