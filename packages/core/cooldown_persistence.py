"""Lightweight JSON persistence for runtime cooldown / blacklist state.

## Why this exists

`TradingAgent` keeps three runtime maps that gate new-position entries:

  * ``_cooldown_map``           ``symbol -> last losing exit time``
                                Blocks re-entry into a stock for
                                ``reentry_cooldown_minutes`` after a stop-out.
  * ``_stock_loss_today``       ``symbol -> #losses today``
                                Blacklists a stock once losses reach
                                ``max_losses_per_stock_per_day``.
  * ``_rejection_cooldown_map`` ``(symbol, direction) -> last rejection time``
                                Suppresses re-evaluating the same signal for
                                ``rejection_cooldown_minutes`` after a
                                persistent-gate rejection.

All three live only in memory. They reset daily in
``_reset_daily_trackers``. A mid-session container restart wipes them.

## Live evidence

2026-05-15 14:46 IST: code deploy restarted the container. The pre-restart
heartbeat had ``Cooldowns=['BSOFT','AEGISLOG','FEDERALBNK']`` (three
stop-outs today). The post-restart heartbeat showed ``Cooldowns=[]``.
FEDERALBNK then fired a stability=2/2 SELL signal at 14:58 IST that would
have re-opened the same losing trade. Only ``late_entry_cutoff: 14:30``
saved us -- the signal-audit shows the re-entry was rejected with
``late_cutoff:14:30``. A restart at 11 AM would not have had that backstop.

## Design

A single JSON file ``data/cooldowns.json`` holds all three maps with ISO
timestamps in IST. Write is atomic (write to temp + ``os.replace``).
Load filters out stale entries using the same TTLs the runtime enforces
(``reentry_cooldown`` and ``rejection_cooldown``). ``_stock_loss_today``
is only restored if the snapshot was written on the same calendar date in
IST -- otherwise the daily-reset semantics would be violated.

The module is intentionally stdlib-only so it works inside the docker
container without adding deps."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DATA_DIR = Path("data")
SNAPSHOT_FILENAME = "cooldowns.json"
SCHEMA_VERSION = 1


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically via a temp file rename.

    Survives a crash mid-write -- the old file is either fully intact or
    fully replaced. Caller does not need to fsync; ``os.replace`` on
    POSIX is atomic for same-filesystem renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=".cooldowns.", suffix=".tmp"
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


def save_cooldown_state(
    cooldown_map: Dict[str, datetime],
    stock_loss_today: Dict[str, int],
    rejection_cooldown_map: Dict[Tuple[str, str], datetime],
    *,
    cooldown_side_map: Optional[Dict[str, str]] = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> None:
    """Atomically persist the runtime cooldown maps to ``cooldowns.json``.

    ``cooldown_side_map`` is the parallel ``symbol -> entry-side`` tracker
    introduced 2026-05-17 (P2 logic-edges) to let an opposite-side edge
    fire on a symbol that just stopped out the other way. It is keyword
    only and defaults to None for legacy callers (treated as empty).

    Failures are logged at WARNING and swallowed -- a missed write
    must never crash the trading loop. The next mutation will retry."""

    path = data_dir / SNAPSHOT_FILENAME
    payload = {
        "version": SCHEMA_VERSION,
        "saved_at": _iso(datetime.now(IST)),
        "cooldown_map": {sym: _iso(ts) for sym, ts in cooldown_map.items()},
        "stock_loss_today": {sym: int(c) for sym, c in stock_loss_today.items()},
        "rejection_cooldown_map": {
            f"{sym}|{direction}": _iso(ts)
            for (sym, direction), ts in rejection_cooldown_map.items()
        },
        # 2026-05-18 regression fix: persist the side tracker too. The
        # original P2 patch deliberately left this in-memory only with a
        # fall-back to bare-symbol blocking on restart -- that's safe but
        # too strict: it blocks the opposite-side edge for a full
        # reentry-cooldown after every restart. With the snapshot we
        # restore full side-awareness across restarts on the same calendar
        # day. Schema remains v1 because the field is additive (older
        # readers ignore unknown keys, newer readers tolerate a missing
        # key as empty).
        "cooldown_side_map": {
            str(sym): str(side)
            for sym, side in (cooldown_side_map or {}).items()
            if side
        },
    }
    try:
        _atomic_write_json(path, payload)
    except Exception as exc:  # noqa: BLE001 - persistence must not raise
        logger.warning(f"[COOLDOWN-PERSIST] save failed: {exc!r}")


def load_cooldown_state(
    *,
    reentry_cooldown: timedelta,
    rejection_cooldown: timedelta,
    data_dir: Path = DEFAULT_DATA_DIR,
    now: Optional[datetime] = None,
) -> Tuple[
    Dict[str, datetime],
    Dict[str, int],
    Dict[Tuple[str, str], datetime],
    Dict[str, str],
]:
    """Restore the cooldown maps from disk, filtering out stale entries.

    Returns four empty dicts when no snapshot exists or the file is
    unreadable / outright corrupted -- callers must not block startup on
    this path. (Note: 2026-05-18 the return tuple grew a fourth slot for
    ``cooldown_side_map``; legacy callers that unpack only three values
    will get a TypeError -- update them.)

    Schema-version handling (Regression #5, 2026-05-18): the snapshot's
    ``version`` field is now compared against the current SCHEMA_VERSION.
    A future-version snapshot (newer writer than reader -- can happen on
    a rollback) is REFUSED and a CRITICAL alert is logged so the operator
    is forced to acknowledge the format skew rather than silently
    mis-parsing the layout. A missing version field is tolerated (treated
    as v1) for back-compat with snapshots written before this change.

    Filtering rules:
      - ``cooldown_map``: drop entries older than ``reentry_cooldown``
        (the same TTL the runtime applies in ``_is_in_cooldown``).
      - ``stock_loss_today``: drop the whole map if the snapshot was
        written on a different IST calendar date (the runtime resets
        this map daily, so a yesterday-snapshot must not survive).
      - ``rejection_cooldown_map``: drop entries older than
        ``rejection_cooldown``.
      - ``cooldown_side_map``: drop entries whose symbol no longer has a
        live cooldown_map entry after filtering."""

    path = data_dir / SNAPSHOT_FILENAME
    if not path.exists():
        return {}, {}, {}, {}

    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Regression #6 (2026-05-18): corrupt snapshot of SAFETY state
        # (cooldowns / blacklist) silently dropped to empty maps and the
        # next mutation cheerfully overwrote it with a fresh-start file.
        # That erased today's protective state -- a stock that should be
        # blacklisted (3 losses) would be tradeable again. Promote to
        # CRITICAL with a distinct tag so the operator can flag the
        # daemon as untrusted; callers still receive empty maps so the
        # daemon does not crash on startup. Operator runbook: stop the
        # container, restore data/cooldowns.json from the most recent
        # known-good snapshot (data/.cooldowns.*.tmp leftover or a
        # backup), and restart.
        logger.critical(
            f"[COOLDOWN-PERSIST] CORRUPT snapshot at {path}: {exc!r}. "
            "Cooldown / blacklist / rejection state has been LOST. "
            "Manual recovery required -- see runbook."
        )
        return {}, {}, {}, {}

    snapshot_version_raw = payload.get("version", 1)
    try:
        snapshot_version_int = int(snapshot_version_raw)
    except (TypeError, ValueError):
        logger.critical(
            f"[COOLDOWN-PERSIST] UNPARSEABLE schema version {snapshot_version_raw!r} "
            f"at {path}. REFUSING to load (treating as untrusted/corrupt format)."
        )
        return {}, {}, {}, {}
    if snapshot_version_int > SCHEMA_VERSION:
        logger.critical(
            f"[COOLDOWN-PERSIST] FUTURE schema v{snapshot_version_int} at {path} "
            f"(this build expects v<={SCHEMA_VERSION}). REFUSING to load. "
            "If you just rolled back, restore an older snapshot or wipe the file."
        )
        return {}, {}, {}, {}
    if snapshot_version_int < 1:
        logger.critical(
            f"[COOLDOWN-PERSIST] INVALID schema v{snapshot_version_int} at {path}. "
            "REFUSING to load."
        )
        return {}, {}, {}, {}

    if now is None:
        now = datetime.now(IST)
    saved_at = _parse_iso(payload.get("saved_at"))

    cooldown_map: Dict[str, datetime] = {}
    for sym, ts_raw in (payload.get("cooldown_map") or {}).items():
        ts = _parse_iso(ts_raw)
        if ts is None:
            continue
        if (now - ts) < reentry_cooldown:
            cooldown_map[str(sym)] = ts

    stock_loss_today: Dict[str, int] = {}
    if saved_at is not None and saved_at.date() == now.date():
        for sym, count in (payload.get("stock_loss_today") or {}).items():
            try:
                stock_loss_today[str(sym)] = int(count)
            except (TypeError, ValueError):
                continue

    rejection_cooldown_map: Dict[Tuple[str, str], datetime] = {}
    for key, ts_raw in (payload.get("rejection_cooldown_map") or {}).items():
        ts = _parse_iso(ts_raw)
        if ts is None or not isinstance(key, str) or "|" not in key:
            continue
        sym, direction = key.split("|", 1)
        if (now - ts) < rejection_cooldown:
            rejection_cooldown_map[(sym, direction)] = ts

    cooldown_side_map: Dict[str, str] = {}
    for sym, side_raw in (payload.get("cooldown_side_map") or {}).items():
        if not isinstance(side_raw, str) or not side_raw:
            continue
        sym_s = str(sym)
        # Only keep side entries that still have a live cooldown_map
        # entry after staleness filtering -- otherwise the side would
        # outlive the cooldown that gave it meaning.
        if sym_s in cooldown_map:
            cooldown_side_map[sym_s] = side_raw

    if cooldown_map or stock_loss_today or rejection_cooldown_map or cooldown_side_map:
        logger.info(
            "[COOLDOWN-PERSIST] restored "
            f"cooldowns={list(cooldown_map.keys())} "
            f"side_map={cooldown_side_map} "
            f"losses_today={dict(stock_loss_today)} "
            f"rejections={len(rejection_cooldown_map)}"
        )
    return cooldown_map, stock_loss_today, rejection_cooldown_map, cooldown_side_map


__all__ = [
    "DEFAULT_DATA_DIR",
    "SNAPSHOT_FILENAME",
    "SCHEMA_VERSION",
    "save_cooldown_state",
    "load_cooldown_state",
]
