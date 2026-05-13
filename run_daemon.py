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

# Phase 1 sys.path bootstrap -- packages/ is the new home for core, strategies, etc.
import sys as _sys
from pathlib import Path as _Path
_pkg = _Path(__file__).resolve().parent / 'packages'
if str(_pkg) not in _sys.path:
    _sys.path.insert(0, str(_pkg))


import argparse
import json
import os
import signal
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz
import yaml
from loguru import logger

# Corporate proxy / self-signed-cert workaround.
#
# DEFAULT = bypass ENABLED (matches historical behaviour, keeps corp-network
# laptops working out of the box). To OPT INTO secure SSL verification --
# strongly recommended for cloud VMs (OCI / AWS / DigitalOcean / any public
# host) -- set TRADER_DISABLE_SSL_VERIFY=false in the deployment .env.
#
# Silently trusting any cert on a public box is a real security regression;
# the cloud .env.production.example flips this to "false" explicitly.
_ssl_bypass = os.environ.get("TRADER_DISABLE_SSL_VERIFY", "true").lower()
if _ssl_bypass in ("1", "true", "yes"):
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


def _write_idle_heartbeat(config_path: str) -> None:
    """Refresh `logs/health.json` while the daemon is idling off-market hours.

    Without this, `TradingAgent._write_health_json` (the in-cycle heartbeat
    writer) never runs during the overnight/pre-market sleep window, so the
    Docker healthcheck reads a missing-or-stale file and flips the container
    to `unhealthy`. We emit a minimal payload with `state=idle_off_hours`
    that `tools/health_check.py` will see as fresh (recent `ts_unix`).
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        config = {}

    log_dir = Path(config.get("logging", {}).get("log_dir", "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    now = datetime.now(IST)
    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "ts_unix": int(now.timestamp()),
        "pid": os.getpid(),
        "mode": (config.get("broker", {}) or {}).get("mode", "paper"),
        "state": "idle_off_hours",
        "cycle_count": 0,
        "running": False,
        "open_positions": [],
        "open_position_count": 0,
        "cash": float(config.get("initial_capital", 0.0)),
        "daily_pnl": 0.0,
        "daily_trades": 0,
    }

    tmp = log_dir / "health.json.tmp"
    final = log_dir / "health.json"
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(final)
    except Exception as e:
        logger.warning(f"Idle heartbeat write failed: {e}")


def _emergency_stop_path_from_config(config_path: str) -> str:
    """Resolve the emergency-stop file path *without* instantiating a full
    ``TradingAgent`` (which would require a live broker session). Used by
    the off-hours sleep loop so the kill switch works even when the
    daemon is idling outside market hours.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    ops = cfg.get("operations") or {}
    log_dir = (cfg.get("logging") or {}).get("log_dir", "logs")
    return ops.get("emergency_stop_path") or os.path.join(log_dir, "STOP")


def sleep_until_market(config_path: str):
    """Sleep until the next market window opens.

    Wakes every minute so the file-based emergency stop is honoured even
    overnight / on weekends. Without this check, ``touch logs/STOP``
    while the daemon was off-hours sleeping would sit unobserved for up
    to 5 minutes and -- more importantly -- never trigger
    ``TradingAgent._check_emergency_stop`` because the agent's run loop
    is not yet active. Detecting it here lets the wrapper exit cleanly
    instead.
    """
    logger.info("Outside market hours — sleeping until next market window...")
    _write_idle_heartbeat(config_path)
    stop_path = _emergency_stop_path_from_config(config_path)
    global _shutdown_requested
    while not is_market_window() and not _shutdown_requested:
        # 60s instead of the legacy 300s. The previous interval was set
        # to save CPU on a free-tier micro-VM, but the actual cost of
        # waking once a minute to stat() a single file is negligible
        # (~microseconds) and gives operators a kill switch that
        # responds in under a minute instead of "maybe in 5 minutes,
        # maybe never if the daemon stays idle".
        try:
            if os.path.exists(stop_path):
                logger.critical(
                    f"[EMERGENCY-STOP] Stop file detected at {stop_path} "
                    f"during off-hours sleep — exiting daemon wrapper."
                )
                _shutdown_requested = True
                break
        except OSError as e:
            # Filesystem flakes shouldn't crash us — same posture as
            # TradingAgent._check_emergency_stop.
            logger.debug(f"emergency_stop FS check failed (ignored): {e}")
        now = datetime.now(IST)
        logger.debug(f"Sleeping... {now.strftime('%H:%M')} IST (next check in 60s)")
        time.sleep(60)
        _write_idle_heartbeat(config_path)


def run_once(config_path: str, paper: bool, interval: int, dashboard: bool,
             reset_balance: bool = False,
             max_loss_rs: float | None = None,
             single_shot: bool = False):
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
        max_loss_rs=max_loss_rs,
        single_shot=single_shot,
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
    parser.add_argument(
        "--max-loss-rs", type=float, default=None, metavar="N",
        help="Hard rupee floor on daily realised P&L. When the day's realised "
             "P&L drops to <= -N, the risk manager refuses all new entries "
             "(existing positions still receive SL/TP management). Independent "
             "from `risk.daily_loss_limit_pct` in config -- whichever is "
             "tighter fires first. Designed for Stage 3 live basket runs "
             "where the percentage limit on a Rs 1L config is too lax "
             "(e.g. `--max-loss-rs 500` for a Rs 5k experiment).",
    )
    parser.add_argument(
        "--single-shot", action="store_true",
        help="Stage 3 safety: once any symbol has completed a full round-trip "
             "(entered + exited) within the day, refuse re-entry on that same "
             "symbol until tomorrow. Caps maximum fills per symbol per day "
             "at 2 (one entry, one exit). Existing position management is "
             "unaffected.",
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
    if args.max_loss_rs is not None:
        logger.warning(f"  [E2E] --max-loss-rs: Rs {args.max_loss_rs:,.2f} (hard rupee floor)")
    if args.single_shot:
        logger.warning("  [E2E] --single-shot: one round-trip per symbol per day")
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
                     reset_balance=reset_flag,
                     max_loss_rs=args.max_loss_rs,
                     single_shot=args.single_shot)
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
