"""
Send today's EOD summary email manually.
=========================================
Use when the daemon died before 15:20 IST (the configured EOD send time)
and the auto-summary therefore never fired. Reads the same DB the daemon
was using and sends the same email format the daemon would have sent.

Safe to run any time after market close. It does NOT start trading or
modify any state — only:
  - reads portfolio + risk state from DB
  - builds the EOD report (same code path as `_maybe_send_eod_summary`)
  - calls AlertManager.send_alert with the result

Usage:
    python -m tools.send_eod_summary

Or from the project root:
    python tools/send_eod_summary.py
"""

from __future__ import annotations

import os
import ssl
import sys

# Match the SSL workaround in run_daemon.py so urllib3 / yfinance won't
# fail on the same self-signed cert paths the daemon avoids.
os.environ.setdefault("CURL_CA_BUNDLE", "")
os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
try:
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE
    ssl._create_default_https_context = lambda: _ctx
except Exception:
    pass


def main() -> int:
    import yaml
    from loguru import logger

    # Quieter logging — we only want the EOD report itself plus pass/fail.
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    # Force paper mode — we never want this script to touch a real broker.
    config.setdefault("broker", {})["mode"] = "paper"

    # Local import so the SSL workaround is in place before any network
    # libraries (yfinance, requests) get loaded transitively.
    from trading_agent import TradingAgent

    logger.info("Instantiating TradingAgent (paper mode, will NOT start trading loop)...")
    agent = TradingAgent(
        config_path="config.yaml",
        smart_api=None,
        reset_balance=False,
    )

    # Defensive: force flag false in case __init__ flipped it from a stale
    # daemon flag in some future refactor.
    agent._eod_summary_sent = False

    logger.info("Triggering EOD summary...")
    agent._maybe_send_eod_summary()

    if agent._eod_summary_sent:
        logger.info("[OK] EOD summary fired. Check the configured alert channel "
                    "(email recipient in config.yaml -> robust.alerts.email.recipient).")
        return 0
    else:
        logger.warning("[WARN] EOD summary was NOT fired. Most likely the time "
                       "guard rejected (current time < eod_summary_time in config). "
                       "Lower 'robust.eod_summary_time' or set it to '00:00' to "
                       "force-send and re-run.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
