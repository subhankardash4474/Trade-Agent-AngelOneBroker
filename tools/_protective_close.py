"""One-shot protective close of trend-mismatched positions.

Closes ZYDUSWELL and MEESHO at current LTP via the proper
Portfolio.close_position() path so all charges, cash accounting,
and equity persistence happen the standard way. Exit reason
is tagged "manual_protective_trend" so tomorrow's audit can
distinguish these from algorithmic exits.

This script must only be run when the daemon is stopped, otherwise
the in-memory Portfolio in the running daemon will diverge from the
DB. Stop the scheduled task first:

    Stop-ScheduledTask -TaskName TradingAgentDaemon

Then run this script, then restart:

    Start-ScheduledTask -TaskName TradingAgentDaemon
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from core.database import Database
from core.data_handler import DataHandler
from core.portfolio import Portfolio


def load_config() -> dict:
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


SYMBOLS_TO_CLOSE = ["ZYDUSWELL"]
EXIT_REASON = "manual_protective_trend"


def main() -> None:
    cfg = load_config()
    db_path = cfg.get("database", {}).get("path", "data/trading_agent.db")

    db = Database(db_path)
    dh = DataHandler(cfg)

    initial_capital = float(cfg.get("trading", {}).get("initial_capital", 50000))
    portfolio = Portfolio(initial_balance=initial_capital, database=db)

    # Print current state
    logger.info(f"Portfolio loaded with {len(portfolio.positions)} open positions:")
    for sym, p in portfolio.positions.items():
        logger.info(f"  {sym} {p.side} qty={p.quantity} entry={p.entry_price:.2f} "
                    f"sl={p.stop_loss} tp={p.take_profit}")

    print()
    closed_records = []
    for sym in SYMBOLS_TO_CLOSE:
        if sym not in portfolio.positions:
            logger.warning(f"{sym} not in open positions, skipping")
            continue

        ltp = dh.get_ltp(sym)
        if ltp is None or ltp <= 0:
            logger.error(f"Could not get LTP for {sym}, skipping")
            continue

        logger.info(f"Closing {sym} @ LTP {ltp:.2f} (reason={EXIT_REASON})")
        rec = portfolio.close_position(sym, exit_price=ltp, exit_reason=EXIT_REASON)
        if rec is not None:
            closed_records.append(rec)
            logger.info(
                f"  -> Closed: PnL={rec.pnl:+.2f} ({rec.pnl_pct:+.2f}%) "
                f"commission={rec.commission:.2f} held={rec.holding_minutes:.1f}m"
            )
            try:
                db.store_trade({
                    "symbol": rec.symbol,
                    "side": rec.side,
                    "entry_price": rec.entry_price,
                    "exit_price": rec.exit_price,
                    "quantity": rec.quantity,
                    "entry_time": rec.entry_time.isoformat() if hasattr(rec.entry_time, "isoformat") else str(rec.entry_time),
                    "exit_time": rec.exit_time.isoformat() if hasattr(rec.exit_time, "isoformat") else str(rec.exit_time),
                    "pnl": rec.pnl,
                    "pnl_pct": rec.pnl_pct,
                    "strategy": rec.strategy,
                    "exit_reason": rec.exit_reason,
                    "commission": rec.commission,
                    "slippage": 0,
                    "market_context": "protective close - against-trend SHORT",
                })
                logger.info(f"  -> Persisted to trades table")
            except Exception as e:
                logger.error(f"  -> FAILED to persist trade row: {e}")
        else:
            logger.error(f"  -> Close FAILED for {sym}")

    print()
    total_pnl = sum(r.pnl for r in closed_records)
    logger.info(f"=== Summary ===")
    logger.info(f"Closed {len(closed_records)} positions, total PnL: {total_pnl:+.2f}")
    logger.info(f"Remaining open positions: {len(portfolio.positions)}")
    for sym, p in portfolio.positions.items():
        logger.info(f"  {sym} {p.side} qty={p.quantity} entry={p.entry_price:.2f}")
    logger.info(f"Cash now: {portfolio.cash:,.2f}")


if __name__ == "__main__":
    main()
