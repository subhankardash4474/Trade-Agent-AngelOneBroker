"""
Portfolio Module
Tracks open positions, trade history, and computes P&L in real time.
"""

import csv
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import pytz
from loguru import logger

from core.charges import compute_one_leg, compute_round_trip

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class Position:
    """Represents an open position."""
    symbol: str
    side: str  # BUY or SELL
    entry_price: float
    quantity: int
    entry_time: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    order_id: str = ""
    rsi: Optional[float] = None
    atr_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    market_trend: Optional[int] = None
    regime: Optional[str] = None
    # Per-strategy credit shares (sums to ~1.0). Used by the learner to
    # attribute PnL back to the strategies that voted for this trade.
    contributing_strategies: Optional[Dict[str, float]] = None

    @property
    def value(self) -> float:
        return self.entry_price * self.quantity

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.quantity
        return (self.entry_price - current_price) * self.quantity

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / self.value * 100


@dataclass
class TradeRecord:
    """Immutable record of a completed trade."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: int
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_pct: float
    strategy: str
    exit_reason: str  # signal, stop_loss, take_profit, eod_square_off
    commission: float = 0.0
    rsi: Optional[float] = None
    atr_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    market_trend: Optional[int] = None
    regime: Optional[str] = None
    contributing_strategies: Optional[Dict[str, float]] = None
    holding_minutes: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "strategy": self.strategy,
            "exit_reason": self.exit_reason,
            "commission": round(self.commission, 2),
            "regime": self.regime or "",
            "holding_minutes": round(self.holding_minutes, 1),
        }


class Portfolio:
    """
    Manages open positions and trade history.
    Provides real-time P&L and performance metrics.
    Persists open positions to database so they survive agent restarts.
    """

    def __init__(self, initial_balance: float, commission_pct: float = 0.03,
                 log_dir: str = "logs", database=None, product_type: str = "INTRADAY",
                 reset_balance: bool = False):
        """
        Args:
            initial_balance: Seed balance used on first ever run (or when
                `reset_balance=True`). Once the DB has equity history, the
                portfolio will continue from the last recorded cash balance
                unless you explicitly opt out.
            reset_balance: If True, ignore DB history and start fresh from
                `initial_balance`. Useful for debugging or after manual resets.
        """
        self.initial_balance = initial_balance
        self.cash = initial_balance
        self.commission_pct = commission_pct
        # INTRADAY (MIS) or DELIVERY (CNC) — drives realistic charges model
        self.product_type = product_type.upper()
        self.positions: Dict[str, Position] = {}
        self.trade_history: List[TradeRecord] = []
        self._db = database
        self._log_dir = log_dir
        self._trade_log_path = os.path.join(log_dir, "trades.csv")
        self._ensure_trade_log()
        # Stash the flag so _restore_positions can decide whether to override
        # cash from the equity_curve snapshot. Pre-2026-05-05, --reset-balance
        # only short-circuited the standalone cash-restore branch (above) but
        # _restore_positions (below) still unconditionally clobbered cash from
        # the snapshot, defeating the flag whenever ANY open position existed
        # at boot. With this change, --reset-balance now reliably means
        # "cash := initial_balance from config, regardless of held positions",
        # which matches the user's intuition of "top up my budget without
        # touching trades in flight".
        self._reset_balance = reset_balance
        # Reentrant lock to serialize open/close operations. Prevents the
        # race observed on 2026-04-27 where 4 ELECON trades opened at the
        # exact same second under two strategies emitting BUY signals
        # concurrently within a single trading cycle.
        self._pos_lock = threading.RLock()

        # Continuity across days: seed cash from the last equity snapshot so
        # yesterday's losses/gains carry forward, instead of blindly resetting
        # to `initial_balance` every morning. Config value is only used as a
        # first-ever seed or if explicitly reset.
        if self._db is not None and not reset_balance:
            try:
                snap = self._db.get_last_equity_point()
                if snap and snap.get("cash") is not None:
                    last_cash = float(snap["cash"])
                    last_positions = int(snap.get("positions", 0))
                    # Only trust the snapshot if no positions were open at snapshot
                    # time. If positions were open, _restore_positions() will
                    # compute the cash correctly based on cost basis.
                    if last_positions == 0:
                        logger.info(
                            f"Restoring cash from DB snapshot: Rs {last_cash:,.2f} "
                            f"(config initial_balance was Rs {initial_balance:,.2f})"
                        )
                        self.cash = last_cash
                    else:
                        logger.debug(
                            f"Skipping cash restore — DB snapshot had {last_positions} "
                            f"open positions; _restore_positions will handle it."
                        )
            except Exception as e:
                logger.warning(f"Failed to restore cash from DB: {e}")

        # Restore open positions from database (if agent was restarted mid-day)
        if self._db is not None:
            self._restore_positions()

    def _restore_positions(self):
        """Reload open positions from database after a restart.

        Cash reconciliation:
          The historical heuristic was `min(cash_after)` over remaining open
          positions, which works when the agent only OPENS between snapshots.
          But when a position CLOSES (releasing cash), the cash_after fields
          on the OTHER still-open rows become stale — they reflect the cash
          balance at THEIR open, before the later close happened.

          Today (2026-05-04) IDEA was closed at 10:29:30 freeing ~Rs 2,800
          of collateral; the daemon was killed at 10:31 before the next
          5-cycle equity snapshot; on restart `min(cash_after)` returned
          Rs 1,218 (stale, missing the IDEA close bump). Equity downstream
          showed Rs 6,679 instead of the true Rs ~9,500 — a phantom Rs 2,830
          loss with no corresponding trade.

          Fix: now that close_position persists an equity snapshot atomically
          (see `_persist_state_after_event`), the equity_curve table has the
          freshest cash reading. If its snapshot timestamp is NEWER than the
          most recent open-position entry_time, that snapshot is authoritative
          and we use it directly. Otherwise we fall back to the legacy
          min(cash_after) heuristic for backward compatibility.
        """
        try:
            saved = self._db.load_open_positions()
            if not saved:
                return
            # Sort by entry_time to replay cash deductions in order
            saved.sort(key=lambda r: r.get("entry_time", ""))
            min_cash = None
            latest_open_time: Optional[datetime] = None
            for row in saved:
                pos = Position(
                    symbol=row["symbol"],
                    side=row["side"],
                    entry_price=row["entry_price"],
                    quantity=row["quantity"],
                    entry_time=datetime.fromisoformat(row["entry_time"]),
                    stop_loss=row.get("stop_loss"),
                    take_profit=row.get("take_profit"),
                    strategy=row.get("strategy", ""),
                    order_id=row.get("order_id", ""),
                )
                self.positions[row["symbol"]] = pos
                ca = row.get("cash_after")
                if ca is not None:
                    if min_cash is None or ca < min_cash:
                        min_cash = ca
                try:
                    et = pos.entry_time
                    if et.tzinfo is None:
                        et = IST.localize(et)
                    if latest_open_time is None or et > latest_open_time:
                        latest_open_time = et
                except Exception:
                    pass

            # Prefer the latest equity_curve snapshot's cash IF it's newer
            # than any open position. That handles the close-during-shutdown
            # case where a snapshot was written after a close but before the
            # next open. See docstring above for the 2026-05-04 incident.
            cash_resolved = None
            cash_source = "min_cash_after"
            try:
                snap = self._db.get_last_equity_point()
            except Exception:
                snap = None
            if snap and snap.get("cash") is not None and snap.get("timestamp"):
                try:
                    snap_time = datetime.fromisoformat(snap["timestamp"])
                    if snap_time.tzinfo is None:
                        snap_time = IST.localize(snap_time)
                    if (latest_open_time is None
                            or snap_time >= latest_open_time):
                        cash_resolved = float(snap["cash"])
                        cash_source = "equity_curve_snapshot"
                except (ValueError, TypeError):
                    pass

            if cash_resolved is None and min_cash is not None:
                cash_resolved = float(min_cash)

            # When --reset-balance is in effect, the user explicitly asked for
            # a fresh cash baseline. Do NOT clobber self.cash with the snapshot
            # value; keep it at self.initial_balance (already set in __init__).
            # This is the fix for the live observation on 2026-05-05 where
            # `python run_daemon.py --paper --reset-balance` (after editing
            # config to Rs 25k) booted with cash=Rs 2,482 instead of Rs 25,000
            # because 3 open positions caused this branch to override cash
            # from the equity_curve snapshot regardless of the flag.
            if self._reset_balance:
                cash_source = "reset_balance_flag"
                logger.info(
                    f"Restored {len(saved)} open positions from database: "
                    f"{[p['symbol'] for p in saved]} | Cash: \u20B9{self.cash:,.2f} "
                    f"(source={cash_source}; snapshot Rs {cash_resolved:,.2f} "
                    f"intentionally ignored due to --reset-balance)"
                    if cash_resolved is not None else
                    f"Restored {len(saved)} open positions from database: "
                    f"{[p['symbol'] for p in saved]} | Cash: \u20B9{self.cash:,.2f} "
                    f"(source={cash_source})"
                )
                # Persist the new cash baseline + restored positions to the
                # equity_curve immediately so any next-boot restart sees the
                # reset-cash value as authoritative.
                try:
                    self._persist_state_after_event()
                except Exception:
                    pass
                return

            if cash_resolved is not None:
                self.cash = cash_resolved
            logger.info(
                f"Restored {len(saved)} open positions from database: "
                f"{[p['symbol'] for p in saved]} | Cash: \u20B9{self.cash:,.2f} "
                f"(source={cash_source})"
            )
        except Exception as e:
            logger.error(f"Failed to restore positions: {e}")

    def _ensure_trade_log(self):
        os.makedirs(self._log_dir, exist_ok=True)
        if not os.path.exists(self._trade_log_path):
            with open(self._trade_log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "symbol", "side", "entry_price", "exit_price", "quantity",
                    "entry_time", "exit_time", "pnl", "pnl_pct", "strategy",
                    "exit_reason", "commission",
                ])

    def open_position(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        strategy: str = "",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        order_id: str = "",
        rsi: Optional[float] = None,
        atr_pct: Optional[float] = None,
        volume_ratio: Optional[float] = None,
        market_trend: Optional[int] = None,
        regime: Optional[str] = None,
        contributing_strategies: Optional[Dict[str, float]] = None,
    ) -> bool:
        """
        Open a new position. Thread-safe: serialized via `_pos_lock` so
        concurrent callers for the same symbol cannot both pass the
        duplicate-position check. The DB further enforces uniqueness via
        PRIMARY KEY on `open_positions.symbol`.
        """
        with self._pos_lock:
            # Defensive duplicate-position guard (in-memory + DB re-check).
            # The lock prevents two concurrent callers from both passing this
            # check. The DB PRIMARY KEY is the last line of defence.
            if symbol in self.positions:
                logger.warning(f"Position already open for {symbol} (in-memory)")
                return False
            if self._db is not None:
                try:
                    rows = self._db.load_open_positions()
                    if any(r["symbol"] == symbol for r in rows):
                        logger.warning(
                            f"Position already open for {symbol} (in DB) — refusing duplicate"
                        )
                        return False
                except Exception:
                    pass

            cost = price * quantity
            # Realistic entry-side charges. For a SHORT (side="SELL") entry
            # the charges schedule differs (intraday STT is levied on the
            # sell leg, no stamp duty). compute_one_leg handles this.
            entry_side = "BUY" if side == "BUY" else "SELL"
            commission = compute_one_leg(price, quantity, side=entry_side, product=self.product_type)

            # Cash/margin accounting:
            #   * LONG: we pay `cost + commission` upfront.
            #   * SHORT: with a real broker we'd receive the sell-proceeds
            #     and post margin (≈20% of notional for MIS). For simplicity
            #     and to prevent paper-mode over-leveraging, we lock the full
            #     notional as collateral (same as a long). On close we
            #     reverse the math so the net cash change equals realized PnL.
            if cost + commission > self.cash:
                logger.warning(
                    f"Insufficient cash for {symbol}: need {cost + commission:.2f}, "
                    f"have {self.cash:.2f}"
                )
                return False

            # Persist to DB FIRST (PRIMARY KEY enforces uniqueness). If this
            # fails due to a concurrent insert, abort before mutating
            # in-memory state.
            entry_time = datetime.now(IST)
            entry_time_iso = entry_time.isoformat()
            if self._db is not None:
                try:
                    self._db.save_open_position(
                        symbol=symbol, side=side, entry_price=price,
                        quantity=quantity, entry_time=entry_time_iso,
                        stop_loss=stop_loss, take_profit=take_profit,
                        strategy=strategy, order_id=order_id,
                        cash_after=self.cash - (cost + commission),
                        regime=regime,
                        contributing_strategies=contributing_strategies,
                    )
                except Exception as e:
                    # If DB rejects, abort cleanly. The reason can be
                    # multiple things — a UNIQUE constraint (concurrent
                    # duplicate), a JSON-serialization failure (numpy
                    # types in the contributing_strategies blob), or a
                    # transient sqlite lock. Keep the log message
                    # truthful so future diagnostics aren't misled by
                    # an over-confident hint. (2026-05-06: previously
                    # logged "likely concurrent duplicate" for what
                    # was actually a numpy.float32 JSON failure.)
                    err_kind = type(e).__name__
                    logger.error(
                        f"DB rejected open_position for {symbol}: "
                        f"{err_kind}: {e} — refusing to open"
                    )
                    return False

            self.cash -= (cost + commission)
            self.positions[symbol] = Position(
                symbol=symbol,
                side=side,
                entry_price=price,
                quantity=quantity,
                entry_time=entry_time,
                stop_loss=stop_loss,
                take_profit=take_profit,
                strategy=strategy,
                order_id=order_id,
                rsi=rsi,
                atr_pct=atr_pct,
                volume_ratio=volume_ratio,
                market_trend=market_trend,
                regime=regime,
                contributing_strategies=contributing_strategies or {},
            )
            logger.info(
                f"Opened {side} position: {quantity} x {symbol} @ {price:.2f} "
                f"(commission: {commission:.2f})"
            )
            # Persist cash + equity atomically (2026-05-04 fix). Without this,
            # a daemon kill between events would leave the DB with stale cash
            # — see _restore_positions docstring.
            self._persist_state_after_event()
            return True

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str = "signal",
    ) -> Optional[TradeRecord]:
        with self._pos_lock:
            pos = self.positions.get(symbol)
            if pos is None:
                logger.warning(f"No open position for {symbol}")
                return None

            exit_value = exit_price * pos.quantity

            # Round-trip realistic charges — includes STT, exchange txn, SEBI, GST,
            # stamp duty, and DP (CDSL) if delivery. Entry-side was already deducted
            # from cash on open; here we deduct only the exit-side difference.
            # For SHORT positions: entry leg is SELL (intraday STT, no stamp),
            # exit leg is BUY (stamp duty, no intraday STT). compute_round_trip
            # is symmetric in its buy/sell arguments so it still totals correctly.
            all_charges = compute_round_trip(
                buy_price=pos.entry_price if pos.side == "BUY" else exit_price,
                sell_price=exit_price if pos.side == "BUY" else pos.entry_price,
                quantity=pos.quantity,
                product=self.product_type,
            )
            total_commission = all_charges.total
            entry_side = "BUY" if pos.side == "BUY" else "SELL"
            exit_side = "SELL" if pos.side == "BUY" else "BUY"
            entry_commission = compute_one_leg(
                pos.entry_price, pos.quantity, side=entry_side, product=self.product_type,
            )
            exit_commission = total_commission - entry_commission

            # pnl reflects true realized profit net of all charges on both legs.
            # unrealized_pnl already handles sign for LONG vs SHORT.
            gross_pnl = pos.unrealized_pnl(exit_price)
            pnl = gross_pnl - total_commission

            pnl_pct = 0.0
            if pos.value > 0:
                pnl_pct = pnl / pos.value * 100

            # Cash reconciliation:
            #   LONG: on open we deducted (entry_value + entry_commission).
            #         On close we receive sell proceeds (exit_value) minus the
            #         exit-side commission. Net = gross_pnl - total_commission.
            #   SHORT: on open we deducted (entry_value + entry_commission) as
            #         locked collateral. On close we release that collateral
            #         and apply the realized gross_pnl (which is already signed
            #         correctly by unrealized_pnl), minus exit commission.
            if pos.side == "BUY":
                self.cash += exit_value - exit_commission
            else:
                self.cash += pos.entry_price * pos.quantity + gross_pnl - exit_commission

            exit_time = datetime.now(IST)
            holding_minutes = 0.0
            try:
                entry_time = pos.entry_time
                # Ensure both are tz-aware or both naive
                if entry_time.tzinfo is None:
                    entry_time = IST.localize(entry_time)
                holding_minutes = max(0.0, (exit_time - entry_time).total_seconds() / 60.0)
            except Exception:
                pass

            record = TradeRecord(
                symbol=symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                quantity=pos.quantity,
                entry_time=pos.entry_time,
                exit_time=exit_time,
                pnl=pnl,
                pnl_pct=pnl_pct,
                strategy=pos.strategy,
                exit_reason=exit_reason,
                commission=total_commission,
                rsi=pos.rsi,
                atr_pct=pos.atr_pct,
                volume_ratio=pos.volume_ratio,
                market_trend=pos.market_trend,
                regime=pos.regime,
                contributing_strategies=pos.contributing_strategies or {},
                holding_minutes=holding_minutes,
            )
            self.trade_history.append(record)
            self._log_trade(record)
            del self.positions[symbol]

            logger.info(
                f"Closed {pos.side} position: {pos.quantity} x {symbol} @ {exit_price:.2f} | "
                f"PnL: {pnl:.2f} ({pnl_pct:.2f}%) | Reason: {exit_reason}"
            )

            # Remove from database
            if self._db is not None:
                try:
                    self._db.remove_open_position(symbol)
                except Exception as e:
                    logger.error(f"Failed to remove position from DB: {e}")

                # Idempotent trade persistence (2026-05-07 fix). The original
                # contract was: trading_agent's _on_trade_closed() persists
                # the trade record. But scripts that bypass trading_agent and
                # call close_position() directly (e.g. tools/_protective_close
                # _backfill_zyduswell, future manual-close CLI) used to silently
                # skip persistence, leaving the DB inconsistent with cash and
                # equity_curve. We now attempt persistence here, idempotently
                # — if the trading_agent path also persists later, the second
                # call no-ops via the pre-insert existence check below.
                try:
                    self._maybe_persist_trade(record)
                except Exception as e:
                    logger.error(f"Trade persistence (close_position) failed: {e}")

            # Persist new cash atomically with the close (2026-05-04 fix).
            # The previous gap was: position-row removed, but the cash bump
            # from collateral release + realized PnL only got persisted on
            # the next 5-cycle equity snapshot. A daemon death in that
            # window left the DB with stale cash on next boot.
            self._persist_state_after_event()

            return record

    def _maybe_persist_trade(self, record: "TradeRecord") -> bool:
        """Insert a trade row into the trades table iff not already present.

        Returns True if a new row was inserted, False if a duplicate was
        detected and skipped. Idempotent: trading_agent.py also persists
        via `_store_trade_to_db()`; whichever runs first wins, the second
        no-ops here. Existence check is on (symbol, exit_time) which is
        unique enough in practice (microsecond timestamps).
        """
        if self._db is None:
            return False
        d = record.to_dict() if hasattr(record, "to_dict") else dict(record.__dict__)
        # Normalise datetime-like to iso string the way store_trade expects
        for key in ("entry_time", "exit_time"):
            v = d.get(key)
            if v is not None and hasattr(v, "isoformat"):
                d[key] = v.isoformat()
        try:
            import sqlite3
            db_path = getattr(self._db, "_db_path", None) \
                or getattr(self._db, "db_path", None) \
                or getattr(self._db, "path", None)
            if db_path:
                conn = sqlite3.connect(db_path)
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND exit_time=? LIMIT 1",
                        (d["symbol"], d["exit_time"]),
                    ).fetchone()
                finally:
                    conn.close()
                if exists:
                    return False
            self._db.store_trade(d)
            return True
        except Exception as e:
            logger.warning(f"_maybe_persist_trade: store failed for {d.get('symbol')}: {e}")
            return False

    def _persist_state_after_event(self):
        """Persist cash + mark-to-cost equity to the equity_curve table.

        Called atomically after every cash mutation in `open_position` and
        `close_position` so a daemon death never leaves the DB with stale
        cash relative to the open-positions table. Falls back to mark-to-cost
        for equity (Portfolio doesn't have current LTPs); the next periodic
        `_snapshot_equity` call from TradingAgent will refresh with mark-to-
        market values.
        """
        if self._db is None:
            return
        try:
            equity_at_cost = self.cash + sum(
                p.entry_price * p.quantity for p in self.positions.values()
            )
            self._db.store_equity_point(
                equity=equity_at_cost,
                cash=self.cash,
                positions=len(self.positions),
            )
        except Exception as e:
            logger.error(f"Failed to persist post-event state: {e}")

    def _log_trade(self, record: TradeRecord):
        try:
            with open(self._trade_log_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    record.symbol, record.side, record.entry_price, record.exit_price,
                    record.quantity, record.entry_time.isoformat(), record.exit_time.isoformat(),
                    round(record.pnl, 2), round(record.pnl_pct, 2), record.strategy,
                    record.exit_reason, round(record.commission, 2),
                ])
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def get_total_value(self, current_prices: Dict[str, float]) -> float:
        """Total portfolio value = cash + unrealized equity of open positions.

        For a LONG position, equity = qty * current_price (we own the shares).
        For a SHORT position, the sell-proceeds are already reserved as cash
        collateral on open, so equity = entry_value + unrealized_pnl
        (collateral + mark-to-market PnL).
        """
        total_position_equity = 0.0
        for p in self.positions.values():
            mkt = current_prices.get(p.symbol, p.entry_price)
            if p.side == "BUY":
                total_position_equity += p.quantity * mkt
            else:
                total_position_equity += (
                    p.entry_price * p.quantity + p.unrealized_pnl(mkt)
                )
        return self.cash + total_position_equity

    def get_unrealized_pnl(self, current_prices: Dict[str, float]) -> float:
        return sum(
            p.unrealized_pnl(current_prices.get(p.symbol, p.entry_price))
            for p in self.positions.values()
        )

    def get_realized_pnl(self) -> float:
        return sum(t.pnl for t in self.trade_history)

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    def get_performance_metrics(self) -> dict:
        """Compute comprehensive performance metrics from trade history."""
        if not self.trade_history:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_win": 0.0,
                "max_loss": 0.0,
                "profit_factor": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
            }

        pnls = [t.pnl for t in self.trade_history]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_pnl = sum(pnls)
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0

        # Sharpe ratio (annualized, assuming ~250 trading days)
        pnl_series = pd.Series(pnls)
        sharpe = 0.0
        if pnl_series.std() > 0:
            sharpe = (pnl_series.mean() / pnl_series.std()) * (250 ** 0.5)

        # Max drawdown from cumulative P&L
        cum_pnl = pnl_series.cumsum()
        running_max = cum_pnl.cummax()
        drawdowns = running_max - cum_pnl
        max_dd = drawdowns.max()
        max_dd_pct = (max_dd / (self.initial_balance + running_max.max())) * 100 if running_max.max() > 0 else 0

        return {
            "total_trades": len(pnls),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(pnls), 2),
            "max_win": round(max(wins), 2) if wins else 0.0,
            "max_loss": round(min(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
        }

    def get_summary(self, current_prices: Optional[Dict[str, float]] = None) -> dict:
        prices = current_prices or {}
        return {
            "cash": round(self.cash, 2),
            "total_value": round(self.get_total_value(prices), 2),
            "unrealized_pnl": round(self.get_unrealized_pnl(prices), 2),
            "realized_pnl": round(self.get_realized_pnl(), 2),
            "open_positions": self.open_position_count,
            "total_trades": len(self.trade_history),
            "metrics": self.get_performance_metrics(),
        }
