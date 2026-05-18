"""Heartbeat email — daily "trader is alive" pulse.

Why this script exists
======================

The 2026-05-19 external verdict flagged *silent operational failures*
as a HIGH-probability freeze-killer (`docs/freeze_contingencies.md` §C1).
The most insidious form is "the daemon is down but I think it's up
because nothing told me otherwise." A daily heartbeat email -- one
explicit message per market day before 09:15 IST -- inverts that:
the *absence* of the email becomes the alarm.

What it sends
=============

A markdown body (rendered to HTML by ``packages.monitoring.alerts``
in this same observability commit) containing:

* Daemon uptime + when it started
* Yesterday's EOD: net PnL + closed-trade count
* Latest audit checkpoint level (GREEN / AMBER / RED) and time
* Open positions count
* Failed-alert spool depth (a non-zero value is itself a flag)
* Disk usage snapshot for the data/logs volume

What it does NOT do
===================

* No trading decisions, no behaviour changes -- pure observability.
* Does NOT crash if the daemon is down. The whole point is to send a
  message saying the daemon is down.
* Does NOT count against the `freeze-v2.1` bypass cap -- it's in the
  observability bucket explicitly allowed by `FREEZE_v2.1.md`
  §"What is NOT frozen".

Wiring
======

* Live VM: ``tools/cloud/install_heartbeat_cron.sh`` installs the cron
  entry ``10 9 * * 1-5 cd <repo> && python tools/send_heartbeat.py``.
* The cron entry runs Mon-Fri at 09:10 IST, before market open.
* If the VM's timezone is UTC, ``install_heartbeat_cron.sh`` rewrites
  to ``40 3 * * 1-5`` (UTC 03:40 == IST 09:10).
* Local dev: ``python tools/send_heartbeat.py --dry-run`` prints the
  composed body without sending. Use to verify formatting.

Exit codes
==========

* 0 -- heartbeat sent (or dry-run produced output)
* 1 -- alerter not configured (no email provider, no credentials)
* 2 -- composed body but send failed (network, SMTP auth, Resend 4xx).
       The body is still printed to stdout so cron mail captures it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Path bootstrap so we can find packages/ from the repo root regardless
# of how cron invoked us.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages"))
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytz  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
HEALTH_FILE = PROJECT_ROOT / "logs" / "health.json"
DB_FILE = PROJECT_ROOT / "data" / "trading_agent.db"
DIAG_DIR = PROJECT_ROOT / "logs" / "diagnostics"
FAILED_ALERTS_DIR = PROJECT_ROOT / "logs" / "failed_alerts"


# ─────────────────────────────────────────────────────────────────────
# Data collectors -- each one returns a dict; failure returns
# {"ok": False, "reason": str} so the body can render "unavailable"
# without crashing the script.
# ─────────────────────────────────────────────────────────────────────
def collect_health() -> dict:
    """Read logs/health.json (written by the daemon's main loop).

    Schema is: {"timestamp": "...", "uptime_seconds": int,
                "open_positions": int, "last_cycle": "..."} (subject to
    daemon-version drift -- this function tolerates missing keys).
    """
    if not HEALTH_FILE.exists():
        return {"ok": False, "reason": "logs/health.json missing -- daemon may be down"}
    try:
        mtime = HEALTH_FILE.stat().st_mtime
        age_seconds = (datetime.now().timestamp() - mtime)
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        data["_file_age_seconds"] = age_seconds
        data["ok"] = True
        return data
    except Exception as e:
        return {"ok": False, "reason": f"unreadable: {type(e).__name__}: {e}"}


def collect_last_eod() -> dict:
    """Find the most recent logs/diagnostics/eod_YYYY-MM-DD.md and read
    the TL;DR portfolio numbers from it. Best-effort; missing fields
    just produce ``unknown`` placeholders.
    """
    if not DIAG_DIR.exists():
        return {"ok": False, "reason": "no logs/diagnostics dir"}
    eod_files = sorted(DIAG_DIR.glob("eod_*.md"))
    if not eod_files:
        return {"ok": False, "reason": "no eod_*.md files yet"}
    latest = eod_files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    out: dict = {"ok": True, "file": latest.name, "pnl": None, "trades": None}
    import re as _re
    # Look for "Total PnL ...: Rs +/-XX" and "Trades: NN" near the top.
    m_pnl = _re.search(r"Total PnL.*?Rs\s*([+-]?[0-9,\.]+)", text)
    if m_pnl:
        try:
            out["pnl"] = float(m_pnl.group(1).replace(",", ""))
        except Exception:
            pass
    m_trd = _re.search(r"Trades:\*?\*?\s*(\d+)", text)
    if m_trd:
        try:
            out["trades"] = int(m_trd.group(1))
        except Exception:
            pass
    return out


def collect_last_audit() -> dict:
    """Latest audit checkpoint -- the markdown file in
    logs/diagnostics/audit_checkpoint_*.md. We pluck the verdict line
    (GREEN / AMBER / RED) without parsing the whole report.
    """
    if not DIAG_DIR.exists():
        return {"ok": False, "reason": "no logs/diagnostics dir"}
    audit_files = sorted(DIAG_DIR.glob("audit_checkpoint_*.md"))
    if not audit_files:
        return {"ok": False, "reason": "no audit_checkpoint_*.md files yet"}
    latest = audit_files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    import re as _re
    # Verdict appears as something like "**Audit verdict: GREEN**" or
    # "## RED:" depending on the version of the script -- match any of
    # the three colour-words appearing in a bold context.
    m = _re.search(r"\*\*[^*]*\b(GREEN|AMBER|RED)\b[^*]*\*\*", text)
    verdict = m.group(1) if m else "UNKNOWN"
    # Time-stamp from filename: audit_checkpoint_YYYYMMDD_HHMMSS.md
    ts_m = _re.search(r"audit_checkpoint_(\d{8}_\d{6})", latest.name)
    ts = ts_m.group(1) if ts_m else "?"
    return {"ok": True, "file": latest.name, "verdict": verdict, "timestamp": ts}


def collect_spool_depth() -> dict:
    """Count failed-alert JSON files awaiting drain."""
    if not FAILED_ALERTS_DIR.exists():
        return {"ok": True, "depth": 0}
    return {"ok": True, "depth": len(list(FAILED_ALERTS_DIR.glob("*.json")))}


def collect_disk() -> dict:
    """Disk usage on the project volume."""
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        pct = (usage.used / usage.total) * 100
        return {
            "ok": True,
            "pct": pct,
            "free_gb": usage.free / (1024**3),
            "total_gb": usage.total / (1024**3),
        }
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────
# Body composer
# ─────────────────────────────────────────────────────────────────────
def _format_uptime(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "unknown"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h >= 24:
        d = h // 24
        h = h % 24
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m"


def compose_body(now_ist: datetime,
                 health: dict,
                 last_eod: dict,
                 audit: dict,
                 spool: dict,
                 disk: dict) -> str:
    """Build the markdown body. Designed to render cleanly through
    ``packages.monitoring.alerts._render_email_html``.
    """
    lines: list[str] = []
    lines.append("## Trading Agent Heartbeat")
    lines.append("")
    lines.append(f"*Sent {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}*")
    lines.append("")

    # Daemon status -- the headline.
    if health.get("ok"):
        age = health.get("_file_age_seconds", 0)
        if age > 300:
            lines.append(f"- **Daemon:** STALE -- health.json is {int(age)}s old (>5 min)")
        else:
            uptime = _format_uptime(health.get("uptime_seconds", 0))
            lines.append(f"- **Daemon:** UP (uptime {uptime})")
        lines.append(f"- **Open positions:** {health.get('open_positions', 'unknown')}")
        if "last_cycle" in health:
            lines.append(f"- **Last loop cycle:** {health['last_cycle']}")
    else:
        lines.append(f"- **Daemon:** UNREACHABLE -- {health.get('reason', 'unknown')}")
        lines.append("- **Action:** see `docs/freeze_contingencies.md` §C1.a")

    lines.append("")

    # Last EOD
    if last_eod.get("ok"):
        pnl_str = f"Rs {last_eod['pnl']:+,.0f}" if last_eod.get("pnl") is not None else "?"
        trd_str = f"{last_eod['trades']} closed trades" if last_eod.get("trades") is not None else "trades=?"
        lines.append(f"- **Last EOD:** {pnl_str}  |  {trd_str}  (`{last_eod['file']}`)")
    else:
        lines.append(f"- **Last EOD:** unavailable -- {last_eod.get('reason', 'unknown')}")

    # Audit
    if audit.get("ok"):
        lines.append(f"- **Latest audit:** {audit['verdict']} at {audit['timestamp']}")
    else:
        lines.append(f"- **Latest audit:** unavailable -- {audit.get('reason', 'unknown')}")

    # Spool
    if spool.get("ok"):
        depth = spool.get("depth", 0)
        flag = " WARN: non-zero -- alert pipeline may be broken" if depth > 0 else ""
        lines.append(f"- **Failed-alert spool depth:** {depth}{flag}")

    # Disk
    if disk.get("ok"):
        flag = " WARN: > 75 %" if disk["pct"] > 75 else ""
        lines.append(f"- **Disk usage:** {disk['pct']:.1f} % "
                     f"(free {disk['free_gb']:.1f} GB / {disk['total_gb']:.0f} GB){flag}")
    else:
        lines.append(f"- **Disk usage:** unavailable -- {disk.get('reason', 'unknown')}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*If you didn't receive this email by 09:25 IST, follow*")
    lines.append("*`docs/freeze_contingencies.md` §C1.b (alert pipeline broken).*")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Send path -- via the existing AlertManager
# ─────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    """Load config.yaml from the project root."""
    import yaml as _yaml
    cfg_path = PROJECT_ROOT / "config.yaml"
    if not cfg_path.exists():
        return {}
    return _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def send_heartbeat(body: str, *, dry_run: bool, force_send: bool = False) -> int:
    """Dispatch through AlertManager. Returns process exit code.

    ``force_send`` bypasses the dedup TTL -- the daily heartbeat is
    *meant* to repeat. Without this, the second day's heartbeat with
    very similar body would be deduped against day 1.
    """
    if dry_run:
        print("=== DRY RUN -- composed body below, NOT sent ===")
        print(body)
        return 0

    config = _load_config()
    try:
        from packages.monitoring.alerts import AlertManager
    except Exception as e:
        print(f"[ERROR] alert manager not importable: {e}", file=sys.stderr)
        print(body)
        return 1

    try:
        mgr = AlertManager(config)
    except Exception as e:
        print(f"[ERROR] alert manager init failed: {e}", file=sys.stderr)
        print(body)
        return 1

    if not getattr(mgr, "_email_enabled", False):
        print("[INFO] email not configured -- printing body to stdout instead", file=sys.stderr)
        print(body)
        return 1

    today = datetime.now(IST).strftime("%Y-%m-%d")
    subject = f"Heartbeat {today}"
    # Force-send by bumping a counter into the body -- the dedup hash
    # would otherwise treat consecutive heartbeats as duplicates.
    # We use the date as part of the dedup key by including it in the
    # subject; the alert manager hashes subject+body. A different date
    # produces a different hash. force_send is kept as a knob for
    # operator-triggered manual sends within the same day.
    if force_send:
        body = body + f"\n\n<!-- nonce {datetime.now().timestamp():.0f} -->"

    try:
        mgr.send_alert(subject, body, level="info")
        return 0
    except Exception as e:
        print(f"[ERROR] send_alert raised: {e}", file=sys.stderr)
        print(body)
        return 2


# ─────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compose and print the body to stdout, do not send.",
    )
    parser.add_argument(
        "--force-send", action="store_true",
        help="Bypass dedup TTL (for operator-triggered manual heartbeats).",
    )
    args = parser.parse_args()

    now = datetime.now(IST)
    body = compose_body(
        now_ist=now,
        health=collect_health(),
        last_eod=collect_last_eod(),
        audit=collect_last_audit(),
        spool=collect_spool_depth(),
        disk=collect_disk(),
    )
    return send_heartbeat(body, dry_run=args.dry_run, force_send=args.force_send)


if __name__ == "__main__":
    raise SystemExit(main())
