"""
Dry-run preview of the new EOD email format. Builds the exact same report
string the daemon would have sent at 15:20 today, but logs it to stdout
WITHOUT calling the alert manager. Use to preview format changes without
spamming the inbox.

Usage:
    python -m tools.preview_eod_summary
"""

from __future__ import annotations

import os
import ssl
import sys

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
    from datetime import datetime
    import pytz
    import yaml
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    config.setdefault("broker", {})["mode"] = "paper"

    from trading_agent import TradingAgent
    agent = TradingAgent(
        config_path="config.yaml",
        smart_api=None,
        reset_balance=False,
    )

    IST = pytz.timezone("Asia/Kolkata")
    now = datetime.now(IST)
    day_iso = now.strftime("%Y-%m-%d")

    # Replicate the report-build path from `_maybe_send_eod_summary` exactly
    # but skip the alert manager call.
    summary = agent.portfolio.get_summary()
    risk = agent.risk_manager.get_risk_summary()

    diag = agent._build_daily_diagnostics(day_iso)
    strategy_mix = agent._build_strategy_mix_report()
    open_pos_block = agent._build_open_positions_section()

    try:
        _today_rows = agent.database.load_trades_for_day(day_iso) or []
        _wins = sum(1 for r in _today_rows if (r.get("pnl", 0) or 0) > 0)
        _wr = (_wins / len(_today_rows) * 100) if _today_rows else 0.0
    except Exception:
        _wr = 0.0

    _peak = float(risk.get("peak_equity") or risk.get("peak", 0) or 0)
    _equity_now = float(summary.get("total_value", summary.get("cash", 0)) or 0)

    report = (
        f"EOD Report {day_iso}\n"
        f"Day PnL: Rs {risk['daily_pnl']:+,.2f}\n"
        f"Trades: {risk['daily_trades']}\n"
        f"Win Rate: {_wr:.0f}%\n"
        f"Cash: Rs {summary['cash']:,.2f}\n"
        f"Equity (mark-to-market): Rs {_equity_now:,.2f}"
        + (f"  (peak Rs {_peak:,.2f})" if _peak else "") + "\n"
        f"Drawdown: {risk['drawdown_pct']:.1f}%   [agent halts at 20%]"
        f"{open_pos_block}"
        f"{diag}"
        f"{strategy_mix}"
    )

    print()
    print("=" * 78)
    print("PREVIEW OF NEW EOD EMAIL FORMAT")
    print("=" * 78)
    print(report)
    print("=" * 78)
    print("(no email sent — preview only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
