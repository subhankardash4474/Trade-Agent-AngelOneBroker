"""Replay any alerts that the agent spooled to disk because the network
was down at send-time (e.g. VPN/DNS hiccup at EOD).

Usage:
    python tools/replay_failed_alerts.py            # drain everything
    python tools/replay_failed_alerts.py --dry-run  # list, don't send
    python tools/replay_failed_alerts.py --max 5    # cap per run

Successfully replayed files are deleted from `logs/failed_alerts/`. Files
that fail again (still no network) stay on disk and are retried next time.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.secrets import apply_env_to_config, load_dotenv  # noqa: E402
from monitoring.alerts import AlertManager  # noqa: E402

SPOOL_DIR = ROOT / "logs" / "failed_alerts"


def _list_spool() -> list[Path]:
    if not SPOOL_DIR.exists():
        return []
    return sorted(SPOOL_DIR.glob("*.json"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list only, do not send")
    ap.add_argument("--max", type=int, default=50, help="max alerts per run")
    args = ap.parse_args()

    files = _list_spool()
    if not files:
        print("[OK] No spooled alerts. Nothing to do.")
        return 0

    print(f"Found {len(files)} spooled alert(s) under {SPOOL_DIR}:")
    for p in files[: args.max]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            subj = data.get("subject", "?")
            reason = data.get("reason", "?")
            spooled = data.get("spooled_at", "?")
            print(f"  {p.name}\n     spooled={spooled}  reason={reason}\n     subject={subj}")
        except Exception as e:
            print(f"  {p.name}  (unreadable: {e})")

    if args.dry_run:
        print("\n[DRY-RUN] no replay attempted.")
        return 0

    load_dotenv(str(ROOT / ".env"))
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = apply_env_to_config(cfg)

    alert = AlertManager(cfg)
    if not alert._email_enabled:
        print("[ERROR] alerts disabled in config; cannot replay.")
        return 1

    result = alert.drain_failed_alerts(max_per_run=args.max)
    print(f"\nDrain result: sent={result['sent']} failed={result['failed']} skipped={result['skipped']}")
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
