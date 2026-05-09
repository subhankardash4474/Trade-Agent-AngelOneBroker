"""
Main Entry Point
Launch the trading agent in various modes: live trading, paper trading,
backtesting, or dashboard-only viewing.
"""

# Phase 1 sys.path bootstrap -- packages/ is the new home for core, strategies, etc.
import sys as _sys
from pathlib import Path as _Path
_pkg = _Path(__file__).resolve().parent / 'packages'
if str(_pkg) not in _sys.path:
    _sys.path.insert(0, str(_pkg))


import argparse
import os
import ssl
import sys
import threading

import yaml
from loguru import logger

# Corporate proxy / self-signed cert workaround (common on office networks)
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


def connect_angelone(config: dict):
    """
    Establish an authenticated session with AngelOne SmartAPI.
    Returns the SmartAPI object or None if in paper/simulation mode.
    """
    broker = config.get("broker", {})
    if broker.get("mode") == "paper":
        logger.info("Running in PAPER mode — no broker connection needed")
        return None

    try:
        from SmartApi import SmartConnect
        import pyotp

        api = SmartConnect(api_key=broker["api_key"])
        totp = pyotp.TOTP(broker["totp_secret"]).now()
        session = api.generateSession(broker["client_id"], broker["password"], totp)

        if session.get("status"):
            feed_token = api.getfeedToken()
            logger.info(f"AngelOne session established for {broker['client_id']}")
            return api
        else:
            logger.error(f"AngelOne login failed: {session.get('message', 'Unknown error')}")
            sys.exit(1)
    except ImportError:
        logger.error("smartapi-python not installed. Run: pip install smartapi-python pyotp")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to connect to AngelOne: {e}")
        sys.exit(1)


def run_agent(args):
    """Start the live/paper trading agent."""
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    if args.paper:
        config["broker"]["mode"] = "paper"

    smart_api = connect_angelone(config)

    from trading_agent import TradingAgent
    from monitoring.dashboard import Dashboard

    agent = TradingAgent(
        config_path=args.config,
        smart_api=smart_api,
        reset_balance=getattr(args, "reset_balance", False),
    )

    if args.dashboard:
        dashboard = Dashboard(agent, refresh_interval=config.get("monitoring", {}).get("dashboard_refresh_seconds", 5))
        agent_thread = threading.Thread(target=agent.run, kwargs={"poll_interval": args.interval}, daemon=True)
        agent_thread.start()
        dashboard.run()
    else:
        agent.run(poll_interval=args.interval)


def run_backtest(args):
    """Run backtesting engine."""
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    from research.backtest import BacktestEngine

    symbols = args.symbols
    if symbols is None:
        symbols = [i["symbol"] for i in config.get("market", {}).get("instruments", [])]

    engine = BacktestEngine(config)
    engine.run(symbols=symbols, strategy_names=args.strategies, interval=args.interval)
    engine.print_results()

    if args.export:
        engine.export_results()


def run_status(args):
    """Print a one-time status snapshot."""
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    config["broker"]["mode"] = "paper"

    from trading_agent import TradingAgent
    from monitoring.dashboard import print_snapshot

    agent = TradingAgent(config_path=args.config, smart_api=None)
    print_snapshot(agent)


def run_scan(args):
    """Run the stock scanner and show results."""
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    from core.stock_scanner import StockScanner

    scanner = StockScanner(config)
    results = scanner.scan(force=True)

    print(scanner.get_scan_summary())
    if args.top:
        print(f"\nTop {args.top} would be used for trading.")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="AI Trading Agent for Indian Stock Market (AngelOne)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py trade --paper --dashboard          Start paper trading with dashboard
  python main.py trade --paper --interval 30        Paper trade, poll every 30s
  python main.py scan                               Run stock scanner to see picks
  python main.py backtest --symbols RELIANCE TCS    Backtest specific symbols
  python main.py backtest --export                  Backtest and export results
  python main.py status                             Show current agent status
        """,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Trade command
    trade_parser = subparsers.add_parser("trade", help="Start the trading agent")
    trade_parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    trade_parser.add_argument("--dashboard", action="store_true", help="Enable live CLI dashboard")
    trade_parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    trade_parser.add_argument(
        "--reset-balance",
        action="store_true",
        help="Ignore DB equity history and start fresh from config.initial_balance "
             "(otherwise the agent continues from the last equity snapshot).",
    )

    # Backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run strategy backtests")
    bt_parser.add_argument("--symbols", nargs="+", default=None, help="Symbols to backtest")
    bt_parser.add_argument("--strategies", nargs="+", default=None, help="Strategies to test")
    bt_parser.add_argument("--interval", default="1d", help="Data interval (default: 1d)")
    bt_parser.add_argument("--export", action="store_true", help="Export results to CSV")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Run stock scanner to see picks")
    scan_parser.add_argument("--top", type=int, default=None, help="Override top_n picks")

    # Status command
    subparsers.add_parser("status", help="Show agent status snapshot")

    args = parser.parse_args()

    if args.command == "trade":
        run_agent(args)
    elif args.command == "backtest":
        run_backtest(args)
    elif args.command == "scan":
        run_scan(args)
    elif args.command == "status":
        run_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
