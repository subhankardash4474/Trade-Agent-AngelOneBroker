"""Manual position-close CLI.

Replaces the multi-step manual choreography we used during the 2026-05-07
ZYDUSWELL/MEESHO interventions. Single command:

    python tools/close_position.py SYMBOL [--reason REASON] [--at-ltp]
                                   [--price PRICE] [--dry-run]
                                   [--no-stop-daemon]

Behaviour:
  1. (Optional) Stops the running daemon if `daemon.pid` exists. Prevents
     the daemon from racing with the manual close. Skip with
     `--no-stop-daemon` if you've already stopped it (e.g. emergency stop).
  2. Loads Portfolio + Database, finds the open position.
  3. Resolves exit price:
       --at-ltp (default) : pulls current LTP from yfinance (.NS).
       --price 305.50     : uses the explicit price.
  4. Calls portfolio.close_position() — which now (post-2026-05-07)
     persists the trade idempotently via Database.store_trade().
  5. Prints the resulting TradeRecord and reconciles cash.
  6. (Optional) Restarts the daemon via run_daemon_resilient.ps1.

Examples:
    # Dry-run a close at LTP, daemon stays up so you can see the impact:
    python tools/close_position.py CROMPTON --dry-run --no-stop-daemon

    # Real close at LTP with the daemon stopped/restarted automatically:
    python tools/close_position.py CROMPTON --reason manual_protective_trend

    # Close at an explicit price (after-hours analysis):
    python tools/close_position.py CROMPTON --price 305.50 --reason post_close
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger


PID_FILE = ROOT / "daemon.pid"
WATCHDOG_PS1 = ROOT / "tools" / "run_daemon_resilient.ps1"


def _get_ltp(symbol: str) -> Optional[float]:
    """Pull the latest 5-min close from yfinance as a paper-LTP proxy."""
    try:
        import yfinance as yf
        df = yf.download(f"{symbol}.NS", period="2d", interval="5m",
                         progress=False, auto_adjust=False)
        if df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"Failed to fetch LTP for {symbol}: {e}")
        return None


def _stop_daemon() -> bool:
    """Best-effort: stop the daemon by SIGTERM, return True if stopped."""
    if not PID_FILE.exists():
        logger.info("No daemon.pid found - assuming daemon not running")
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception as e:
        logger.error(f"Bad daemon.pid: {e}")
        return False
    logger.info(f"Stopping daemon PID={pid}...")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           check=False, capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        logger.warning(f"taskkill/SIGTERM failed: {e}")
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass
            logger.info("Daemon stopped")
            return True
        time.sleep(0.5)
    logger.warning("Daemon did not exit within 15s - proceeding anyway")
    return False


def _restart_daemon() -> None:
    """Best-effort: re-launch the watchdog wrapper."""
    if not WATCHDOG_PS1.exists():
        logger.warning(f"{WATCHDOG_PS1} not found - skipping restart")
        return
    if os.name != "nt":
        logger.warning("Auto-restart only supported on Windows; restart manually")
        return
    logger.info("Restarting daemon via watchdog...")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-File", str(WATCHDOG_PS1)],
        cwd=str(ROOT),
    )
    logger.info("Watchdog launched (background)")


def main() -> int:
    p = argparse.ArgumentParser(description="Manually close an open position")
    p.add_argument("symbol")
    p.add_argument("--reason", default="manual_close",
                   help="exit_reason tag (e.g. manual_protective_trend)")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--at-ltp", action="store_true",
                     help="use latest 5-min close from yfinance (default)")
    grp.add_argument("--price", type=float, default=None,
                     help="explicit exit price")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen, don't write anything")
    p.add_argument("--no-stop-daemon", action="store_true",
                   help="skip daemon stop/start (caller already handled it)")
    p.add_argument("--no-restart", action="store_true",
                   help="don't restart the daemon after closing")
    args = p.parse_args()

    sym = args.symbol.upper()

    if not args.no_stop_daemon and not args.dry_run:
        _stop_daemon()

    from core.config_loader import load_config
    from core.database import Database
    from core.portfolio import Portfolio

    config = load_config("config.yaml")
    initial_capital = float(config.get("trading", {}).get("initial_capital", 10000))
    db = Database("data/trading_agent.db")
    portfolio = Portfolio(
        initial_balance=initial_capital,
        commission_pct=config.get("trading", {}).get("commission_pct", 0.03),
        database=db,
    )

    pos = portfolio.positions.get(sym)
    if pos is None:
        logger.error(f"No open position for {sym}")
        return 1

    if args.price is not None:
        exit_price = float(args.price)
        src = "explicit"
    else:
        exit_price = _get_ltp(sym) or pos.entry_price
        src = "ltp" if exit_price != pos.entry_price else "fallback_entry"

    side = pos.side
    qty = pos.quantity
    entry = pos.entry_price
    pnl_estimate = (entry - exit_price) * qty if side == "SELL" else (exit_price - entry) * qty

    logger.info(f"=== Close plan for {sym} ===")
    logger.info(f"  Side: {side}  Qty: {qty}")
    logger.info(f"  Entry: {entry:.2f}  Exit: {exit_price:.2f}  ({src})")
    logger.info(f"  Estimated gross PnL: {pnl_estimate:+.2f}  (excl. commission)")
    logger.info(f"  Reason: {args.reason}")

    if args.dry_run:
        logger.info("DRY-RUN — no changes made")
        return 0

    rec = portfolio.close_position(sym, exit_price=exit_price,
                                    exit_reason=args.reason)
    if rec is None:
        logger.error("close_position() returned None — close FAILED")
        return 2

    logger.info("=== Closed ===")
    logger.info(
        f"  Realized PnL: {rec.pnl:+.2f} ({rec.pnl_pct:+.2f}%)  "
        f"commission {rec.commission:.2f}  held {rec.holding_minutes:.1f}m"
    )
    logger.info(f"  Cash now: \u20B9{portfolio.cash:.2f}")

    if not args.no_stop_daemon and not args.no_restart:
        _restart_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
