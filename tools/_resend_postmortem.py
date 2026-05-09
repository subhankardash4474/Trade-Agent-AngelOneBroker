"""One-shot: resend today's post-mortem email with the freshly regenerated
bar-aware report (the auto-fired 15:21 email had [NO-BARS] because yfinance
5-min bars weren't available yet at market close).
"""
from __future__ import annotations

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


def main() -> int:
    day_iso = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).strftime("%Y-%m-%d")
    report_path = ROOT / "logs" / "postmortem" / f"{day_iso}.md"

    if not report_path.exists():
        print(f"[ERROR] No post-mortem report found at {report_path}")
        return 1

    body = report_path.read_text(encoding="utf-8")
    if len(body) > 50_000:
        body = body[:50_000] + "\n\n... (truncated)"

    load_dotenv(str(ROOT / ".env"))
    cfg_path = ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = apply_env_to_config(cfg)

    alert = AlertManager(cfg)
    title = f"Trade Post-Mortem {day_iso} (Updated with bar data)"
    alert.send_alert(title, body, level="info")
    print(f"[OK] Resent {title} ({len(body)} chars) to configured channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
