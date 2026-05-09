"""
Trading Agent Daemon — Auto-restart wrapper for unattended operation.

Features:
  - Exponential backoff on crash (2s, 4s, 8s, ... up to 5 min)
  - Backoff resets after a long successful run (>10 min)
  - Graceful shutdown via Ctrl+C or SIGTERM
  - Logs crash count and uptime
  - Market-hours-only mode: sleeps outside trading hours instead of busy-looping

Usage:
  python run_daemon.py --paper                    # paper trading daemon
  python run_daemon.py --paper --interval 30      # 30s poll interval
"""

import argparse
import os
import signal
import ssl
import sys
import time
from datetime import datetime

import pytz
import yaml
from loguru import logger

os.environ.setdefault("CURL_CA_BUNDLE", "")
os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
try:
    _default_ctx = ssl.create_default_context()
    _default_ctx.check_hostname = False
    _default_ctx.verify_mode = ssl.CERT_NONE
    ssl._create_default_https_context = lambda: _default_ctx
except Exception:
    pass
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

IST = pytz.timezone("Asia/Kolkata")

MAX_BACKOFF = 300
MIN_STABLE_RUN = 600

_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    logger.info(f"Shutdown signal received ({sig})")
    _shutdown_requested = True


def is_market_window() -> bool:
    """Returns True if within 08:30-16:00 IST on a weekday (pre-market + post-cleanup window)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    hour = now.hour
    return 8 <= hour < 16


def sleep_until_market(config_path: str):
    """Sleep until the next market window opens, checking every 5 minutes."""
    logger.info("Outside market hours — sleeping until next market window...")
    while not is_market_window() and not _shutdown_requested:
        now = datetime.now(IST)
        logger.debug(f"Sleeping... {now.strftime('%H:%M')} IST (next check in 5 min)")
        time.sleep(300)


def run_once(config_path: str, paper: bool, interval: int, dashboard: bool,
             reset_balance: bool = False):
    """Single run of the trading agent. Returns when agent exits or crashes."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if paper:
        config["broker"]["mode"] = "paper"

    smart_api = None
    if config.get("broker", {}).get("mode") != "paper":
        from main import connect_angelone
        smart_api = connect_angelone(config)

    from trading_agent import TradingAgent

    agent = TradingAgent(
        config_path=config_path,
        smart_api=smart_api,
        reset_balance=reset_balance,
    )

    if dashboard:
        from monitoring.dashboard import Dashboard
        import threading
        dash = Dashboard(agent, refresh_interval=config.get("monitoring", {}).get("dashboard_refresh_seconds", 5))
        agent_thread = threading.Thread(target=agent.run, kwargs={"poll_interval": interval}, daemon=True)
        agent_thread.start()
        dash.run()
    else:
        agent.run(poll_interval=interval)


def main():
    parser = argparse.ArgumentParser(description="Trading Agent Daemon (auto-restart wrapper)")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    parser.add_argument("--dashboard", action="store_true", help="Enable CLI dashboard")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    parser.add_argument("--market-hours-only", action="store_true", default=True,
                        help="Only run during market hours (default: True)")
    parser.add_argument(
        "--reset-balance", action="store_true",
        help="Ignore DB equity history and start from config.initial_balance. "
             "Only applied on the FIRST launch of this daemon — subsequent "
             "auto-restarts within the same day continue normally.",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.remove()
    os.makedirs("logs", exist_ok=True)
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add("logs/daemon_{time:YYYY-MM-DD}.log", level="DEBUG",
               rotation="10 MB", retention=5)

    crash_count = 0
    backoff = 2

    logger.info("=" * 60)
    logger.info("TRADING AGENT DAEMON STARTED")
    logger.info(f"  Config: {args.config}")
    logger.info(f"  Mode: {'PAPER' if args.paper else 'LIVE'}")
    logger.info(f"  Poll: {args.interval}s")
    logger.info(f"  Market hours only: {args.market_hours_only}")
    logger.info("=" * 60)

    while not _shutdown_requested:
        if args.market_hours_only and not is_market_window():
            sleep_until_market(args.config)
            if _shutdown_requested:
                break
            backoff = 2
            crash_count = 0

        start_time = time.monotonic()
        try:
            logger.info(f"Starting agent (attempt #{crash_count + 1})...")
            # Only honour --reset-balance on the first launch, so auto-restarts
            # after a crash don't wipe out the in-progress day's balance.
            reset_flag = args.reset_balance and crash_count == 0
            run_once(args.config, args.paper, args.interval, args.dashboard,
                     reset_balance=reset_flag)
            logger.info("Agent exited cleanly")
            break
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user")
            break
        except Exception as e:
            elapsed = time.monotonic() - start_time
            crash_count += 1

            if elapsed > MIN_STABLE_RUN:
                backoff = 2
                logger.info(f"Agent ran for {elapsed:.0f}s before crash — resetting backoff")

            logger.error(f"Agent crashed (#{crash_count}): {e}")
            logger.info(f"Restarting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    logger.info(f"Daemon exiting (total crashes: {crash_count})")


if __name__ == "__main__":
    main()
