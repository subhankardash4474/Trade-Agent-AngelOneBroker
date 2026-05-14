"""
Execution Engine Module (Enhanced)
Handles order placement via AngelOne/Kite SmartAPI with bracket orders,
trailing stop-loss updates, partial fill handling, and retry logic.
"""

import time
import uuid
from datetime import datetime
from typing import Optional

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

        self._order_log: list[dict] = []
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
        """Update the stop-loss on a pending SL order (for trailing stops)."""
        if self.mode == "paper":
            if order_id in self._pending_orders:
                self._pending_orders[order_id]["stop_loss_price"] = new_sl
                logger.debug(f"[PAPER] SL updated to {new_sl:.2f} for {order_id}")
                return True
            return False

        if self._api is None:
            return False

        try:
            self._api.modifyOrder({
                "variety": "NORMAL",
                "orderid": order_id,
                "triggerprice": str(new_sl),
            })
            logger.info(f"SL modified: {order_id} → {new_sl:.2f}")
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

                    return result

            except Exception as e:
                logger.error(f"Order failed (attempt {attempt}): {e}")

            if attempt < self.retry_attempts:
                time.sleep(self.retry_delay)

        logger.error(f"Order FAILED after {self.retry_attempts} attempts")
        return None

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

    @property
    def order_history(self) -> list[dict]:
        return list(self._order_log)

    @property
    def is_paper_mode(self) -> bool:
        return self.mode == "paper"
