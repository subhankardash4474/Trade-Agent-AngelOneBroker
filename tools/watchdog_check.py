#!/usr/bin/env python3
"""
Daemon liveness watchdog -- intra-day silent-hang detector.

Why this exists
---------------
On 2026-05-22 the trader daemon went silent at 12:23 IST and stayed
silent for 11 hours -- no exception, no exit, just a frozen process.
The 09:10 IST heartbeat email runs ONCE PER DAY and was therefore
useless: by the time we noticed, the entire afternoon's session was
gone.

This script is the missing piece: a 5-minute cron that checks the
freshness of ``logs/health.json`` (which the daemon refreshes on
every heartbeat cycle) and fires an alert when it's stale.

Design constraints
------------------
* Frozen-file safe: does NOT modify the trading daemon. It reads
  ``logs/health.json`` and that file alone.
* Idempotent: writes a small state file
  ``logs/watchdog_state.json`` so we don't re-alert every 5 minutes
  while the daemon is hung. We re-alert only on transitions
  (HEALTHY -> STALE, STALE -> HEALTHY) and on an "escalation" boundary
  every N hours of continuous staleness.
* Quiet on weekends / off-hours: only alerts during the trading
  window (09:00 -- 16:00 IST, Mon-Fri). This avoids a 1500-strong
  inbox over a 3-day weekend if the daemon is intentionally stopped.
* Failure-tolerant: any exception this script raises must NOT crash
  cron's invoking shell -- the script always exits 0 on operational
  errors, only nonzero on unrecoverable config issues.

Cron line installed by tools/cloud/install_heartbeat_cron.sh:

    */5 * * * * cd /opt/trading-agent && \\
        sudo docker exec trader python tools/watchdog_check.py \\
        >> logs/watchdog_cron.log 2>&1
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional

# Import path nudge so we work both as `python tools/watchdog_check.py`
# and as `python -m tools.watchdog_check`.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")

# Files we read / write.
HEALTH_FILE = _REPO / "logs" / "health.json"
STATE_FILE = _REPO / "logs" / "watchdog_state.json"

# Operational thresholds (tuned 2026-05-25 against observed cycle
# cadence -- heartbeat fires every ~60s, so anything older than 5x
# that is unambiguous evidence of a hang).
STALE_SECONDS = 300            # 5 minutes -- threshold for "STALE"
ESCALATION_HOURS = 1           # Re-alert every hour while still stale
TRADING_WINDOW = (dt_time(9, 0), dt_time(16, 0))   # IST
TRADING_DAYS = (0, 1, 2, 3, 4)  # Mon-Fri


# ─────────────────────────────────────────────────────────────────────
# Config loader (re-uses the same env-overlay mechanism as the daemon)
# ─────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    """Load config.yaml + apply .env overrides (Resend key, etc.)."""
    import yaml
    from packages.core.secrets import apply_env_to_config, load_dotenv

    load_dotenv()
    cfg_path = _REPO / "config.yaml"
    if not cfg_path.exists():
        return {}
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    apply_env_to_config(cfg)
    return cfg


# ─────────────────────────────────────────────────────────────────────
# State file: tracks the last alert we emitted so we don't spam.
# ─────────────────────────────────────────────────────────────────────
def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_status": "UNKNOWN", "last_alert_unix": 0,
                "first_stale_unix": 0}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_status": "UNKNOWN", "last_alert_unix": 0,
                "first_stale_unix": 0}


def _write_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.warning(f"watchdog: couldn't persist state: {e}")


# ─────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────
def _read_health() -> tuple[Optional[float], Optional[dict]]:
    """Return (file_age_seconds, parsed_health_dict). Either may be None."""
    if not HEALTH_FILE.exists():
        return None, None
    try:
        mtime = HEALTH_FILE.stat().st_mtime
        age = datetime.now().timestamp() - mtime
        try:
            data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = None
        return age, data
    except Exception:
        return None, None


# ─────────────────────────────────────────────────────────────────────
# Trading window check -- so we don't alert at 03:00 IST Sunday.
# ─────────────────────────────────────────────────────────────────────
def _is_trading_window(now: datetime) -> bool:
    """True iff `now` is Mon-Fri between 09:00 and 16:00 IST."""
    if now.weekday() not in TRADING_DAYS:
        return False
    t = now.timetz().replace(tzinfo=None)
    return TRADING_WINDOW[0] <= t <= TRADING_WINDOW[1]


# ─────────────────────────────────────────────────────────────────────
# Alert composition + dispatch
# ─────────────────────────────────────────────────────────────────────
def _compose_alert(status: str, age_seconds: Optional[float],
                   health: Optional[dict], state: dict,
                   now: datetime) -> tuple[str, str]:
    """Return (subject, body) for the alert email."""
    age_min = (age_seconds or 0) / 60.0
    first_stale_unix = state.get("first_stale_unix", 0)
    if first_stale_unix:
        stuck_min = (now.timestamp() - first_stale_unix) / 60.0
    else:
        stuck_min = 0

    if status == "STALE":
        subject = f"[WATCHDOG] Daemon SILENT (health.json {age_min:.0f}m old)"
        lines = [
            "## Watchdog Alert: Trader Daemon SILENT",
            "",
            f"*Triggered {now.strftime('%Y-%m-%d %H:%M:%S IST')}*",
            "",
            f"- **health.json age:** {age_min:.1f} minutes (>{STALE_SECONDS}s threshold)",
            f"- **continuous stale duration:** {stuck_min:.1f} minutes",
        ]
        if health:
            lines.append(f"- **last reported cycle:** {health.get('cycle_count', 'unknown')}")
            lines.append(f"- **last reported ts:** {health.get('ts', 'unknown')}")
            lines.append(f"- **last reported pid:** {health.get('pid', 'unknown')}")
            lines.append(f"- **last reported open positions:** {health.get('open_position_count', 'unknown')}")
        lines.extend([
            "",
            "**Likely failure modes (ranked by 2026-05-22 incident frequency):**",
            "1. Broker session token expired -> blocking refresh call",
            "2. AngelOne API hung TCP socket -> infinite read",
            "3. Python GIL deadlock on background thread",
            "",
            "**Operator actions:**",
            "- SSH to VM and `sudo docker logs --tail 200 trader`",
            "- If logs frozen: `sudo docker compose restart trader`",
            "- Check open_positions in DB: any stuck positions need manual exit",
            "- See `docs/freeze_contingencies.md` -SS-C2 (silent-hang playbook)",
        ])
    elif status == "RECOVERED":
        subject = f"[WATCHDOG] Daemon recovered (was silent {stuck_min:.0f}m)"
        lines = [
            "## Watchdog: Daemon RECOVERED",
            "",
            f"*Recovered {now.strftime('%Y-%m-%d %H:%M:%S IST')}*",
            "",
            f"- **silent for:** {stuck_min:.1f} minutes",
            f"- **current health.json age:** {age_min:.1f}s",
        ]
        if health:
            lines.append(f"- **current cycle:** {health.get('cycle_count', 'unknown')}")
            lines.append(f"- **current pid:** {health.get('pid', 'unknown')}")
        lines.extend([
            "",
            "Verify open positions are still aligned with strategy state.",
        ])
    else:
        subject = "[WATCHDOG] Unknown state"
        lines = [f"unexpected status={status}"]

    return subject, "\n".join(lines)


def _send_alert(cfg: dict, subject: str, body: str) -> bool:
    """Best-effort send via AlertManager. Failure is logged but doesn't
    crash the watchdog -- spool will catch it on next AlertManager call."""
    try:
        from packages.monitoring.alerts import AlertManager
        am = AlertManager(cfg)
        return am.send_alert(subject, body, level="ERROR")
    except Exception as e:
        logger.error(f"watchdog: alert dispatch failed: {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────
# Main control flow
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    now = datetime.now(IST)
    age, health = _read_health()
    state = _read_state()
    now_unix = int(now.timestamp())

    # Determine current status.
    if age is None:
        # health.json missing entirely -- treat as STALE if in trading window.
        current_status = "STALE"
    elif age > STALE_SECONDS:
        current_status = "STALE"
    else:
        current_status = "HEALTHY"

    last_status = state.get("last_status", "UNKNOWN")
    last_alert_unix = state.get("last_alert_unix", 0)
    first_stale_unix = state.get("first_stale_unix", 0)

    in_window = _is_trading_window(now)

    # State machine for alert decisions.
    should_alert = False
    alert_status = current_status

    if current_status == "STALE":
        if first_stale_unix == 0:
            first_stale_unix = now_unix
        # Alert on TRANSITION to stale, but only if we're in the trading
        # window (avoids weekend / overnight noise).
        if last_status != "STALE":
            should_alert = in_window
            alert_status = "STALE"
        else:
            # Re-alert every ESCALATION_HOURS hours of continuous stale.
            hours_since_alert = (now_unix - last_alert_unix) / 3600.0
            if in_window and hours_since_alert >= ESCALATION_HOURS:
                should_alert = True
                alert_status = "STALE"
    else:  # HEALTHY
        # Alert on RECOVERY -- only if we previously alerted on the
        # stale state (so we don't fire "recovered" emails on first run).
        if last_status == "STALE" and last_alert_unix > 0:
            should_alert = True
            alert_status = "RECOVERED"
        first_stale_unix = 0

    # Always log a one-liner to logs/watchdog_cron.log so the operator
    # can confirm the watchdog is alive even when no alert fires.
    age_str = f"{age:.0f}s" if age is not None else "missing"
    logger.info(
        f"watchdog_check: status={current_status} age={age_str} "
        f"in_window={in_window} should_alert={should_alert}"
    )

    if should_alert:
        # Defensive: every operation here is wrapped because the watchdog
        # is the LAST observability layer. If it crashes via cron, cron
        # mails root@ with the stderr trace and the operator never sees
        # the actual silent-hang (the thing we were watching for). We'd
        # rather log an internal error and keep state consistent than
        # propagate any exception out of main().
        try:
            try:
                cfg = _load_config()
            except Exception as e:
                logger.error(f"watchdog: config load failed: {e}")
                cfg = {}
            subject, body = _compose_alert(alert_status, age, health, state, now)
            if _send_alert(cfg, subject, body):
                logger.info(f"watchdog: alerted ({alert_status})")
                last_alert_unix = now_unix
            else:
                logger.error(f"watchdog: alert FAILED ({alert_status})")
        except Exception as e:
            logger.error(
                f"watchdog: unexpected error during alert dispatch: "
                f"{type(e).__name__}: {e}"
            )

    # Persist state so the next cron tick has context.
    new_state = {
        "last_status": current_status,
        "last_alert_unix": last_alert_unix,
        "first_stale_unix": first_stale_unix,
        "last_check_iso": now.isoformat(timespec="seconds"),
    }
    _write_state(new_state)

    # Always exit 0 -- a non-zero exit would cause cron to email root,
    # which is noise we don't want.
    return 0


if __name__ == "__main__":
    sys.exit(main())
