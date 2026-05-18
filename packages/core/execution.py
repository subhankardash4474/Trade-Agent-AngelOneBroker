"""
Execution Engine Module (Enhanced)
Handles order placement via AngelOne/Kite SmartAPI with bracket orders,
trailing stop-loss updates, partial fill handling, and retry logic.
"""

import time
import uuid
from datetime import datetime
from typing import Dict, Optional

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")


class ExecutionEngine:
    """
    Order execution layer supporting paper and live trading.

    Enhancements over v1:
      - Bracket order support (entry + SL + target in one shot).
      - Trailing stop-loss order updates.
      - Partial fill detection and handling.
      - Slippage tracking (expected vs actual fill price).
    """

    VALID_ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
    VALID_PRODUCT_TYPES = ("INTRADAY", "DELIVERY", "MARGIN")
    VALID_TRANSACTION_TYPES = ("BUY", "SELL")

    def __init__(self, config: dict, smart_api=None, database=None):
        self._config = config
        self._api = smart_api
        self._db = database
        exec_cfg = config.get("execution", {})
        broker_cfg = config.get("broker", {})

        self.mode = broker_cfg.get("mode", "paper")
        # Shadow mode: do not actually place orders, but log intent.
        # Useful for validating a new config/model before risking capital.
        # If true, place_order returns a SHADOW result and the portfolio
        # layer can skip opening the position (caller decides).
        self.shadow_mode: bool = bool(exec_cfg.get("shadow_mode", False))
        self.order_type = exec_cfg.get("order_type", "LIMIT")
        self.product_type = exec_cfg.get("product_type", "INTRADAY")
        self.retry_attempts = exec_cfg.get("retry_attempts", 3)
        self.retry_delay = exec_cfg.get("retry_delay_seconds", 2)
        self.slippage_tolerance = exec_cfg.get("slippage_tolerance_pct", 0.1)
        self.exchange = config.get("market", {}).get("exchange", "NSE")

        # Partial-fill simulation (paper mode). Large orders have a small
        # probability of partial fill; low-volume symbols are more at risk.
        # Controlled by execution.paper_partial_fill_prob in config (0.0–1.0).
        self.partial_fill_prob: float = exec_cfg.get("paper_partial_fill_prob", 0.0)
        self.partial_fill_min_ratio: float = exec_cfg.get("paper_partial_fill_min_ratio", 0.5)

        # P3 polish (2026-05-17): bounded deque so a multi-month daemon
        # doesn't slowly leak memory. The OLD ``list`` grew unbounded; on
        # a heavy-trading day the order log alone could hit a few MB of
        # dicts. 50,000 entries is plenty (covers ~5 years at the current
        # rate) and lookup is O(n) anyway since we iterate.
        from collections import deque as _deque
        self._order_log: _deque = _deque(maxlen=50_000)
        self._pending_orders: dict[str, dict] = {}
        # 2026-05-14 LIVE-MODE SAFETY -----------------------------------
        # Track per-symbol broker-side SL-M order id + last trigger price.
        # Without this, every close path leaves the entry-time SL-M as an
        # orphan on the broker -- if LTP later touches that trigger, an
        # unintended reverse position opens. We cancel on every close and
        # propagate trail-SL updates via `modify_stop_loss`.
        # Schema:
        #     {symbol: {"order_id": str, "trigger": float, "side": "BUY"|"SELL"}}
        self._sl_orders_by_symbol: dict[str, dict] = {}

    def place_order(
        self,
        symbol: str,
        token: str,
        transaction_type: str,
        quantity: int,
        price: float,
        order_type: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        tag: str = "",
    ) -> Optional[dict]:
        """Place an order with retry logic. Returns order result or None."""
        if transaction_type not in self.VALID_TRANSACTION_TYPES:
            logger.error(f"Invalid transaction type: {transaction_type}")
            return None
        if quantity <= 0:
            logger.error(f"Invalid quantity: {quantity}")
            return None

        otype = order_type or self.order_type

        # Shadow mode short-circuit. The intent is recorded, but no broker
        # call or simulated fill occurs; the returned dict flags the caller
        # so it can choose NOT to update the portfolio/cash/DB.
        if self.shadow_mode:
            return self._shadow_order(symbol, token, transaction_type, quantity, price, otype, tag, stop_loss, take_profit)

        if self.mode == "paper":
            return self._paper_order(symbol, token, transaction_type, quantity, price, otype, tag, stop_loss, take_profit)

        return self._live_order_with_retry(
            symbol, token, transaction_type, quantity, price, otype, stop_loss, take_profit, tag
        )

    def place_bracket_order(
        self,
        symbol: str,
        token: str,
        transaction_type: str,
        quantity: int,
        price: float,
        stop_loss: float,
        target: float,
        trailing_sl: Optional[float] = None,
        tag: str = "",
    ) -> Optional[dict]:
        """
        Place a bracket order (entry + SL + target).
        In paper mode, simulates as three linked orders.
        """
        if self.mode == "paper":
            order = self._paper_order(symbol, token, transaction_type, quantity, price, "LIMIT", tag, stop_loss, target)
            if order:
                order["bracket"] = True
                order["stop_loss_price"] = stop_loss
                order["target_price"] = target
                order["trailing_sl"] = trailing_sl
            return order

        if self._api is None:
            logger.error("SmartAPI not initialized for bracket order")
            return None

        try:
            params = {
                "variety": "ROBO",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": transaction_type,
                "exchange": self.exchange,
                "ordertype": "LIMIT",
                "producttype": "BO",
                "duration": "DAY",
                "price": str(price),
                "quantity": str(quantity),
                "squareoff": str(abs(target - price)),
                "stoploss": str(abs(price - stop_loss)),
            }
            if trailing_sl is not None:
                params["trailingstoploss"] = str(trailing_sl)

            response = self._api.placeOrder(params)
            if response:
                result = {
                    "order_id": response,
                    "status": "PLACED",
                    "symbol": symbol,
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "requested_price": price,
                    "stop_loss_price": stop_loss,
                    "target_price": target,
                    "bracket": True,
                    "mode": "live",
                    "timestamp": datetime.now(IST).isoformat(),
                    "tag": tag,
                }
                self._order_log.append(result)
                self._pending_orders[response] = result
                logger.info(f"[LIVE] Bracket order placed: {response}")
                return result
        except Exception as e:
            logger.error(f"Bracket order failed: {e}")

        return None

    def modify_stop_loss(self, order_id: str, new_sl: float) -> bool:
        """Update the stop-loss on a pending SL order (for trailing stops).

        P1 #12 (2026-05-17) -- LIVE-MODE SAFETY: Angel SmartAPI returns
        HTTP 200 with ``{"status": false, "message": "..."}`` on most
        validation failures (insufficient margin change, order in terminal
        state, invalid trigger). The OLD code treated absence of an exception
        as success and logged "SL modified" while the broker order was
        unchanged. Trail SL only existed in RAM. Now we parse the response.
        """
        if self.mode == "paper":
            if order_id in self._pending_orders:
                self._pending_orders[order_id]["stop_loss_price"] = new_sl
                logger.debug(f"[PAPER] SL updated to {new_sl:.2f} for {order_id}")
                return True
            return False

        if self._api is None:
            return False

        try:
            response = self._api.modifyOrder({
                "variety": "NORMAL",
                "orderid": order_id,
                "triggerprice": str(new_sl),
            })
            # Two shapes we accept as success:
            #   1) Truthy non-dict (legacy SDK returns the new order id as a
            #      bare string -- still success).
            #   2) Dict with status truthy. Anything else (status=false,
            #      empty dict, None) is treated as failure.
            if isinstance(response, dict):
                status_ok = bool(response.get("status"))
                if not status_ok:
                    logger.error(
                        f"Failed to modify SL for {order_id}: broker rejected "
                        f"with status=false (message={response.get('message')!r}). "
                        f"Trail SL update did NOT propagate to broker."
                    )
                    return False
            elif not response:
                logger.error(
                    f"Failed to modify SL for {order_id}: broker returned "
                    f"empty/None response. Trail SL update did NOT propagate."
                )
                return False
            logger.info(f"SL modified: {order_id} \u2192 {new_sl:.2f}")
            return True
        except Exception as e:
            logger.error(f"Failed to modify SL for {order_id}: {e}")
            return False

    def _paper_order(
        self, symbol: str, token: str, tx_type: str,
        quantity: int, price: float, order_type: str, tag: str,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    ) -> dict:
        """Simulate order execution locally with realistic slippage and
        (optionally) partial fills to better approximate live market depth."""
        order_id = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        now = datetime.now(IST)

        # Slippage: BUY fills slightly higher, SELL slightly lower
        import random
        slip_pct = random.uniform(0.0, self.slippage_tolerance) / 100
        if tx_type == "BUY":
            filled_price = round(price * (1 + slip_pct), 2)
        else:
            filled_price = round(price * (1 - slip_pct), 2)
        slippage = round(abs(filled_price - price), 2)

        # Partial-fill simulation. In real markets, LIMIT orders may fill only
        # partially if the stock is thin or you're asking for more than the
        # visible liquidity. Disabled by default (prob=0) to keep backtests
        # reproducible — enable via config when you want stress testing.
        filled_quantity = quantity
        status = "FILLED"
        if (
            self.partial_fill_prob > 0
            and order_type == "LIMIT"
            and quantity > 1
            and random.random() < self.partial_fill_prob
        ):
            min_ratio = max(0.0, min(1.0, self.partial_fill_min_ratio))
            filled_ratio = random.uniform(min_ratio, 1.0)
            filled_quantity = max(1, int(quantity * filled_ratio))
            if filled_quantity < quantity:
                status = "PARTIALLY_FILLED"

        result = {
            "order_id": order_id,
            "status": status,
            "symbol": symbol,
            "token": token,
            "transaction_type": tx_type,
            "quantity": quantity,
            "filled_quantity": filled_quantity,
            "order_type": order_type,
            "requested_price": price,
            "filled_price": filled_price,
            "exchange": self.exchange,
            "timestamp": now.isoformat(),
            "mode": "paper",
            "tag": tag,
            "stop_loss_price": stop_loss,
            "target_price": take_profit,
            "slippage": slippage,
            "bracket": False,
        }
        self._order_log.append(result)
        self._pending_orders[order_id] = result
        self._persist_order(result)
        if status == "PARTIALLY_FILLED":
            logger.warning(
                f"[PAPER-PARTIAL] {tx_type} {filled_quantity}/{quantity} x {symbol} "
                f"@ Rs {filled_price:.2f} | {order_id}"
            )
        else:
            logger.info(
                f"[PAPER] {tx_type} {quantity} x {symbol} @ Rs {filled_price:.2f} "
                f"(req={price:.2f}, slip={slippage:.2f}) | {order_id}"
            )
        return result

    def _shadow_order(
        self, symbol: str, token: str, tx_type: str,
        quantity: int, price: float, order_type: str, tag: str,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    ) -> dict:
        """Log would-be order without touching the broker or portfolio.
        Used for validating a new config in production before going live."""
        order_id = f"SHADOW-{uuid.uuid4().hex[:12].upper()}"
        result = {
            "order_id": order_id,
            "status": "SHADOW",
            "symbol": symbol,
            "token": token,
            "transaction_type": tx_type,
            "quantity": quantity,
            "filled_quantity": 0,
            "order_type": order_type,
            "requested_price": price,
            "filled_price": None,
            "exchange": self.exchange,
            "timestamp": datetime.now(IST).isoformat(),
            "mode": "shadow",
            "tag": tag,
            "stop_loss_price": stop_loss,
            "target_price": take_profit,
            "slippage": None,
            "bracket": False,
        }
        self._order_log.append(result)
        self._persist_order(result)
        logger.info(
            f"[SHADOW] WOULD {tx_type} {quantity} x {symbol} @ Rs {price:.2f} "
            f"SL={stop_loss} TP={take_profit} | {order_id}"
        )
        return result

    def _persist_order(self, result: dict) -> None:
        """Write an order record to the DB ledger (audit trail)."""
        if self._db is None:
            return
        try:
            self._db.save_order(result)
        except Exception as e:
            logger.debug(f"Order ledger persist failed: {e}")

    def _live_order_with_retry(
        self, symbol: str, token: str, tx_type: str,
        quantity: int, price: float, order_type: str,
        stop_loss: Optional[float], take_profit: Optional[float], tag: str,
    ) -> Optional[dict]:
        """Place a live order through AngelOne with retry logic."""
        if self._api is None:
            logger.error("SmartAPI not initialized for live trading")
            return None

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": tx_type,
            "exchange": self.exchange,
            "ordertype": order_type,
            "producttype": self.product_type,
            "duration": "DAY",
            "quantity": str(quantity),
        }
        if order_type == "LIMIT":
            order_params["price"] = str(price)
        if stop_loss is not None and order_type in ("SL", "SL-M"):
            order_params["triggerprice"] = str(stop_loss)

        for attempt in range(1, self.retry_attempts + 1):
            try:
                logger.info(f"Placing order (attempt {attempt}/{self.retry_attempts}): {tx_type} {quantity} x {symbol}")
                response = self._api.placeOrder(order_params)

                if response:
                    result = {
                        "order_id": response,
                        "status": "PLACED",
                        "symbol": symbol,
                        "token": token,
                        "transaction_type": tx_type,
                        "quantity": quantity,
                        "order_type": order_type,
                        "requested_price": price,
                        "filled_price": None,
                        "exchange": self.exchange,
                        "timestamp": datetime.now(IST).isoformat(),
                        "mode": "live",
                        "tag": tag,
                        "slippage": None,
                    }
                    self._order_log.append(result)
                    self._pending_orders[response] = result
                    self._persist_order(result)
                    logger.info(f"[LIVE] Order placed: {response}")

                    # Place SL order after entry (opposite side). Track its
                    # id by symbol so we can cancel/modify it on close/trail.
                    if stop_loss is not None:
                        sl_side = "SELL" if tx_type == "BUY" else "BUY"
                        sl_id = self._place_sl_order(
                            symbol, token, quantity, stop_loss, sl_side
                        )
                        if sl_id:
                            self._sl_orders_by_symbol[symbol] = {
                                "order_id": sl_id,
                                "trigger": float(stop_loss),
                                "side": sl_side,
                                "quantity": quantity,
                                "token": token,
                            }
                        else:
                            # P0 #3 (2026-05-15) — LIVE-MODE SAFETY:
                            # entry order succeeded but SL placement failed
                            # (margin glitch, API throttle, rejection). Prior
                            # code would return ``result`` and the caller
                            # would record an "OK entry" in the portfolio
                            # while broker reality is a NAKED position. Single
                            # API hiccup = unhedged exposure with uncapped
                            # downside until we (or RMS) notice.
                            #
                            # Rollback strategy: immediately place a counter
                            # market order to flatten. Log CRITICAL so the
                            # operator sees the rollback in the daemon log
                            # even if the trading agent later reports the
                            # entry as "failed". Counter-flatten over cancel
                            # because the entry may already be filled and a
                            # cancelOrder on a filled order is a no-op.
                            self._entry_sl_rollback(
                                symbol=symbol, token=token,
                                entry_order_id=response,
                                entry_tx=tx_type, quantity=quantity,
                                requested_stop_loss=stop_loss,
                            )
                            return None

                    return result

            except Exception as e:
                logger.error(f"Order failed (attempt {attempt}): {e}")

            if attempt < self.retry_attempts:
                time.sleep(self.retry_delay)

        logger.error(f"Order FAILED after {self.retry_attempts} attempts")
        return None

    def _entry_sl_rollback(
        self,
        *,
        symbol: str,
        token: str,
        entry_order_id: str,
        entry_tx: str,
        quantity: int,
        requested_stop_loss: float,
    ) -> None:
        """P0 #3 (2026-05-15) — emergency rollback when entry placed but
        the SL leg failed.

        Counter-flatten with a market order on the OPPOSITE side. Cleans
        the order-log tracking artifacts of the failed compound so the
        caller treats the entry as if it never happened. The original
        entry's DB ledger row is intentionally retained so the rollback
        attempt is auditable post-mortem.
        """
        logger.critical(
            f"[ENTRY-ROLLBACK] {symbol}: entry order {entry_order_id} placed "
            f"but SL placement FAILED (requested trigger \u20B9{requested_stop_loss:.2f}). "
            f"Initiating counter-flatten to avoid naked position."
        )
        counter_side = "SELL" if entry_tx == "BUY" else "BUY"
        try:
            flatten_params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": counter_side,
                "exchange": self.exchange,
                "ordertype": "MARKET",
                "producttype": self.product_type,
                "duration": "DAY",
                "quantity": str(quantity),
            }
            counter_id = self._api.placeOrder(flatten_params)
            if counter_id:
                logger.critical(
                    f"[ENTRY-ROLLBACK] {symbol}: counter-flatten {counter_side} "
                    f"x{quantity} placed (order {counter_id}). Net broker exposure "
                    f"should be zero. Manual reconciliation still recommended."
                )
            else:
                logger.critical(
                    f"[ENTRY-ROLLBACK] {symbol}: broker returned empty id for "
                    f"counter-flatten. POSITION MAY BE NAKED. INTERVENE NOW."
                )
        except Exception as e:
            logger.critical(
                f"[ENTRY-ROLLBACK] {symbol}: counter-flatten FAILED ({e}). "
                f"POSITION IS NAKED AT BROKER. IMMEDIATE INTERVENTION REQUIRED."
            )

        # Remove the failed compound's tracking artifacts so the caller's
        # in-memory state matches "entry never succeeded" semantics.
        try:
            if self._order_log and self._order_log[-1].get("order_id") == entry_order_id:
                self._order_log.pop()
        except Exception:
            pass
        self._pending_orders.pop(entry_order_id, None)

    def _place_sl_order(self, symbol: str, token: str, quantity: int,
                        trigger_price: float, tx_type: str) -> Optional[str]:
        """Place a stop-loss market order as protection.

        Returns the broker-assigned order id (str) on success, or None.
        Caller is responsible for storing the returned id so subsequent
        trail-SL updates can call `modify_stop_loss` and close paths can
        call `cancel_sl_order_for_symbol`.
        """
        if self._api is None:
            return None
        try:
            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": tx_type,
                "exchange": self.exchange,
                "ordertype": "SL-M",
                "producttype": self.product_type,
                "duration": "DAY",
                "quantity": str(quantity),
                "triggerprice": str(trigger_price),
            }
            sl_order_id = self._api.placeOrder(params)
            if sl_order_id:
                logger.info(f"SL order placed: {sl_order_id} @ trigger \u20B9{trigger_price:.2f}")
                return sl_order_id
        except Exception as e:
            logger.error(f"SL order failed: {e}")
        return None

    # ── 2026-05-14 LIVE-MODE SL TRACKING API ─────────────────────────

    def get_sl_order_for_symbol(self, symbol: str) -> Optional[dict]:
        """Return the {order_id, trigger, side, quantity, token} dict for
        the active broker-side SL-M tied to ``symbol``, or None."""
        return self._sl_orders_by_symbol.get(symbol)

    def update_sl_trigger_for_symbol(self, symbol: str, new_trigger: float) -> bool:
        """Modify the broker-side SL trigger so it matches the trail SL.

        Without this, the trail SL only existed in the agent's memory --
        a daemon crash mid-trail would leave the broker enforcing the
        original (much wider) SL. Idempotent: if the new trigger equals
        the cached trigger, we don't bother the broker.

        Returns True on success / no-op, False on broker rejection.
        """
        meta = self._sl_orders_by_symbol.get(symbol)
        if not meta:
            return False
        # Idempotency: skip if the trigger hasn't actually moved.
        try:
            if abs(float(new_trigger) - float(meta.get("trigger", 0.0))) < 1e-6:
                return True
        except (TypeError, ValueError):
            pass
        ok = self.modify_stop_loss(meta["order_id"], float(new_trigger))
        if ok:
            meta["trigger"] = float(new_trigger)
            self._sl_orders_by_symbol[symbol] = meta
        return ok

    def cancel_sl_order_for_symbol(self, symbol: str) -> bool:
        """Cancel the active broker-side SL-M for ``symbol`` and forget it.

        Called on every close path (signal-exit, trailing, peak-giveback,
        carryover-lock, square-off-all) so an in-process close cannot leave
        an orphaned SL-M that fires later and opens an unintended reverse
        position. Cheap no-op when no SL is tracked (paper mode, or symbol
        was never placed live).

        Returns True on success / no-op, False on broker rejection.
        """
        meta = self._sl_orders_by_symbol.pop(symbol, None)
        if not meta:
            return True   # nothing to cancel
        order_id = meta.get("order_id")
        if not order_id:
            return True
        try:
            ok = self.cancel_order(order_id, variety="NORMAL")
            if not ok:
                # Re-track so a later retry has the id; without this, a
                # transient broker hiccup would silently abandon an active
                # standing order.
                self._sl_orders_by_symbol[symbol] = meta
                logger.error(
                    f"[SL-CANCEL] Broker refused cancel for {symbol} "
                    f"(order {order_id}); re-tracking. Manual intervention may be needed."
                )
            else:
                logger.info(f"[SL-CANCEL] {symbol} broker SL {order_id} cancelled")
            return ok
        except Exception as e:
            self._sl_orders_by_symbol[symbol] = meta
            logger.error(f"[SL-CANCEL] {symbol} failed: {e}")
            return False

    def list_tracked_sl_orders(self) -> dict:
        """Defensive introspection. Used by the heartbeat / health check
        to flag any SL we believe is active on the broker."""
        return dict(self._sl_orders_by_symbol)

    # P0 #4 (2026-05-15) — LIVE-MODE SAFETY: restart reconciliation.
    #
    # When the daemon restarts mid-session with open positions, the portfolio
    # is rehydrated from the DB but `_sl_orders_by_symbol` is empty in this
    # fresh ExecutionEngine instance. The broker, however, still has the
    # original SL-M orders live (broker state outlasts our process). Without
    # this reconciliation step:
    #   • `update_sl_trigger_for_symbol` silently returns False — trail SL
    #     never propagates to the broker again.
    #   • `cancel_sl_order_for_symbol` silently returns True (treats the
    #     missing entry as "nothing to cancel") — so on the next close we
    #     leave an orphaned SL behind that can fire later as a reverse trade.
    #
    # This is the exact bug the SL-tracking PR was supposed to close,
    # reintroduced by ignoring the restart path.

    def reconcile_sl_orders_from_broker(
        self, restored_positions: dict
    ) -> dict:
        """Rebuild ``_sl_orders_by_symbol`` from the broker's order book.

        Two passes:

        1. Match-and-register pass — for each restored position, look for a
           live SL-M order on the same symbol whose ``transactiontype`` is
           opposite (the protective leg) and register the FIRST match. Log
           CRITICAL for any restored position with no matching broker SL.

        2. (P0 #3 residual, 2026-05-18) Orphan / duplicate sweep — any live
           SL-M on the broker that does NOT correspond to a restored
           position (orphan) OR is a SECOND matching SL-M for the same
           symbol (duplicate) is CANCELLED. Rationale: a stale SL-M can
           fire later and silently open an unintended reverse position
           against an empty book — the exact bug the registry was meant to
           kill. The most common cause is a daemon crash AFTER the entry
           order filled but BEFORE the DB persisted it.

        Args:
            restored_positions: {symbol: Position-like} as held by Portfolio.

        Returns:
            Dict {symbol: status} where status is one of
              ``reconciled`` — broker SL found and registered;
              ``unprotected`` — no SL-M found for this symbol;
              ``orphan_cancelled`` — SL-M on broker had no matching DB
                                     position; cancel issued OK;
              ``orphan_cancel_failed`` — orphan SL-M cancel rejected by
                                         broker; manual intervention needed;
              ``skipped_paper`` — paper mode, no broker to query.
        """
        report: dict[str, str] = {}
        if self.mode == "paper" or self._api is None:
            for sym in restored_positions:
                report[sym] = "skipped_paper"
            return report
        try:
            order_book = self._api.orderBook()
        except Exception as e:
            logger.error(
                f"[SL-RECONCILE] orderBook() call failed ({e}); all "
                f"restored positions remain UNPROTECTED in tracking. "
                f"Trail SL propagation will no-op until a restart with "
                f"working broker connectivity."
            )
            for sym in restored_positions:
                report[sym] = "unprotected"
                logger.critical(
                    f"[SL-RECONCILE] {sym}: unable to query broker, no SL "
                    f"registered. Manual reconciliation needed."
                )
            return report

        if not order_book or not order_book.get("status"):
            logger.warning(
                f"[SL-RECONCILE] orderBook() returned empty/false-status "
                f"payload: {order_book!r}"
            )
            for sym in restored_positions:
                report[sym] = "unprotected"
            return report

        # Build a symbol -> [orders] index restricted to live SL-M legs.
        # Angel's order statuses: "trigger pending", "open", "open pending",
        # "validation pending" — we accept anything that isn't a terminal
        # state. The terminal states are "complete", "rejected", "cancelled".
        TERMINAL = {"complete", "rejected", "cancelled"}
        live_sl_orders: dict[str, list[dict]] = {}
        for order in order_book.get("data", []) or []:
            try:
                if (order.get("ordertype") or "").upper() != "SL-M":
                    continue
                status = (order.get("status") or "").lower()
                if status in TERMINAL:
                    continue
                sym = order.get("tradingsymbol")
                if not sym:
                    continue
                live_sl_orders.setdefault(sym, []).append(order)
            except Exception:
                continue

        # Pass 1: match a live SL-M against each restored position.
        # Track which order_ids we "used" so the orphan sweep can later
        # cancel everything left behind without re-cancelling our own
        # matched leg.
        used_order_ids: set = set()
        for sym, pos in restored_positions.items():
            candidates = live_sl_orders.get(sym, [])
            expected_side = "SELL" if getattr(pos, "side", "BUY") == "BUY" else "BUY"
            match = None
            for cand in candidates:
                if (cand.get("transactiontype") or "").upper() == expected_side:
                    match = cand
                    break
            if match is None:
                report[sym] = "unprotected"
                logger.critical(
                    f"[SL-RECONCILE] {sym}: position restored (side={pos.side}, "
                    f"qty={pos.quantity}, entry={pos.entry_price}) but no "
                    f"matching live SL-M order found on broker. POSITION IS "
                    f"UNPROTECTED. Trail SL updates will silently no-op."
                )
                continue
            try:
                trigger = float(match.get("triggerprice") or 0.0)
            except (TypeError, ValueError):
                trigger = 0.0
            matched_oid = match.get("orderid")
            self._sl_orders_by_symbol[sym] = {
                "order_id": matched_oid,
                "trigger": trigger,
                "side": expected_side,
                "quantity": int(float(match.get("quantity") or pos.quantity)),
                "token": match.get("symboltoken") or "",
            }
            used_order_ids.add(matched_oid)
            report[sym] = "reconciled"
            logger.info(
                f"[SL-RECONCILE] {sym}: re-registered broker SL "
                f"{matched_oid} @ trigger \u20B9{trigger:.2f}"
            )

        # Pass 2 (P0 #3 residual, 2026-05-18): sweep orphans + duplicates.
        # Anything left in `live_sl_orders` that we didn't use is either:
        #   • A duplicate SL-M for a symbol whose primary leg we matched
        #     in pass 1 (e.g. a previous bug double-placed; or the same
        #     symbol got two entries that both placed SL legs and only
        #     one survived in our DB).
        #   • An orphan SL-M for a symbol with no DB position at all
        #     (e.g. crash AFTER entry order filled but BEFORE the open
        #     was persisted; or the operator manually closed the
        #     position but the SL leg was never reaped).
        # Either way, leaving it standing means a stale SL-M can later
        # fire on the broker and silently open an unintended REVERSE
        # position against an empty book. Cancel both classes.
        n_orphan_ok = 0
        n_orphan_fail = 0
        n_dup_ok = 0
        n_dup_fail = 0
        for sym, orders in live_sl_orders.items():
            in_restored = sym in restored_positions
            for cand in orders:
                oid = cand.get("orderid")
                if not oid or oid in used_order_ids:
                    continue
                kind = "duplicate" if in_restored else "orphan"
                trigger = cand.get("triggerprice")
                tx = (cand.get("transactiontype") or "").upper()
                logger.critical(
                    f"[SL-RECONCILE] {sym}: {kind} live SL-M detected — "
                    f"order_id={oid} side={tx} trigger=\u20B9{trigger}. "
                    f"This was NOT registered to any restored position; "
                    f"if left standing it can fire later and open an "
                    f"unintended reverse position. Cancelling."
                )
                ok = False
                try:
                    ok = self.cancel_order(oid, variety="NORMAL")
                except Exception as e:
                    logger.error(
                        f"[SL-RECONCILE] {sym}: {kind} cancel raised {e!r}; "
                        f"order_id={oid} remains live at broker."
                    )
                if ok:
                    if kind == "orphan":
                        n_orphan_ok += 1
                        # Orphans aren't in restored_positions, so they
                        # surface in the report under their own key.
                        report[sym] = "orphan_cancelled"
                    else:
                        n_dup_ok += 1
                        # Duplicates: keep the "reconciled" status from
                        # pass 1 so callers still see the symbol as
                        # protected. The cancelled extra is in the log.
                else:
                    if kind == "orphan":
                        n_orphan_fail += 1
                        report[sym] = "orphan_cancel_failed"
                        logger.critical(
                            f"[SL-RECONCILE] {sym}: orphan SL-M CANCEL FAILED "
                            f"(order_id={oid}). MANUAL INTERVENTION REQUIRED — "
                            f"this order can still fire as a reverse trade."
                        )
                    else:
                        n_dup_fail += 1
                        logger.critical(
                            f"[SL-RECONCILE] {sym}: duplicate SL-M CANCEL FAILED "
                            f"(order_id={oid}). The primary SL leg is registered "
                            f"but the duplicate remains live."
                        )

        n_recon = sum(1 for v in report.values() if v == "reconciled")
        n_unprot = sum(1 for v in report.values() if v == "unprotected")
        logger.info(
            f"[SL-RECONCILE] complete: {n_recon} reconciled, "
            f"{n_unprot} unprotected, {n_orphan_ok} orphans cancelled "
            f"({n_orphan_fail} failed), {n_dup_ok} duplicates cancelled "
            f"({n_dup_fail} failed) (out of {len(restored_positions)} restored)"
        )
        return report

    def get_order_status(self, order_id: str) -> Optional[dict]:
        if self.mode == "paper":
            return self._pending_orders.get(order_id)
        if self._api is None:
            return None
        try:
            order_book = self._api.orderBook()
            if order_book and order_book.get("status"):
                for order in order_book.get("data", []):
                    if order.get("orderid") == order_id:
                        # Track slippage
                        filled_price = float(order.get("averageprice", 0))
                        pending = self._pending_orders.get(order_id, {})
                        requested = pending.get("requested_price", filled_price)
                        if filled_price > 0 and requested > 0:
                            pending["filled_price"] = filled_price
                            pending["slippage"] = round(abs(filled_price - requested), 2)
                        return order
        except Exception as e:
            logger.error(f"Failed to fetch order status: {e}")
        return None

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if self.mode == "paper":
            self._pending_orders.pop(order_id, None)
            logger.info(f"[PAPER] Order {order_id} cancelled")
            return True
        if self._api is None:
            return False
        try:
            response = self._api.cancelOrder(order_id, variety)
            if response:
                logger.info(f"Order {order_id} cancelled")
                return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
        return False

    def get_positions(self) -> Optional[dict]:
        if self._api is None:
            return None
        try:
            return self._api.position()
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return None

    def reconcile_positions_with_broker(
        self,
        restored_positions: Dict[str, Dict],
    ) -> Dict[str, Dict]:
        """P2 restart-cluster (2026-05-17): close DB-side positions that
        the broker has already flattened (RMS auto-flatten, manual close
        by the operator, broker margin call). Returns a report keyed by
        symbol with one of three statuses:

          * ``ok``      DB and broker agree the position is open.
          * ``orphan``  DB shows open, broker shows flat. Caller MUST
                        close the DB-side position with reason
                        ``broker_reconcile_at_boot``.
          * ``skipped`` broker call failed or paper mode -- caller leaves
                        the position as-is.

        We do NOT touch the portfolio here; the caller (TradingAgent)
        owns that. Keeps this concern at the daemon layer.
        """
        report: Dict[str, Dict] = {}
        if not restored_positions:
            return report
        if self.mode == "paper":
            for sym in restored_positions:
                report[sym] = {"status": "skipped", "reason": "paper_mode"}
            return report
        try:
            response = self.get_positions()
        except Exception as exc:  # noqa: BLE001 - never block boot on this
            logger.warning(
                f"[POSITION-RECONCILE] broker positionBook fetch raised: "
                f"{exc!r}. Skipping reconciliation -- positions left as DB."
            )
            for sym in restored_positions:
                report[sym] = {"status": "skipped", "reason": "api_error"}
            return report
        if not response:
            for sym in restored_positions:
                report[sym] = {"status": "skipped", "reason": "empty_response"}
            return report

        rows = response.get("data") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            for sym in restored_positions:
                report[sym] = {"status": "skipped", "reason": "unexpected_shape"}
            return report

        broker_qty_by_symbol: Dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = (row.get("tradingsymbol") or row.get("symbol") or "").strip()
            if not sym:
                continue
            qty_raw = row.get("netqty") or row.get("net_quantity") or "0"
            try:
                broker_qty_by_symbol[sym] = int(float(qty_raw))
            except (TypeError, ValueError):
                broker_qty_by_symbol[sym] = 0

        for sym, db_pos in restored_positions.items():
            broker_qty = broker_qty_by_symbol.get(sym, 0)
            if broker_qty == 0:
                logger.critical(
                    f"[POSITION-RECONCILE] {sym}: DB shows OPEN "
                    f"({db_pos.get('side')} qty={db_pos.get('quantity')}) "
                    f"but broker netqty=0. Likely RMS auto-flatten or "
                    f"manual close. Marking ORPHAN; caller will close "
                    f"the DB position with broker_reconcile_at_boot."
                )
                report[sym] = {
                    "status": "orphan",
                    "broker_netqty": broker_qty,
                    "db_quantity": db_pos.get("quantity"),
                }
            else:
                # Broker has a position too -- it's at least in the right
                # direction if broker_qty has the same sign as DB's side.
                report[sym] = {
                    "status": "ok",
                    "broker_netqty": broker_qty,
                    "db_quantity": db_pos.get("quantity"),
                }
        return report

    @property
    def order_history(self) -> list[dict]:
        return list(self._order_log)

    @property
    def is_paper_mode(self) -> bool:
        return self.mode == "paper"
