"""One-shot: resend today's EOD summary that the daemon tried to send at 15:20
but failed at the network layer (DNS via VPN). Body is reconstructed from the
log entry the daemon already wrote.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from datetime import datetime
import pytz

from monitoring.alerts import AlertManager
from core.secrets import apply_env_to_config, load_dotenv

IST = pytz.timezone("Asia/Kolkata")


def extract_eod_block(log_path: Path) -> str | None:
    """Read the daemon log and pull the [EOD SUMMARY] block."""
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    # The log line is: "<ts> | INFO | [EOD SUMMARY]\n<body>\n<next ts> | ..."
    # We want to capture from "EOD Report" through the strategy-mix block,
    # stopping at the next loguru-prefixed timestamp line.
    m = re.search(
        r"EOD Report\s+\d{4}-\d{2}-\d{2}.*?(?=\n\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        text,
        re.DOTALL,
    )
    if not m:
        return None
    return m.group(0).strip()


def main() -> int:
    day_iso = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).strftime("%Y-%m-%d")
    log_path = ROOT / "logs" / f"trading_agent_{day_iso}.log"
    body = extract_eod_block(log_path)
    if not body:
        print(f"[ERROR] No [EOD SUMMARY] block found in {log_path}")
        return 1

    load_dotenv(str(ROOT / ".env"))
    cfg_path = ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = apply_env_to_config(cfg)

    alert = AlertManager(cfg)
    title = f"EOD Summary {day_iso} (Resend - VPN recovery)"
    alert.send_alert(title, body, level="info")
    print(f"[OK] Resent {title} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
