"""
Execution Engine Module (Enhanced)
Handles order placement via AngelOne/Kite SmartAPI with bracket orders,
trailing stop-loss updates, partial fill handling, and retry logic.
"""

import os
import random as _random_module
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")


# B-7 (audit 2026-05-25): paper-order slippage and partial-fill draws used the
# global `random` module, which has its own implicit state seeded from /dev/
# urandom at process start. Two consequences:
#   1. Backtests are NOT reproducible — replaying the same config + data
#      yields different fills each run.
#   2. The battery harness can't compare variants apples-to-apples.
#
# Module-level `_paper_rng` (a dedicated random.Random instance) plus
# `EXECUTION_PAPER_SEED` env var fixes both. When the env var is unset we
# fall back to the legacy random behaviour (urandom-seeded) so live paper
# users see no behaviour change unless they opt in.
_PAPER_SEED_ENV = "EXECUTION_PAPER_SEED"
_paper_rng = _random_module.Random()
_seed_value_raw = os.environ.get(_PAPER_SEED_ENV)
if _seed_value_raw is not None:
    try:
        _paper_rng.seed(int(_seed_value_raw))
    except ValueError:
        _paper_rng.seed(_seed_value_raw)


def _set_paper_seed(seed: Optional[int]) -> None:
    """Test/Backtest hook: re-seed (or unseed) the paper-order RNG.

    Called by `backtest_ensemble.BacktestConfig.seed` and by unit tests
    that need deterministic fills (B-7). Passing `None` re-randomises.
    """
    if seed is None:
        _paper_rng.seed()
    else:
        _paper_rng.seed(int(seed))


# Strings that AngelOne / Kite are known (or likely) to emit as a stringy
# version of a boolean status field. Maintained at module scope so the
# table is auditable. Lower-cased for the comparison.
_BROKER_FALSE_STRINGS = frozenset({
    "false", "0", "no", "fail", "failed", "rejected", "error",
})
_BROKER_TRUE_STRINGS = frozenset({
    "true", "1", "yes", "ok", "okay", "success", "successful",
})


def _interpret_broker_status(value: object) -> bool:
    """Interpret a broker response's ``status`` field as a strict boolean.

    Why this exists: AngelOne historically sends ``status`` as a Python
    bool, but production traces have shown it occasionally returns the
    STRING ``"false"`` (e.g. on Modify rejections that the broker chose
    to wrap rather than raise). The naive ``bool(value)`` check passes
    ``"false"`` as truthy because every non-empty string is truthy in
    Python -- which means the agent celebrated a "SL modified" the
    broker had just rejected.

    Contract:
      * ``True`` / ``"true"`` / ``"ok"`` / ``"success"`` / ``"1"`` / etc.
        -> True (success).
      * ``False`` / ``"false"`` / ``"0"`` / ``"no"`` / ``"fail"`` / etc.
        -> False (failure).
      * ``None`` / empty string -> False (defensive).
      * Numeric: non-zero -> True, zero -> False (mirrors stdlib bool).
      * Any other type: ``bool(value)`` as a last-resort fallback.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return False
        if s in _BROKER_FALSE_STRINGS:
            return False
        if s in _BROKER_TRUE_STRINGS:
            return True
        # Unknown string -- be conservative and treat as failure so a
        # silent broker contract change surfaces as a no-op rather than
        # a false success. Logging is the caller's responsibility.
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


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

        # ── Phase 2 (audit 2026-05-28) ────────────────────────────────
        # ORD-01 / STATE-01: wait-for-terminal timing knobs. Pre-fix
        # ``_live_order_with_retry`` returned immediately on
        # ``placeOrder`` response with ``status=="PLACED"`` -- the
        # caller then opened a portfolio position using the signal-time
        # price as the "fill price" without waiting to know whether
        # the order actually filled or what price it filled at. The
        # poll loop below blocks for at most ``live_order_fill_timeout_sec``
        # and polls every ``live_order_fill_poll_interval_sec``. The
        # defaults are conservative (10s / 1s) so the live path
        # blocks no more than 10s per entry; intra-cycle latency
        # budget is comfortably above that.
        self.live_order_fill_timeout_sec: float = float(
            exec_cfg.get("live_order_fill_timeout_sec", 10.0)
        )
        self.live_order_fill_poll_interval_sec: float = float(
            exec_cfg.get("live_order_fill_poll_interval_sec", 1.0)
        )
        # ORD-02 idempotency window: when a retry kicks in, scan the
        # broker orderBook for an order matching (symbol, side, qty,
        # ordertype) that was placed in the last N seconds and reuse
        # its id instead of placing a duplicate.
        self.idempotency_lookback_sec: float = float(
            exec_cfg.get("idempotency_lookback_sec", 30.0)
        )
        # OBS-05 / STATE-02 boot-reconcile state. Set True (in live
        # mode) when the boot ``positionBook()`` fetch fails all of
        # its retries OR when a broker-only symbol is detected at
        # boot. Caller (TradingAgent) checks this before allowing
        # any new entry. Cleared only by an operator-touched ack
        # file (``logs/boot_reconcile.ack``) so a flaky transient
        # API blip cannot silently re-enable trading mid-session.
        self.boot_reconcile_failed_live: bool = False
        self.boot_reconcile_failure_reason: Optional[str] = None

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
            #
            # 2026-05-18 (P1 bool-trap): the old check was
            # ``bool(response.get("status"))`` which had a silent bug --
            # ``bool("false") == True`` in Python because any non-empty
            # string is truthy. So when AngelOne returned ``{"status":
            # "false", "message": "Modify rejected"}`` we mis-read it as
            # success, logged "SL modified" and never re-tried. Now an
            # explicit case-insensitive string check rejects "false",
            # "0", "no", "fail", and "failed" while still accepting True,
            # "true", "ok", "success".
            if isinstance(response, dict):
                if not _interpret_broker_status(response.get("status")):
                    logger.error(
                        f"Failed to modify SL for {order_id}: broker rejected "
                        f"with status={response.get('status')!r} "
                        f"(message={response.get('message')!r}). "
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

            # 2026-05-18 (P1 orderBook confirm): fetch the order book and
            # verify the trigger price actually got updated to new_sl.
            # AngelOne has been observed to return success but silently
            # ignore the modify (RMS soft-reject, throttling). A mismatch
            # surfaces as a WARNING (we still return True because the
            # modify-response said ok and the trail might just be ahead of
            # the orderBook snapshot), but the warning gives operator
            # visibility into a sustained propagation gap.
            try:
                self._verify_sl_modify_propagated(order_id, new_sl)
            except Exception as verify_exc:  # noqa: BLE001 - best effort
                logger.debug(
                    f"[SL-MODIFY-VERIFY] {order_id}: post-modify orderBook "
                    f"check raised {verify_exc!r}; trusting modify response."
                )
            return True
        except Exception as e:
            logger.error(f"Failed to modify SL for {order_id}: {e}")
            return False

    def _verify_sl_modify_propagated(
        self, order_id: str, expected_trigger: float
    ) -> None:
        """Sanity-check that a successful modify actually changed the trigger.

        Logs WARNING on mismatch; never raises and never blocks the
        caller's success path. The modify-response is still the source
        of truth -- this is purely a forensic / monitoring signal."""
        if self._api is None:
            return
        try:
            order_book = self._api.orderBook()
        except Exception as exc:
            # OBS-11 (audit 2026-05-28): pre-fix this was a bare ``return``
            # with no log. AngelOne RMS soft-rejects (documented inline
            # in the modify path) can leave the trigger price stale on
            # the broker side while our local state thinks it's been
            # updated. A failed orderBook() fetch here means we cannot
            # verify the modify -- log loudly so the operator can spot
            # cases where SL-M is drifting away from intended trigger.
            logger.warning(
                f"[SL-verify] orderBook() raised while verifying "
                f"order_id={order_id} expected_trigger={expected_trigger}: "
                f"{type(exc).__name__}: {exc!r}. Trigger may be stale broker-side."
            )
            return
        if not isinstance(order_book, dict):
            return
        for order in order_book.get("data", []) or []:
            if not isinstance(order, dict):
                continue
            if order.get("orderid") != order_id:
                continue
            try:
                live_trigger = float(order.get("triggerprice") or 0)
            except (TypeError, ValueError):
                return
            if live_trigger <= 0:
                return
            # Compare to within 1 paisa to tolerate float jitter.
            if abs(live_trigger - expected_trigger) > 0.01:
                logger.warning(
                    f"[SL-MODIFY-VERIFY] {order_id}: broker accepted "
                    f"modify but orderBook trigger is {live_trigger:.2f}, "
                    f"expected {expected_trigger:.2f}. Possible RMS "
                    f"soft-reject or stale snapshot; will reconcile on "
                    f"next mutation."
                )
            return

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
        # B-7 (audit 2026-05-25): use the seedable module RNG (`_paper_rng`)
        # instead of the global `random` so backtests are reproducible and
        # battery variants can be compared apples-to-apples. Live paper
        # behaviour unchanged when EXECUTION_PAPER_SEED is unset (the RNG
        # is urandom-seeded at module load).
        slip_pct = _paper_rng.uniform(0.0, self.slippage_tolerance) / 100
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
            and _paper_rng.random() < self.partial_fill_prob
        ):
            min_ratio = max(0.0, min(1.0, self.partial_fill_min_ratio))
            filled_ratio = _paper_rng.uniform(min_ratio, 1.0)
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
            # OBS-16 (audit 2026-05-28): pre-fix this was DEBUG which is
            # filtered out at the default file sink. A failed audit-trail
            # write deserves to be visible; the order itself still
            # executed, but downstream EOD reconciliation against the DB
            # ledger would silently miss this row.
            logger.warning(
                f"[order-ledger] persist failed for "
                f"order_id={result.get('order_id')} symbol={result.get('symbol')} "
                f"status={result.get('status')}: {type(e).__name__}: {e!r}"
            )

    # ─────────── ORD-01 / ORD-02 helpers (audit 2026-05-28) ───────────

    # AngelOne (and most brokers) return status strings that vary in
    # case, spelling, and tense. Normalise to lowercase and pin the
    # known set so a typo in the broker payload doesn't silently
    # downgrade us back to "treat as pending" behaviour. The lists
    # mirror the e2e tool tools/test_live_single_trade.py so the
    # poll-loop semantics are identical between e2e harness and prod.
    _TERMINAL_FILLED = frozenset(
        {"complete", "completed", "filled", "executed"}
    )
    _TERMINAL_PARTIAL = frozenset(
        {"partially_filled", "partial", "partially filled"}
    )
    _TERMINAL_CANCELLED = frozenset(
        {"cancelled", "canceled", "rejected"}
    )

    def _wait_for_terminal(
        self,
        order_id: str,
        timeout_sec: Optional[float] = None,
    ) -> Optional[dict]:
        """ORD-01/STATE-01 (audit 2026-05-28): poll the broker orderBook
        until ``order_id`` reaches a terminal state or ``timeout_sec``
        elapses.

        Returns the broker order row dict on terminal (any of FILLED,
        PARTIAL, CANCELLED, REJECTED) or ``None`` if the order was
        still pending at timeout / never seen in the book / broker
        call kept failing.

        Live-mode only. Paper / shadow shouldn't call this -- they
        synth-fill instantly inside ``_paper_order``.

        Why this exists: pre-fix, the live caller treated
        ``placeOrder`` returning an order_id as success and used the
        signal-time price as the "fill price" -- which is wrong
        for any LIMIT, wrong for any MARKET on a fast-moving symbol,
        and catastrophic for a slow-fill scenario where the
        portfolio reflects an open position but the broker order
        is still PENDING. Paper mode hides this entire failure mode
        (paper synth-fills instantly), which is exactly why the
        bug went uncaught.
        """
        if self.mode == "paper" or self._api is None:
            return None

        timeout = timeout_sec if timeout_sec is not None else self.live_order_fill_timeout_sec
        poll = max(0.1, self.live_order_fill_poll_interval_sec)
        deadline = time.time() + timeout
        last_seen_status: Optional[str] = None

        while time.time() < deadline:
            try:
                book = self._api.orderBook()
            except Exception as exc:
                logger.warning(
                    f"[WAIT-TERMINAL] orderBook() raised while polling "
                    f"{order_id}: {type(exc).__name__}: {exc!r}; retrying."
                )
                book = None
            order_row = self._find_order_in_book(book, order_id)
            if order_row is not None:
                status = self._normalise_status(order_row)
                last_seen_status = status
                if (
                    status in self._TERMINAL_FILLED
                    or status in self._TERMINAL_PARTIAL
                    or status in self._TERMINAL_CANCELLED
                ):
                    return order_row
            time.sleep(poll)

        logger.warning(
            f"[WAIT-TERMINAL] order_id={order_id} did not reach terminal "
            f"within {timeout:.1f}s; last_seen_status={last_seen_status!r}. "
            f"Caller will treat as PENDING_AT_TTL."
        )
        return None

    @staticmethod
    def _find_order_in_book(book: Any, order_id: str) -> Optional[dict]:
        """Locate ``order_id`` in a variably-shaped AngelOne orderBook
        response. Returns the matching row dict or None."""
        if not isinstance(book, dict):
            return None
        rows = book.get("data") or []
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (
                row.get("orderid") == order_id
                or row.get("orderID") == order_id
                or row.get("order_id") == order_id
            ):
                return row
        return None

    @staticmethod
    def _normalise_status(row: dict) -> str:
        """Return ``row``'s broker status, lowercased + stripped."""
        raw = row.get("status") or row.get("orderstatus") or "unknown"
        return str(raw).strip().lower()

    @staticmethod
    def _extract_avg_fill_price(row: dict) -> Optional[float]:
        """Parse the broker's average fill price from an orderBook row.
        AngelOne uses ``averageprice``; other brokers vary. Returns
        None if the field is absent / unparseable / zero."""
        for key in ("averageprice", "averagePrice", "avg_price", "avgprice"):
            v = row.get(key)
            if v in (None, "", 0, 0.0, "0", "0.0"):
                continue
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_filled_qty(row: dict) -> int:
        """Parse the broker's filled-quantity field. AngelOne uses
        ``filledshares``. Returns 0 if unparseable."""
        for key in ("filledshares", "filledquantity", "filledqty", "filled_shares"):
            v = row.get(key)
            if v in (None, ""):
                continue
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
        return 0

    def _find_idempotent_match(
        self,
        *,
        symbol: str,
        tx_type: str,
        quantity: int,
        order_type: str,
        max_age_sec: Optional[float] = None,
    ) -> Optional[str]:
        """ORD-02 (audit 2026-05-28): scan the broker orderBook for a
        recently-placed order that matches the (symbol, side, qty,
        ordertype) intent. If found, return its order_id; the caller
        skips the duplicate placeOrder.

        This is the cheapest workable idempotency: AngelOne's
        placeOrder API has no client-supplied order-tag we can use as
        a true idempotency key (verified 2026-05-28 by reading the
        wrapper), so we fall back to a "did the broker already see
        this intent recently?" probe. The lookback window is the
        time between the previous attempt's ``placeOrder`` start
        and now; pre-retry sleep is the retry_delay so a 30s
        default lookback covers the typical retry budget.
        """
        if self.mode == "paper" or self._api is None:
            return None
        max_age = max_age_sec if max_age_sec is not None else self.idempotency_lookback_sec
        cutoff_ts = datetime.now(IST) - timedelta(seconds=max_age)
        try:
            book = self._api.orderBook()
        except Exception as exc:
            logger.warning(
                f"[IDEMPOTENT-CHECK] orderBook() raised: "
                f"{type(exc).__name__}: {exc!r}. Falling through to retry "
                f"(better duplicate than zero exposure)."
            )
            return None
        if not isinstance(book, dict):
            return None
        rows = book.get("data") or []
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("tradingsymbol") or "").upper() != symbol.upper():
                continue
            if (row.get("transactiontype") or "").upper() != tx_type.upper():
                continue
            if (row.get("ordertype") or "").upper() != order_type.upper():
                continue
            # Quantity match: requested == orderqty (or filled+pending).
            try:
                row_qty = int(float(row.get("quantity") or row.get("orderqty") or 0))
            except (TypeError, ValueError):
                continue
            if row_qty != quantity:
                continue
            # Skip already-terminal cancellations -- those don't count
            # as "the intent is alive on the broker side".
            status = self._normalise_status(row)
            if status in self._TERMINAL_CANCELLED:
                continue
            # Time gate: AngelOne timestamps look like
            # "DD-MMM-YYYY HH:MM:SS" or ISO. Be defensive.
            ts_raw = row.get("orderentrytime") or row.get("updatetime") or row.get("exchorderupdatetime")
            placed_at: Optional[datetime] = None
            if ts_raw:
                placed_at = self._parse_broker_timestamp(str(ts_raw))
            if placed_at is None:
                # No usable timestamp -- safer to assume it's recent
                # and reuse than to risk a duplicate.
                pass
            else:
                if placed_at.tzinfo is None:
                    placed_at = IST.localize(placed_at)
                if placed_at < cutoff_ts:
                    continue
            order_id = row.get("orderid") or row.get("orderID") or row.get("order_id")
            if order_id:
                logger.warning(
                    f"[IDEMPOTENT-CHECK] {symbol} {tx_type} {quantity} "
                    f"{order_type}: broker already has an in-flight order "
                    f"id={order_id} (status={status}, placed_at={placed_at}). "
                    f"Reusing instead of placing a duplicate."
                )
                return str(order_id)
        return None

    @staticmethod
    def _parse_broker_timestamp(raw: str) -> Optional[datetime]:
        """Parse a broker timestamp string. Tries the AngelOne
        ``DD-MMM-YYYY HH:MM:SS`` format first, then ISO. Returns
        None on any failure."""
        if not raw:
            return None
        # AngelOne's documented format
        for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except (ValueError, TypeError):
                continue
        try:
            # Handles ISO with sub-second / tz offset
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    def _live_order_with_retry(
        self, symbol: str, token: str, tx_type: str,
        quantity: int, price: float, order_type: str,
        stop_loss: Optional[float], take_profit: Optional[float], tag: str,
    ) -> Optional[dict]:
        """Place a live order through AngelOne with retry logic.

        ORD-01 (audit 2026-05-28): after placeOrder succeeds, the
        loop now calls ``_wait_for_terminal`` and only mutates the
        returned dict to ``FILLED`` / ``PARTIALLY_FILLED`` / etc.
        based on the broker's terminal state. ``filled_price`` is
        the broker's ``averageprice`` (never the signal-time price)
        and ``slippage`` is computed against the requested price.

        ORD-02 (audit 2026-05-28): on retry attempts >= 2, scan
        the broker orderBook for a recently-placed order matching
        (symbol, side, qty, ordertype). If found, use its id
        instead of placing a duplicate. AngelOne's ``placeOrder``
        timeout warning is honoured: a timed-out call may have
        placed the order, and a naive retry would duplicate it.
        """
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
            # ORD-02 idempotency probe (only on retry attempts).
            response: Optional[str] = None
            if attempt >= 2:
                idempotent_id = self._find_idempotent_match(
                    symbol=symbol, tx_type=tx_type,
                    quantity=quantity, order_type=order_type,
                )
                if idempotent_id is not None:
                    response = idempotent_id

            try:
                if response is None:
                    logger.info(
                        f"Placing order (attempt {attempt}/{self.retry_attempts}): "
                        f"{tx_type} {quantity} x {symbol}"
                    )
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
                        "filled_quantity": 0,
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

                    # ORD-01/STATE-01: wait for terminal status before
                    # treating this as a fill. The caller (entry path)
                    # uses ``filled_price`` to open the portfolio
                    # position, so we MUST know the real fill price
                    # before returning -- otherwise the portfolio's
                    # P&L would be biased by the signal-time price
                    # vs broker reality (esp. on LIMIT orders).
                    terminal_row = self._wait_for_terminal(response)
                    if terminal_row is not None:
                        status = self._normalise_status(terminal_row)
                        filled_price = self._extract_avg_fill_price(terminal_row)
                        filled_qty = self._extract_filled_qty(terminal_row)
                        if status in self._TERMINAL_FILLED:
                            result["status"] = "FILLED"
                            result["filled_price"] = filled_price or price
                            result["filled_quantity"] = filled_qty or quantity
                        elif status in self._TERMINAL_PARTIAL:
                            result["status"] = "PARTIALLY_FILLED"
                            result["filled_price"] = filled_price or price
                            result["filled_quantity"] = filled_qty
                        elif status in self._TERMINAL_CANCELLED:
                            result["status"] = "REJECTED"
                            result["filled_price"] = None
                            result["filled_quantity"] = 0
                            logger.error(
                                f"[LIVE] Order {response} terminal status "
                                f"{status!r} -- treating as failure."
                            )
                            # Caller treats None as failed; surface as such.
                            return None
                        # Recompute slippage now that we know the real fill.
                        if (
                            result["filled_price"]
                            and price
                            and price > 0
                        ):
                            result["slippage"] = round(
                                abs(result["filled_price"] - price), 4
                            )
                        # Reflect updated state on the pending-orders cache
                        # so downstream order-status queries see the truth.
                        self._pending_orders[response] = result
                    else:
                        # TTL expired with no terminal observation. Keep
                        # ``status="PLACED"`` and ``filled_price=None``
                        # so the caller can decide whether to proceed
                        # (current behaviour: caller treats PLACED as
                        # FILLED for the in-memory position). Logging
                        # the TTL warning is already done by _wait_for_terminal.
                        result["filled_quantity"] = 0

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

    def rollback_entry_on_portfolio_failure(
        self,
        *,
        symbol: str,
        token: str,
        entry_order_id: str,
        entry_tx: str,
        quantity: int,
    ) -> bool:
        """ORD-03 (audit 2026-05-28): atomic-entry rollback when the
        broker leg succeeded but ``portfolio.open_position`` failed.

        Pre-fix path: broker holds an open position + a live SL-M leg,
        portfolio has nothing recorded, and the daemon "moves on" --
        leaving NAKED exposure with NO daemon-side awareness. Next
        cycle could even re-fire the same entry on top of it.

        Recovery sequence (best-effort, every step is logged CRITICAL):
          1. Cancel the SL-M leg so it cannot fire as a reverse trade
             after the counter-flatten.
          2. Place a MARKET counter-flatten on the OPPOSITE side to
             zero broker exposure.
          3. Clean up the order-log + pending-orders tracking artifacts
             so the in-memory state matches "entry never happened".

        Returns True iff every step succeeded. False signals a
        partial rollback -- caller MUST block new entries on this
        symbol and surface the alert.

        Paper mode: no-op (the portfolio is the only state to roll
        back, and the caller already noticed the failure).
        """
        if self.mode == "paper" or self._api is None:
            # Nothing to roll back at the broker; clean caller-side
            # tracking and return success.
            self._pending_orders.pop(entry_order_id, None)
            return True

        logger.critical(
            f"[ATOMIC-ROLLBACK] {symbol}: entry order {entry_order_id} "
            f"FILLED at broker but portfolio.open_position FAILED. "
            f"Initiating atomic rollback (cancel SL + counter-flatten)."
        )

        all_ok = True

        # Step 1: cancel SL-M leg so it can't fire mid-rollback.
        try:
            sl_ok = self.cancel_sl_order_for_symbol(symbol)
            if not sl_ok:
                logger.critical(
                    f"[ATOMIC-ROLLBACK] {symbol}: SL cancel returned False. "
                    f"SL-M may still be live on broker -- LTP hit on "
                    f"the trigger could fire a reverse trade."
                )
                all_ok = False
        except Exception as exc:
            logger.critical(
                f"[ATOMIC-ROLLBACK] {symbol}: SL cancel raised "
                f"{type(exc).__name__}: {exc!r}. Continuing with "
                f"counter-flatten anyway -- naked exposure is the "
                f"bigger risk."
            )
            all_ok = False

        # Step 2: counter-flatten the broker leg.
        counter_side = "SELL" if entry_tx.upper() == "BUY" else "BUY"
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
        try:
            counter_id = self._api.placeOrder(flatten_params)
            if counter_id:
                logger.critical(
                    f"[ATOMIC-ROLLBACK] {symbol}: counter-flatten "
                    f"{counter_side} x{quantity} placed "
                    f"(order_id={counter_id}). Broker exposure expected "
                    f"to zero out. Manual broker statement audit recommended."
                )
            else:
                logger.critical(
                    f"[ATOMIC-ROLLBACK] {symbol}: counter-flatten returned "
                    f"empty broker id. BROKER POSITION LIKELY NAKED. "
                    f"INTERVENE IMMEDIATELY."
                )
                all_ok = False
        except Exception as exc:
            logger.critical(
                f"[ATOMIC-ROLLBACK] {symbol}: counter-flatten RAISED "
                f"{type(exc).__name__}: {exc!r}. BROKER POSITION IS NAKED. "
                f"IMMEDIATE INTERVENTION REQUIRED."
            )
            all_ok = False

        # Step 3: clean caller-side tracking so the entry doesn't show
        # as "in-flight" forever. We deliberately do NOT remove the
        # entry-order DB ledger row -- it should remain for forensic
        # audit. The pending-orders / order-log entries are in-memory
        # only and are safe to drop.
        try:
            if self._order_log and self._order_log[-1].get("order_id") == entry_order_id:
                self._order_log.pop()
        except Exception:
            pass
        self._pending_orders.pop(entry_order_id, None)

        return all_ok

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
            # F-16: previously this treated ANY truthy response as success.
            # AngelOne returns dicts where ``status`` may be the string
            # "false" -- which is truthy in Python. The result was that
            # failed SL cancels were reported as success, our internal
            # tracking dropped the SL order, and an orphan SL-M remained
            # live at the broker (reverse-fill risk on next entry / EOD
            # square-off). Mirror the same _interpret_broker_status
            # parsing already used by modify_stop_loss above.
            if isinstance(response, dict):
                if not _interpret_broker_status(response.get("status")):
                    logger.error(
                        f"Failed to cancel order {order_id}: broker rejected "
                        f"with status={response.get('status')!r} "
                        f"message={response.get('message')!r}"
                    )
                    return False
                logger.info(
                    f"Order {order_id} cancelled "
                    f"(broker status={response.get('status')!r})"
                )
                return True
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
        symbol with one of five statuses:

          * ``ok``           DB and broker agree on side AND quantity.
          * ``orphan``       DB shows open, broker shows flat. Caller MUST
                             close the DB-side position with reason
                             ``broker_reconcile_at_boot``.
          * ``mismatch``     DB and broker disagree on side or quantity.
                             Caller should alert and freeze new entries on
                             this symbol until manual reconciliation.
                             Added 2026-05-18 (Regression #3) -- previously
                             any non-zero broker netqty was rubber-stamped
                             ``ok`` without validating side/qty agreement.
          * ``broker_only``  Broker has a non-zero netqty for a symbol the
                             DB doesn't know about. Crash-after-fill-
                             before-DB-write window. STATE-02 (audit
                             2026-05-28): pre-fix this was silently
                             missed -- daemon booted "flat" while broker
                             held real exposure. Caller MUST block new
                             entries on this symbol and emit a CRITICAL
                             alert until manual reconciliation.
          * ``skipped``      broker call failed or paper mode -- caller
                             leaves the position as-is.

        OBS-05 (audit 2026-05-28): when ``get_positions()`` fails in live
        mode, we retry up to 3 times with exponential backoff (2s, 4s,
        8s). On final failure we set ``self.boot_reconcile_failed_live``
        so the caller can fail-CLOSED (refuse new entries) instead of
        the pre-fix fail-open behaviour. Paper mode and the no-DB-
        position case both bypass this gate (nothing to reconcile).

        We do NOT touch the portfolio here; the caller (TradingAgent)
        owns that. Keeps this concern at the daemon layer.
        """
        report: Dict[str, Dict] = {}
        if self.mode == "paper":
            # Paper mode: no broker truth to compare against. Return
            # "skipped" for every DB symbol; nothing else to do.
            for sym in restored_positions or {}:
                report[sym] = {"status": "skipped", "reason": "paper_mode"}
            return report

        # ---------------------------------------------------------------
        # OBS-05: retry positionBook() up to 3x with backoff before we
        # decide to fail-closed. A single transient broker hiccup
        # shouldn't refuse the operator the day, but a persistent one
        # MUST -- the alternative is the pre-fix fail-open behaviour
        # where every DB position was marked "skipped: api_error" and
        # new entries were allowed against possibly-stale state.
        # ---------------------------------------------------------------
        response = None
        last_exc: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                response = self.get_positions()
                if response is not None:
                    break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    f"[POSITION-RECONCILE] positionBook() attempt "
                    f"{attempt}/3 raised: {type(exc).__name__}: {exc!r}"
                )
            if attempt < 3:
                time.sleep(2 ** attempt)  # 2s, 4s
        if response is None:
            # All 3 attempts failed. In live mode this is the OBS-05
            # fail-CLOSED case. Caller will refuse new entries until
            # an operator-touched ack file lifts the gate.
            self.boot_reconcile_failed_live = True
            self.boot_reconcile_failure_reason = (
                f"positionBook_fetch_failed_after_3_retries "
                f"last_exc={type(last_exc).__name__ if last_exc else 'no_response'}"
            )
            logger.critical(
                f"[POSITION-RECONCILE] positionBook() failed after 3 retries. "
                f"Last exception: {last_exc!r}. Setting "
                f"boot_reconcile_failed_live=True. New entries BLOCKED "
                f"until operator inspects broker state and touches the "
                f"ack file (logs/boot_reconcile.ack)."
            )
            for sym in restored_positions or {}:
                report[sym] = {"status": "skipped", "reason": "api_error"}
            return report
        if not response:
            for sym in restored_positions or {}:
                report[sym] = {"status": "skipped", "reason": "empty_response"}
            return report

        rows = response.get("data") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            for sym in restored_positions or {}:
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
            db_side = (db_pos.get("side") or "").upper()
            try:
                db_qty = abs(int(db_pos.get("quantity") or 0))
            except (TypeError, ValueError):
                db_qty = 0

            if broker_qty == 0:
                logger.critical(
                    f"[POSITION-RECONCILE] {sym}: DB shows OPEN "
                    f"({db_side} qty={db_qty}) "
                    f"but broker netqty=0. Likely RMS auto-flatten or "
                    f"manual close. Marking ORPHAN; caller will close "
                    f"the DB position with broker_reconcile_at_boot."
                )
                report[sym] = {
                    "status": "orphan",
                    "broker_netqty": broker_qty,
                    "db_quantity": db_pos.get("quantity"),
                }
                continue

            # 2026-05-18 (Regression #3): validate side + qty match.
            # Previously: any non-zero broker_qty -> "ok". That hid a real
            # bug class -- a half-filled entry (broker netqty=50, DB
            # quantity=100) was silently rubber-stamped, and so was a
            # flipped-side position (DB long-100, broker short-100 because
            # an exit fill was misread as a fresh entry).
            broker_side = "BUY" if broker_qty > 0 else "SELL"
            broker_abs_qty = abs(broker_qty)

            side_ok = db_side == broker_side
            qty_ok = db_qty == broker_abs_qty

            if side_ok and qty_ok:
                report[sym] = {
                    "status": "ok",
                    "broker_netqty": broker_qty,
                    "db_quantity": db_pos.get("quantity"),
                }
                continue

            # Mismatch: surface loudly so the operator can investigate
            # before the next entry tries to add to the wrong side.
            mismatch_reasons = []
            if not side_ok:
                mismatch_reasons.append(
                    f"side(db={db_side}, broker={broker_side})"
                )
            if not qty_ok:
                mismatch_reasons.append(
                    f"qty(db={db_qty}, broker={broker_abs_qty})"
                )
            reason_str = " ".join(mismatch_reasons)
            logger.critical(
                f"[POSITION-RECONCILE] {sym}: DB / broker MISMATCH -- "
                f"{reason_str}. DB position kept as-is; "
                f"new entries on this symbol must be blocked until "
                f"manual reconciliation."
            )
            report[sym] = {
                "status": "mismatch",
                "broker_netqty": broker_qty,
                "broker_side": broker_side,
                "broker_quantity": broker_abs_qty,
                "db_side": db_side,
                "db_quantity": db_qty,
                "reasons": mismatch_reasons,
            }

        # STATE-02 (audit 2026-05-28): scan for broker-only symbols
        # the DB doesn't know about. This is the crash-after-fill-
        # before-DB-write window -- pre-fix the daemon would boot
        # "flat" while the broker held real exposure, and the next
        # cycle's entry on the same symbol would compound it into a
        # double position. Surface every non-zero broker netqty
        # that's absent from the DB and flag it CRITICAL so the
        # caller blocks new entries until manual reconciliation.
        db_symbols = set((restored_positions or {}).keys())
        for sym, broker_qty in broker_qty_by_symbol.items():
            if broker_qty == 0:
                continue
            if sym in db_symbols:
                continue  # already covered above
            broker_side = "BUY" if broker_qty > 0 else "SELL"
            broker_abs_qty = abs(broker_qty)
            logger.critical(
                f"[POSITION-RECONCILE] {sym}: BROKER-ONLY -- "
                f"broker holds {broker_side} qty={broker_abs_qty} but "
                f"DB has no record. Crash-after-fill-before-DB-write or "
                f"out-of-band manual entry. Marking BROKER_ONLY; caller "
                f"will BLOCK new entries on this symbol until operator "
                f"reconciles manually."
            )
            report[sym] = {
                "status": "broker_only",
                "broker_netqty": broker_qty,
                "broker_side": broker_side,
                "broker_quantity": broker_abs_qty,
            }

        return report

    @property
    def order_history(self) -> list[dict]:
        return list(self._order_log)

    @property
    def is_paper_mode(self) -> bool:
        return self.mode == "paper"
