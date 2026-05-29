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
from datetime import datetime, time as dt_time
from pathlib import Path

import pytz
import yaml
from loguru import logger

# Corporate proxy / self-signed-cert workaround.
#
# B-3 (audit 2026-05-25): polarity flipped. DEFAULT IS NOW SECURE (verify
# enabled). To opt OUT (e.g. behind a corporate MITM proxy with a private
# CA), set TRADER_DISABLE_SSL_VERIFY=true in the local .env. The cloud VM
# docker-compose.yml already sets this to "false" explicitly, so cloud
# posture is unchanged. The previous default of "true" silently trusted
# any certificate on every laptop run.
#
# When the bypass IS enabled we now log a WARNING at startup so misuse is
# visible in `logs/trading_agent_*.log` rather than silent.
_ssl_bypass = os.environ.get("TRADER_DISABLE_SSL_VERIFY", "false").lower()
if _ssl_bypass in ("1", "true", "yes"):
    logger.warning(
        "TRADER_DISABLE_SSL_VERIFY is ENABLED — every HTTPS call is now "
        "trusting any certificate without verification. This is ONLY safe "
        "behind a corporate MITM proxy with a known private CA. Unset this "
        "env var (or set =false) in production / cloud deployments."
    )
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
    """Returns True if within 08:00-15:30 IST on a weekday — the window in
    which the agent should be running.

    Bug N (2026-05-29): the upper bound used to be hour-resolution
    ``hour < 16`` (i.e. window stayed open until 15:59:59 IST). That
    misaligned with ``TradingAgent._trading_cycle``'s self-exit at
    15:30 IST, so the supervisor's "skipping restart loop" branch in
    ``main()`` would call ``continue``, the next outer iteration would
    see ``is_market_window()=True``, skip ``sleep_until_market``, and
    re-launch the agent -- which then self-exited again on its first
    cycle. Result: ~22 spurious agent restarts every trading day
    between 15:30 and 16:00 IST (verified in trader-VM logs
    2026-05-29: 22 ``[AUDIT-CHECKPOINT]_HHMM`` writes with ``Cycle=1``
    each, plus 36 ``[ALERT-SUPPRESSED]`` lines from the dedup'd
    EOD/Scanner alerts that fired on every restart). Symptom was
    masked by alert-dedup so the 2026-05-13 "11 EOD emails" surface
    incident never resurfaced -- but the underlying loop ran every day.

    Tightening the upper bound to 15:30 makes ``is_market_window()``
    return False the instant the agent self-exits, so the supervisor's
    next iteration goes into ``sleep_until_market`` as the
    2026-05-13 patch intended. See
    ``docs/findings/findings_log_2026-05-27.md`` §13 for the full RCA.
    """
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(8, 0) <= t < dt_time(15, 30)


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
        # B-11 (audit 2026-05-25): the actual config key is
        # `capital.initial_balance`, not top-level `initial_capital`. The
        # old read was always None → cash always 0.0 in the off-hours
        # health.json, which fed a false "starved daemon" signal to the
        # watchdog and the heartbeat email.
        "cash": float((config.get("capital") or {}).get("initial_balance", 0.0)),
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


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into ``base``. Lists & scalars in
    ``overlay`` replace whatever is in ``base`` (so ``market.instruments``
    in an overlay file fully replaces, not concatenates). Dicts merge
    key-by-key. Returns a *fully detached* result -- mutating the merged
    dict (e.g. ``cfg["broker"]["mode"] = "live"`` downstream) does NOT
    leak back into ``base`` or ``overlay``.
    """
    import copy
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def run_once(config_path: str, paper: bool, interval: int, dashboard: bool,
             reset_balance: bool = False,
             max_loss_rs: float | None = None,
             single_shot: bool = False,
             live: bool = False,
             config_overlay: str | None = None):
    """Single run of the trading agent. Returns when agent exits or crashes.

    ``paper`` and ``live`` are mutually exclusive runtime overrides on top
    of whatever ``broker.mode`` says in the YAML. Resolution order:
      1. ``--paper`` -> force "paper" (even if config says live)
      2. ``--live``  -> force "live"  (even if config says paper)
      3. Neither    -> use the YAML value as-is

    ``config_overlay`` is an optional second YAML file whose keys are
    deep-merged over the base config. Used for Stage 3 / scenario
    presets without forking ``config.yaml``.
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if config_overlay:
        with open(config_overlay, "r") as f:
            overlay = yaml.safe_load(f) or {}
        config = _deep_merge(config, overlay)
        logger.warning(
            f"[OVERLAY] applied {config_overlay} -- {len(overlay)} top-level "
            f"section(s) merged"
        )

    if paper:
        # F-31: safe chain-subscript -- a minimal/empty config that omits
        # the broker: section would crash with KeyError otherwise.
        config.setdefault("broker", {})["mode"] = "paper"
    elif live:
        # Loud log line so a tail of the daemon stdout makes it obvious
        # this run is touching real money. We do NOT short-circuit on a
        # missing API key here -- ``connect_angelone`` will fail-fast
        # with the actual reason. Better one error message than two.
        logger.warning("=" * 60)
        logger.warning("[E2E] --live: broker.mode -> 'live' (REAL MONEY)")
        logger.warning("=" * 60)
        # F-31: same safe chain-subscript as the paper branch above.
        config.setdefault("broker", {})["mode"] = "live"

    smart_api = None
    if config.get("broker", {}).get("mode") != "paper":
        from main import connect_angelone
        smart_api = connect_angelone(config)

    from trading_agent import TradingAgent

    # When an overlay / mode-flag was applied, we pass the *merged* dict in
    # via the new ``config`` kwarg so TradingAgent doesn't re-parse the raw
    # YAML and silently drop our deltas. ``config_path`` is still passed so
    # downstream code that wants the on-disk location (logs, post-mortem,
    # etc.) keeps working.
    agent = TradingAgent(
        config_path=config_path,
        smart_api=smart_api,
        reset_balance=reset_balance,
        max_loss_rs=max_loss_rs,
        single_shot=single_shot,
        config=config,
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
    # F-32: previously ``action="store_true", default=True`` meant the
    # flag could never be disabled from the CLI (passing it and omitting
    # it both yielded True). BooleanOptionalAction supports both
    # ``--market-hours-only`` and ``--no-market-hours-only``.
    parser.add_argument(
        "--market-hours-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only run during market hours. Use --no-market-hours-only to "
             "run 24/7 (testing, shadow mode, non-market monitoring).",
    )
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
    parser.add_argument(
        "--config-overlay", default=None, metavar="PATH",
        help="Optional second YAML file whose keys deep-merge over the "
             "base ``--config``. Used for Stage 3 / scenario presets "
             "(e.g. ``--config-overlay config_overlays/stage3.yaml``). "
             "Lists & scalars in the overlay REPLACE whatever is in the "
             "base; dicts merge key-by-key.",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Stage 3 cutover: force broker.mode = 'live' at runtime, "
             "overriding whatever is in config.yaml. Required to place real "
             "orders on AngelOne. Mutually exclusive with --paper -- if both "
             "are set, --paper wins (defensive: a CLI typo should never "
             "accidentally place real orders). This flag exists so the same "
             "config file can be reused across paper / shadow / live runs "
             "without git-flipping ``broker.mode`` in source control (which "
             "is the kind of edit that gets forgotten on the way back to "
             "paper).",
    )
    args = parser.parse_args()

    # Defensive: --paper always wins over --live. If someone types both
    # on the CLI we treat it as paper. Better to lose a live session to a
    # typo than to lose real money to one.
    if args.paper and args.live:
        logger.warning(
            "Both --paper AND --live were passed. --paper wins (defensive). "
            "If you intended live, remove --paper and rerun."
        )
        args.live = False

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

    # Mode line resolution:
    #   --paper          -> PAPER
    #   --live           -> LIVE (real money)
    #   neither          -> "config" (whatever the YAML says; logged
    #                       explicitly so the operator never has to grep
    #                       config.yaml to find out)
    if args.paper:
        mode_label = "PAPER (--paper)"
    elif args.live:
        mode_label = "LIVE (--live, REAL MONEY)"
    else:
        try:
            with open(args.config, "r") as _f:
                _cfg = yaml.safe_load(_f) or {}
            mode_label = f"{(_cfg.get('broker') or {}).get('mode', 'unknown').upper()} (from config)"
        except Exception:
            mode_label = "unknown (config read failed)"

    logger.info("=" * 60)
    logger.info("TRADING AGENT DAEMON STARTED")
    logger.info(f"  Config: {args.config}")
    logger.info(f"  Mode: {mode_label}")
    logger.info(f"  Poll: {args.interval}s")
    logger.info(f"  Market hours only: {args.market_hours_only}")
    if args.max_loss_rs is not None:
        logger.warning(f"  [E2E] --max-loss-rs: Rs {args.max_loss_rs:,.2f} (hard rupee floor)")
    if args.single_shot:
        logger.warning("  [E2E] --single-shot: one round-trip per symbol per day")
    if args.live:
        logger.warning("  [E2E] --live: real-money orders -- pre-flight checks MUST be green")
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
                     single_shot=args.single_shot,
                     live=args.live,
                     config_overlay=args.config_overlay)
            logger.info("Agent exited cleanly")
            # 2026-05-13: do NOT just `break` here.
            #
            # The agent has exactly one voluntary clean-exit path:
            # ``TradingAgent._trading_cycle`` sets ``self._running = False``
            # at >= 15:30 IST (post market_close). Until today, this branch
            # broke out of the wrapper, the wrapper process exited, and
            # Docker's ``restart: unless-stopped`` policy re-created the
            # container -- which re-ran the agent, which re-ran the EOD
            # work (postmortem + profit-diagnostic subprocesses, each
            # capped at 60-120s of yfinance calls), and re-fired the EOD
            # email. Repeat ~10 times until 16:00 IST flips
            # ``is_market_window`` False. Operator received 11 identical
            # EOD Summary emails on 2026-05-13.
            #
            # New behaviour: if the agent exited cleanly inside the
            # market_hours_only window (i.e. it was a market-close exit,
            # not a manual stop), transition directly to
            # ``sleep_until_market`` instead of letting Docker burn a
            # container restart cycle. ``sleep_until_market`` itself
            # exits when 16:00 IST flips the window AND wakes for the
            # emergency-stop file -- the two correct paths out of the
            # idle window.
            if args.market_hours_only:
                now_ist = datetime.now(IST)
                close_h, close_m = 15, 30
                past_close = (now_ist.weekday() < 5 and
                              (now_ist.hour > close_h or
                               (now_ist.hour == close_h and
                                now_ist.minute >= close_m)))
                if past_close:
                    logger.info(
                        f"Agent self-exited at {now_ist.strftime('%H:%M:%S')} "
                        f"IST -- skipping restart loop and sleeping until "
                        f"the next market window."
                    )
                    backoff = 2
                    crash_count = 0
                    # Bug N (2026-05-29): the *primary* fix lives in
                    # ``is_market_window()`` (upper bound tightened
                    # from 16:00 IST to 15:30 IST so the next outer
                    # iteration's gate flips False the instant the
                    # agent self-exits). The explicit
                    # ``sleep_until_market`` call below is defence-in-
                    # depth: if a future operator widens
                    # ``is_market_window()`` without fixing this branch,
                    # the call still routes us into the off-hours
                    # sleep loop here-and-now instead of falling
                    # through into another ``start_agent`` /
                    # ``run_once`` round-trip. See
                    # docs/findings/findings_log_2026-05-27.md §13.
                    sleep_until_market(args.config)
                    continue
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
