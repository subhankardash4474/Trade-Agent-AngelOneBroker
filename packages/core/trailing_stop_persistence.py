"""P1 #15 (2026-05-17) -- LIVE-MODE SAFETY: persist TrailingStop state.

## Why this exists

`TrailingStop` holds 8 live-mode runtime fields that the risk manager
mutates on every tick / cycle:

  * ``highest_since_entry`` / ``lowest_since_entry`` -- water marks
  * ``current_sl``                                    -- trailed SL value
  * ``trailing_active``                               -- 1R-favorable gate
  * ``peak_unrealized_r``                             -- MFE in R-units
  * ``peak_giveback_armed``                           -- peak-giveback gate
  * ``last_unrealized_r``                             -- last tick R
  * ``breakeven_armed``                               -- 0.5R-favorable gate

All eight reset to zero / False on `__init__`. After a mid-session daemon
restart the portfolio is rehydrated from the SQLite ledger and the SL
registry is reconciled (P0 #4), but the trailing-stop in-memory state is
lost. A position that was 1 tick from breakeven-armed pre-restart goes
back to its initial-SL distance post-restart -- erasing any progress the
classic trail had locked in.

This module mirrors ``cooldown_persistence`` exactly:
  * Atomic JSON write (temp + ``os.replace``).
  * Fail-soft load with validation (entry_price + side must still match
    the live position; symbol must still be open in portfolio).
  * Stdlib-only.

The snapshot lives at ``data/trailing_stops.json`` (gitignored).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DATA_DIR = Path("data")
SNAPSHOT_FILENAME = "trailing_stops.json"
SCHEMA_VERSION = 1


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically via a temp file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=".trailing_stops.", suffix=".tmp"
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


def _serialize_trailing_stop(ts: Any) -> dict:
    """Snapshot the dynamic fields of a ``TrailingStop`` instance.

    Static fields (the thresholds, ``entry_price``, ``side``,
    ``_initial_risk``) are also captured for validation on load -- we
    only restore state onto a TrailingStop whose static params still match.

    2026-05-18 regression fix: also snapshot ``trail_activation_rr`` and
    ``trail_step_pct``. These are normally constants set from config, but
    trend-continuation entries override them post-init (see
    ``trading_agent._execute_signal`` -- the override sets activation=0.5
    and step=0.6 to ride a known winner harder). Without persisting the
    override, a restart resurrected the TrailingStop with the default
    activation=1.0 / step=0.3, silently un-tightening the trail and
    giving back more MFE than the operator chose.
    """
    return {
        "entry_price":          float(ts.entry_price),
        "side":                 str(ts.side),
        "initial_risk":         float(ts._initial_risk),
        "current_sl":           float(ts.current_sl),
        "highest_since_entry":  float(ts.highest_since_entry),
        "lowest_since_entry":   float(ts.lowest_since_entry),
        "trailing_active":      bool(ts.trailing_active),
        "peak_unrealized_r":    float(ts.peak_unrealized_r),
        "peak_giveback_armed":  bool(ts.peak_giveback_armed),
        "last_unrealized_r":    float(ts.last_unrealized_r),
        "breakeven_armed":      bool(ts.breakeven_armed),
        "trail_activation_rr":  float(ts.trail_activation_rr),
        "trail_step_pct":       float(ts.trail_step_pct),
    }


def save_trailing_states(
    trailing_stops_by_symbol: Dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> None:
    """Atomically persist all open trailing-stops.

    Failure is logged WARNING and swallowed -- a missed write can never
    crash the trading loop.
    """
    path = data_dir / SNAPSHOT_FILENAME
    payload = {
        "version": SCHEMA_VERSION,
        "saved_at": datetime.now(IST).isoformat(),
        "trailing_stops": {
            sym: _serialize_trailing_stop(ts)
            for sym, ts in trailing_stops_by_symbol.items()
        },
    }
    try:
        _atomic_write_json(path, payload)
    except Exception as exc:  # noqa: BLE001 - persistence must not raise
        logger.warning(f"[TRAIL-PERSIST] save failed: {exc!r}")


def load_trailing_states(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Dict[str, dict]:
    """Read the on-disk snapshot. Returns ``{symbol: snapshot_dict}``.

    Empty dict if the file is absent or unreadable -- callers must not
    block startup on this path.
    """
    path = data_dir / SNAPSHOT_FILENAME
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Regression #6 (2026-05-18): a corrupt trailing-stop snapshot
        # used to silently drop to empty; the next mutation then
        # overwrote it with a fresh empty file. Net effect: positions
        # restarted with default-distance SLs even when the trail had
        # locked in significant gains pre-restart. Promote to CRITICAL.
        # The daemon still continues (empty dict) so the position is at
        # least protected by its initial SL on the broker side.
        logger.critical(
            f"[TRAIL-PERSIST] CORRUPT snapshot at {path}: {exc!r}. "
            "Trailing-stop progression state has been LOST. "
            "Open positions will fall back to their initial-SL distance."
        )
        return {}

    # Regression #5 (2026-05-18): enforce SCHEMA_VERSION on load.
    snapshot_version_raw = payload.get("version", 1)
    try:
        snapshot_version_int = int(snapshot_version_raw)
    except (TypeError, ValueError):
        logger.critical(
            f"[TRAIL-PERSIST] UNPARSEABLE schema version {snapshot_version_raw!r} "
            f"at {path}. REFUSING to load."
        )
        return {}
    if snapshot_version_int > SCHEMA_VERSION:
        logger.critical(
            f"[TRAIL-PERSIST] FUTURE schema v{snapshot_version_int} at {path} "
            f"(this build expects v<={SCHEMA_VERSION}). REFUSING to load."
        )
        return {}
    if snapshot_version_int < 1:
        logger.critical(
            f"[TRAIL-PERSIST] INVALID schema v{snapshot_version_int} at {path}. "
            "REFUSING to load."
        )
        return {}

    out: Dict[str, dict] = {}
    for sym, snap in (payload.get("trailing_stops") or {}).items():
        if not isinstance(snap, dict):
            continue
        out[str(sym)] = snap
    if out:
        logger.info(f"[TRAIL-PERSIST] loaded snapshot for {sorted(out.keys())}")
    return out


def restore_trailing_state(
    ts: Any,
    snapshot: dict,
    *,
    entry_price_tol_pct: float = 0.05,
) -> bool:
    """Overlay snapshot fields onto a freshly-constructed ``TrailingStop``.

    Validation:
      * ``side`` must match exactly.
      * ``entry_price`` must match within ``entry_price_tol_pct`` percent
        (small float jitter from DB round-trip is OK).
      * ``initial_risk`` (computed from entry_price - initial_sl) must be
        non-zero in both snapshot and ts -- mismatched initial-SL means
        the trade was reopened with a different stop, ignore the snapshot.

    Returns True if state was applied, False if validation failed.
    """
    try:
        snap_side = snapshot.get("side")
        snap_entry = float(snapshot.get("entry_price", 0.0))
        snap_init_risk = float(snapshot.get("initial_risk", 0.0))
    except (TypeError, ValueError):
        return False

    if snap_side != ts.side:
        logger.warning(
            f"[TRAIL-PERSIST] {ts.symbol or '?'}: side mismatch "
            f"(snap={snap_side}, ts={ts.side}); ignoring snapshot."
        )
        return False
    if ts.entry_price == 0:
        return False
    drift = abs(snap_entry - ts.entry_price) / abs(ts.entry_price) * 100.0
    if drift > entry_price_tol_pct:
        logger.warning(
            f"[TRAIL-PERSIST] {ts.symbol or '?'}: entry_price drift "
            f"{drift:.3f}% > tol {entry_price_tol_pct:.3f}% "
            f"(snap={snap_entry}, ts={ts.entry_price}); ignoring snapshot."
        )
        return False
    if snap_init_risk <= 0 or ts._initial_risk <= 0:
        return False

    # All checks passed -- overlay the dynamic state.
    try:
        ts.current_sl          = float(snapshot["current_sl"])
        ts.highest_since_entry = float(snapshot["highest_since_entry"])
        ts.lowest_since_entry  = float(snapshot["lowest_since_entry"])
        ts.trailing_active     = bool(snapshot["trailing_active"])
        ts.peak_unrealized_r   = float(snapshot["peak_unrealized_r"])
        ts.peak_giveback_armed = bool(snapshot["peak_giveback_armed"])
        ts.last_unrealized_r   = float(snapshot["last_unrealized_r"])
        ts.breakeven_armed     = bool(snapshot["breakeven_armed"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            f"[TRAIL-PERSIST] {ts.symbol or '?'}: malformed snapshot "
            f"({exc!r}); ignoring."
        )
        return False

    # 2026-05-18 regression fix: restore the trend-continuation overrides
    # if the snapshot carried them. Optional fields; default behavior is
    # to keep whatever the freshly-constructed TrailingStop already has
    # (the config defaults) so legacy snapshots from before this regression
    # patch continue to work unchanged.
    if "trail_activation_rr" in snapshot:
        try:
            ts.trail_activation_rr = float(snapshot["trail_activation_rr"])
        except (TypeError, ValueError):
            pass
    if "trail_step_pct" in snapshot:
        try:
            ts.trail_step_pct = float(snapshot["trail_step_pct"])
        except (TypeError, ValueError):
            pass

    logger.info(
        f"[TRAIL-PERSIST] {ts.symbol or '?'}: restored state "
        f"sl={ts.current_sl:.2f} peak_R={ts.peak_unrealized_r:.2f} "
        f"breakeven_armed={ts.breakeven_armed} "
        f"peak_giveback_armed={ts.peak_giveback_armed} "
        f"trailing_active={ts.trailing_active}"
    )
    return True


__all__ = [
    "DEFAULT_DATA_DIR",
    "SNAPSHOT_FILENAME",
    "SCHEMA_VERSION",
    "save_trailing_states",
    "load_trailing_states",
    "restore_trailing_state",
]
