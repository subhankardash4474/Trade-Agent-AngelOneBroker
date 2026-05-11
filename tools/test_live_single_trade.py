"""Stage 2 of the e2e broker test plan: single live round-trip during market hours.

Goal
----
Execute ONE real round-trip (BUY then SELL) on AngelOne in under 3
minutes wall-clock, with hard time-based safeties.

This script supports TWO pricing modes:

(default, "patient"):
    Stage 2 -- run 2026-05-11 10:59 IST. PASSED in 69s. BUY LIMIT at
    LTP-0.1% sat below the best bid and never filled, so the script
    cancelled it cleanly. This exercised:
      - place_order during market hours
      - order book polling
      - buy-fill-timeout cancel branch
      - "no exposure -- clean exit" decision logic
    UNEXERCISED: fill detection, SELL leg, MARKET SELL escalation,
    slippage measurement.

--aggressive ("Stage 2.1"):
    Built 2026-05-11 11:05 IST after Stage 2 post-mortem identified the
    untested branches above. Flips the pricing strategy:
      - BUY LIMIT at LTP * 1.002 (+20 bps above mid -> crosses the ask)
      - SELL LIMIT at refreshed_LTP * 0.998 (-20 bps below mid -> crosses
        the bid for guaranteed-quick fill)
    The cost of guaranteed fills is paying full spread both ways: net
    expected loss ~Rs 0.05-0.15 per Rs 23 round-trip (~0.4% of notional).
    This is the price of empirically validating the fill+exit half of
    the state machine.

Risk envelope
-------------
Default mode is DRY-RUN. Pass ``--confirm`` to send live orders.

When ``--confirm`` is set:
  * Symbol:   YESBANK-EQ (~Rs 23/share)
  * Quantity: 1 share
  * Buy / Sell prices: see pricing-mode table above
  * Wall-clock: total 180 seconds max from buy submission to position flat
  * Fallback:  on any time/error fault, force MARKET SELL to flatten
  * Capital deployed: ~Rs 23 notional
  * Realistic expected loss (patient mode):   Rs 0  (no fill)
  * Realistic expected loss (aggressive mode): Rs 0.05-0.15  (full spread)
  * Realistic worst case (either mode): Rs 5-10 (adverse 1-min move during hold)
  * Catastrophic max: ~Rs 25 (full capital, only if YESBANK halts/circuits)

State machine
-------------
    INIT
      |
      v
    BUY_SUBMITTED  -- wait up to BUY_FILL_TIMEOUT (60s)
      |
      +-- buy fill detected --> SELL_SUBMITTED
      |                            |
      |                            +-- sell fill detected --> FLAT (success)
      |                            +-- SELL_FILL_TIMEOUT --> MARKET SELL --> FLAT
      |                            +-- WALL_CLOCK_HIT --> MARKET SELL --> FLAT
      |
      +-- BUY_FILL_TIMEOUT --> cancel buy --> FLAT (no exposure)
      +-- WALL_CLOCK_HIT before buy fills --> cancel buy --> FLAT

Stages (10 total)
-----------------
   1. .env validation
   2. SmartApi + AngelOneBroker import
   3. Broker instantiation
   4. connect()
   5. Resolve token + get LTP + funds preflight
   6. Submit BUY LIMIT order                           [LIVE only]
   7. Wait for buy fill (poll every 5s, max 60s)       [LIVE only]
   8. Submit SELL LIMIT order at LTP+0.1%              [LIVE only]
   9. Wait for sell fill (poll every 5s) OR force MARKET SELL on time-out
  10. Final state check + disconnect

Usage
-----
    # Dry-run -- prints planned orders, no actual submission:
    python tools/test_live_single_trade.py

    # Live single round-trip:
    python tools/test_live_single_trade.py --confirm

    # Adjust budget if you fund the account differently:
    python tools/test_live_single_trade.py --confirm --max-notional 50

Exit codes
----------
    0  -- round-trip completed, position is flat
    N  -- failed at stage N
   77  -- position was filled but could not be exited cleanly (ACTION REQUIRED)
   99  -- uncaught exception
  130  -- Ctrl+C
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

REQUIRED_ENV_KEYS = (
    "ANGELONE_API_KEY",
    "ANGELONE_API_SECRET",
    "ANGELONE_CLIENT_ID",
    "ANGELONE_PASSWORD",
    "ANGELONE_TOTP_SECRET",
)

DEFAULT_SYMBOL = "YESBANK-EQ"
DEFAULT_EXCHANGE = "NSE"
QUANTITY = 1

BUY_FILL_TIMEOUT_SEC = 60     # cancel BUY if not filled within this
SELL_FILL_TIMEOUT_SEC = 60    # convert SELL to MARKET if not filled within this
TOTAL_WALL_CLOCK_SEC = 180    # hard cap on entire round-trip
POLL_INTERVAL_SEC = 5

# Default ("patient") pricing -- guarantees no fill on a normal book; tests
# the cancel-timeout branch.
BUY_PRICE_OFFSET_PATIENT = 0.999      # LIMIT BUY at LTP * 0.999 (0.1% below)
SELL_PRICE_OFFSET_PATIENT = 1.001     # LIMIT SELL at LTP * 1.001 (0.1% above)

# Aggressive pricing (Stage 2.1) -- crosses the spread on both sides for
# guaranteed-quick fills. Pays ~0.4% of notional as the cost of exercising
# the fill+exit code path.
BUY_PRICE_OFFSET_AGGRESSIVE = 1.002   # LIMIT BUY at LTP * 1.002 (0.2% above)
SELL_PRICE_OFFSET_AGGRESSIVE = 0.998  # LIMIT SELL at LTP * 0.998 (0.2% below)

# Order book status values that mean "this leg is done"
COMPLETED_STATUSES = {"complete", "completed", "filled", "executed"}
CANCELLED_STATUSES = {"cancelled", "canceled", "rejected"}


# ---------------------------------------------------------------------------
# Dual-sink logger (console + file).
# ---------------------------------------------------------------------------
class StageLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(log_path, "w", encoding="utf-8")
        self._t0 = time.time()

    def log(self, msg: str, level: str = "INFO") -> None:
        elapsed = time.time() - self._t0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} (+{elapsed:6.2f}s) [{level:5}] {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def kv(self, label: str, payload) -> None:
        try:
            blob = json.dumps(payload, indent=2, default=str)
        except TypeError:
            blob = repr(payload)
        self.log(f"{label}:\n{blob}", level="DATA")

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


def _mask(s: Optional[str], head: int = 3) -> str:
    if not s:
        return "<empty>"
    return f"{s[:head]}***" if len(s) > head else "***"


def _resolve_token(smart_api, exchange: str, symbol: str, log: StageLogger) -> Optional[str]:
    try:
        resp = smart_api.searchScrip(exchange, symbol)
    except Exception as e:
        log.log(f"searchScrip raised: {type(e).__name__}: {e}", "WARN")
        return None
    if not resp or not resp.get("status"):
        return None
    for entry in (resp.get("data") or []):
        ts = (entry.get("tradingsymbol") or "").upper()
        if ts == symbol.upper():
            tok = entry.get("symboltoken") or entry.get("token")
            if tok:
                return str(tok)
    return None


def _find_order_in_book(book, order_id: str) -> Optional[dict]:
    """Locate our order in the (variably-shaped) order book response."""
    if not isinstance(book, dict):
        return None
    for o in (book.get("data") or []):
        if o.get("orderid") == order_id or o.get("orderID") == order_id:
            return o
    return None


def _order_status(order: dict) -> str:
    """Normalise the order's status field to lowercase."""
    return str(order.get("status") or order.get("orderstatus") or "unknown").lower()


def _order_fill_qty(order: dict) -> int:
    """Number of shares filled for this order (across all partial fills)."""
    for key in ("filledshares", "filledquantity", "filledqty"):
        v = order.get(key)
        if v not in (None, ""):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
    return 0


def _wait_for_terminal(
    broker,
    order_id: str,
    deadline: float,
    log: StageLogger,
    label: str,
) -> str:
    """Poll order book until ``order_id`` reaches a terminal state or deadline.

    Returns the final normalised status string. Possible values:
        - one of COMPLETED_STATUSES
        - one of CANCELLED_STATUSES
        - 'pending' (deadline reached, never terminal)
        - 'unknown' (order never visible in book)
    """
    last_seen = "unknown"
    while time.time() < deadline:
        try:
            book = broker.get_orders()
        except Exception as e:
            log.log(f"{label} get_orders WARN: {type(e).__name__}: {e}", "WARN")
            book = None
        order = _find_order_in_book(book, order_id) if book else None
        if order:
            last_seen = _order_status(order)
            filled = _order_fill_qty(order)
            log.log(f"{label} poll: status={last_seen} filled={filled}/{QUANTITY}")
            if last_seen in COMPLETED_STATUSES:
                return last_seen
            if last_seen in CANCELLED_STATUSES:
                return last_seen
        else:
            log.log(f"{label} poll: order_id {order_id} not in book yet (last_seen={last_seen})")
        time.sleep(POLL_INTERVAL_SEC)
    return "pending" if last_seen == "unknown" else last_seen


def _build_order_params(
    symbol: str,
    token: str,
    exchange: str,
    side: str,
    ordertype: str,
    price: Optional[float],
    qty: int,
) -> dict:
    params = {
        "variety":         "NORMAL",
        "tradingsymbol":   symbol,
        "symboltoken":     token,
        "transactiontype": side,
        "exchange":        exchange,
        "ordertype":       ordertype,
        "producttype":     "DELIVERY",
        "duration":        "DAY",
        "squareoff":       "0",
        "stoploss":        "0",
        "quantity":        str(qty),
    }
    if ordertype == "LIMIT" and price is not None:
        params["price"] = f"{price:.2f}"
    else:
        params["price"] = "0"
    return params


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 2 -- AngelOne single live round-trip (BUY+SELL)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--confirm", action="store_true",
                    help="Actually place live orders. Default is dry-run.")
    ap.add_argument("--aggressive", action="store_true",
                    help="Stage 2.1 pricing: BUY at LTP+0.2 pct, SELL at "
                         "LTP-0.2 pct so both legs cross the spread and fill. "
                         "Use this to exercise the fill / SELL / "
                         "MARKET-SELL-escalation branches that the patient "
                         "(default) mode leaves untested. Costs ~0.4 pct of "
                         "notional (~Rs 0.05-0.15 on a Rs 23 YESBANK trade) "
                         "as the price of testing.")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL,
                    help=f"Trading symbol. Default: {DEFAULT_SYMBOL}")
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE,
                    help=f"Exchange. Default: {DEFAULT_EXCHANGE}")
    ap.add_argument("--quantity", type=int, default=QUANTITY,
                    help=f"Share quantity. Default: {QUANTITY}")
    ap.add_argument("--max-notional", type=float, default=200.0,
                    help="Hard cap on notional in rupees. Aborts if buy "
                         "notional exceeds this. Default: Rs 200 (8x our "
                         "expected Rs 23 to leave room for symbol overrides).")
    ap.add_argument("--total-wall-clock-sec", type=int, default=TOTAL_WALL_CLOCK_SEC,
                    help=f"Hard cap on round-trip duration in seconds. "
                         f"Default: {TOTAL_WALL_CLOCK_SEC}")
    args = ap.parse_args()

    # Pick pricing offsets from --aggressive flag.
    if args.aggressive:
        buy_offset = BUY_PRICE_OFFSET_AGGRESSIVE
        sell_offset = SELL_PRICE_OFFSET_AGGRESSIVE
        pricing_mode = "AGGRESSIVE (Stage 2.1: crosses spread both ways)"
    else:
        buy_offset = BUY_PRICE_OFFSET_PATIENT
        sell_offset = SELL_PRICE_OFFSET_PATIENT
        pricing_mode = "PATIENT (default: BUY below bid, no-fill expected)"

    ts_run = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_tag = "stage21" if args.aggressive else "stage2"
    log_path = ROOT / "logs" / "live_e2e" / f"{log_tag}_{ts_run}.log"
    log = StageLogger(log_path)

    log.log("=" * 76)
    log.log("AngelOne Stage 2 -- Single Live Round-Trip")
    log.log(f"Mode:         {'LIVE (will place real orders)' if args.confirm else 'DRY-RUN (no orders sent)'}")
    log.log(f"Pricing:      {pricing_mode}")
    log.log(f"  BUY offset:  LTP * {buy_offset:.4f}")
    log.log(f"  SELL offset: LTP * {sell_offset:.4f}")
    log.log(f"Symbol:       {args.symbol} @ {args.exchange}")
    log.log(f"Quantity:     {args.quantity}")
    log.log(f"Wall-clock:   {args.total_wall_clock_sec}s hard cap")
    log.log(f"Max notional: Rs {args.max_notional:.2f}")
    log.log(f"Log file:     {log_path}")
    log.log("=" * 76)

    total_stages = 10 if args.confirm else 5

    # ── Stage 1: .env ──────────────────────────────────────────────────
    # Use in-house stdlib-only loader so this works inside the Docker
    # container (env_file injection means os.environ is pre-populated and
    # /app/.env is not mounted -- python-dotenv path would falsely fail).
    from core.secrets import load_dotenv  # noqa: E402
    env_path = ROOT / ".env"
    file_loaded = env_path.exists()
    if file_loaded:
        load_dotenv(str(env_path))
    missing = [k for k in REQUIRED_ENV_KEYS if not os.getenv(k)]
    if missing:
        src = ".env + os.environ" if file_loaded else "os.environ (no .env file)"
        log.log(f"[1/{total_stages}] FAIL missing keys in {src}: {missing}", "FAIL")
        log.close()
        return 1
    log.log(f"[1/{total_stages}] OK   .env loaded, {len(REQUIRED_ENV_KEYS)} keys present")

    # ── Stage 2: imports ──────────────────────────────────────────────
    try:
        from SmartApi import SmartConnect  # noqa: F401
        from brokers.angelone import AngelOneBroker, BrokerLoginError
    except ImportError as e:
        log.log(f"[2/{total_stages}] FAIL import: {e}", "FAIL")
        log.close()
        return 2
    log.log(f"[2/{total_stages}] OK   SmartApi + AngelOneBroker imported")

    # ── Stage 3: instantiate ──────────────────────────────────────────
    broker_config = {
        "broker": {
            "api_key":     os.getenv("ANGELONE_API_KEY"),
            "api_secret":  os.getenv("ANGELONE_API_SECRET"),
            "client_id":   os.getenv("ANGELONE_CLIENT_ID"),
            "password":    os.getenv("ANGELONE_PASSWORD"),
            "totp_secret": os.getenv("ANGELONE_TOTP_SECRET"),
            "min_required_cash": 0.0,
        },
        "market": {"exchange": args.exchange},
    }
    try:
        broker = AngelOneBroker(broker_config)
        log.log(f"[3/{total_stages}] OK   broker instantiated "
                f"(client_id={_mask(os.getenv('ANGELONE_CLIENT_ID'))})")
    except Exception as e:
        log.log(f"[3/{total_stages}] FAIL broker init: {type(e).__name__}: {e}", "FAIL")
        log.close()
        return 3

    exit_code = 0
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    position_open = False  # set True between buy fill and sell fill
    t_start = time.time()

    try:
        # ── Stage 4: connect ──────────────────────────────────────────
        try:
            broker.connect()
            if not broker.is_connected():
                log.log(f"[4/{total_stages}] FAIL is_connected()==False after connect", "FAIL")
                exit_code = 4
                return exit_code
            log.log(f"[4/{total_stages}] OK   connect() succeeded")
        except BrokerLoginError as e:
            log.log(f"[4/{total_stages}] FAIL connect: {e}", "FAIL")
            exit_code = 4
            return exit_code
        except Exception as e:
            log.log(f"[4/{total_stages}] FAIL connect: {type(e).__name__}: {e}", "FAIL")
            exit_code = 4
            return exit_code

        # ── Stage 5: token, LTP, funds preflight ─────────────────────
        try:
            token = _resolve_token(broker.raw_api, args.exchange, args.symbol, log)
            if not token:
                log.log(f"[5/{total_stages}] FAIL token resolution failed", "FAIL")
                exit_code = 5
                return exit_code
            log.log(f"[5/{total_stages}] OK   token={token}")

            ltp = broker.get_ltp(args.exchange, args.symbol, token)
            if ltp is None or ltp <= 0:
                log.log(f"[5/{total_stages}] FAIL get_ltp returned {ltp}", "FAIL")
                exit_code = 5
                return exit_code

            buy_price = round(ltp * buy_offset, 2)
            sell_price_planned = round(ltp * sell_offset, 2)
            notional = buy_price * args.quantity

            log.log(f"[5/{total_stages}] OK   LTP=Rs {ltp:.2f}")
            log.log(f"[5/{total_stages}]      BUY  LIMIT @ Rs {buy_price:.2f}  (notional Rs {notional:.2f})")
            log.log(f"[5/{total_stages}]      SELL LIMIT planned @ Rs {sell_price_planned:.2f}")

            if notional > args.max_notional:
                log.log(f"[5/{total_stages}] FAIL notional Rs {notional:.2f} exceeds "
                        f"cap Rs {args.max_notional:.2f}. Pick a cheaper symbol "
                        f"or raise --max-notional.", "FAIL")
                exit_code = 5
                return exit_code

            funds = broker.get_funds()
            avail = float(funds.get("available_cash", 0.0))
            log.log(f"[5/{total_stages}]      funds: available_cash=Rs {avail:,.2f}")
            if avail < notional + 5:  # 5 rupee safety margin
                log.log(f"[5/{total_stages}] FAIL insufficient cash "
                        f"(have Rs {avail:.2f}, need Rs {notional + 5:.2f})", "FAIL")
                exit_code = 5
                return exit_code
        except Exception as e:
            log.log(f"[5/{total_stages}] FAIL: {type(e).__name__}: {e}", "FAIL")
            log.log(traceback.format_exc(), "TRACE")
            exit_code = 5
            return exit_code

        # Build candidate buy params for display
        buy_params = _build_order_params(
            args.symbol, token, args.exchange, "BUY", "LIMIT",
            price=buy_price, qty=args.quantity,
        )
        log.kv("buy_params (planned)", buy_params)

        if not args.confirm:
            log.log(f"[5/{total_stages}] DRY-RUN complete -- no orders placed")
            log.log("To run live: re-invoke with --confirm")
            return 0

        # ────────────────────────────────────────────────────────────────
        # LIVE PATH from here on. From this point forward, every error
        # path must consider whether we have an open position.
        # ────────────────────────────────────────────────────────────────

        wall_clock_deadline = t_start + args.total_wall_clock_sec

        # ── Stage 6: submit BUY LIMIT ─────────────────────────────────
        log.log(f"[6/{total_stages}] submitting BUY LIMIT...")
        try:
            buy_order_id = broker.place_order(buy_params)
        except Exception as e:
            log.log(f"[6/{total_stages}] BUY place_order exception: "
                    f"{type(e).__name__}: {e}", "FAIL")
            log.log(traceback.format_exc(), "TRACE")
            exit_code = 6
            return exit_code

        if not buy_order_id:
            log.log(f"[6/{total_stages}] FAIL BUY place_order returned no order_id "
                    f"(check AngelOne portal for AG7002 or similar)", "FAIL")
            exit_code = 6
            return exit_code

        log.log(f"[6/{total_stages}] OK   BUY submitted, order_id={buy_order_id}")

        # ── Stage 7: wait for buy fill (or buy timeout) ───────────────
        buy_deadline = min(time.time() + BUY_FILL_TIMEOUT_SEC, wall_clock_deadline)
        log.log(f"[7/{total_stages}] waiting up to {int(buy_deadline - time.time())}s for BUY fill...")
        buy_final = _wait_for_terminal(broker, buy_order_id, buy_deadline, log, "BUY")
        log.log(f"[7/{total_stages}] BUY final status: {buy_final}")

        if buy_final in COMPLETED_STATUSES:
            position_open = True
            log.log(f"[7/{total_stages}] OK   BUY filled")
        elif buy_final in CANCELLED_STATUSES:
            log.log(f"[7/{total_stages}] OK   BUY {buy_final} -- no exposure, exiting clean")
            return 0
        else:
            # Pending/unknown -- cancel the buy and exit clean.
            log.log(f"[7/{total_stages}] BUY not filled by deadline; cancelling...")
            try:
                cancel_ok = broker.cancel_order(buy_order_id, variety="NORMAL")
                log.log(f"[7/{total_stages}] BUY cancel returned: {cancel_ok}")
            except Exception as e:
                log.log(f"[7/{total_stages}] BUY cancel exception: {e}", "WARN")
            # Re-check status to confirm.
            time.sleep(2)
            try:
                book = broker.get_orders()
                order = _find_order_in_book(book, buy_order_id)
                if order:
                    final = _order_status(order)
                    filled_qty = _order_fill_qty(order)
                    log.log(f"[7/{total_stages}] post-cancel BUY status: {final}, "
                            f"filled={filled_qty}")
                    if final in COMPLETED_STATUSES or filled_qty >= args.quantity:
                        log.log(f"[7/{total_stages}] WARN BUY filled in the window "
                                f"between deadline and cancel. Proceeding to SELL.", "WARN")
                        position_open = True
                    elif filled_qty > 0:
                        log.log(f"[7/{total_stages}] WARN partial fill of {filled_qty}/{args.quantity}. "
                                f"Treating as open position.", "WARN")
                        position_open = True
            except Exception as e:
                log.log(f"[7/{total_stages}] post-cancel check WARN: {e}", "WARN")

            if not position_open:
                log.log(f"[7/{total_stages}] OK   no exposure -- clean exit")
                return 0

        # ── Stage 8: submit SELL LIMIT ────────────────────────────────
        # Refresh LTP for sell price (uses the same sell_offset chosen at
        # startup -- patient or aggressive).
        try:
            ltp_now = broker.get_ltp(args.exchange, args.symbol, token)
            if ltp_now and ltp_now > 0:
                sell_price = round(ltp_now * sell_offset, 2)
            else:
                sell_price = sell_price_planned
        except Exception:
            sell_price = sell_price_planned

        sell_params = _build_order_params(
            args.symbol, token, args.exchange, "SELL", "LIMIT",
            price=sell_price, qty=args.quantity,
        )
        log.kv("sell_params (planned)", sell_params)
        log.log(f"[8/{total_stages}] submitting SELL LIMIT @ Rs {sell_price:.2f}...")
        try:
            sell_order_id = broker.place_order(sell_params)
        except Exception as e:
            log.log(f"[8/{total_stages}] SELL place_order exception: "
                    f"{type(e).__name__}: {e}", "FAIL")

        if not sell_order_id:
            log.log(f"[8/{total_stages}] FAIL SELL LIMIT didn't take order_id, "
                    f"escalating to MARKET SELL...", "FAIL")
        else:
            log.log(f"[8/{total_stages}] OK   SELL submitted, order_id={sell_order_id}")

            # ── Stage 9: wait for sell fill, escalate to MARKET on timeout
            sell_deadline = min(time.time() + SELL_FILL_TIMEOUT_SEC, wall_clock_deadline)
            log.log(f"[9/{total_stages}] waiting up to "
                    f"{int(sell_deadline - time.time())}s for SELL fill...")
            sell_final = _wait_for_terminal(broker, sell_order_id, sell_deadline, log, "SELL")
            log.log(f"[9/{total_stages}] SELL final status: {sell_final}")

            if sell_final in COMPLETED_STATUSES:
                position_open = False
                log.log(f"[9/{total_stages}] OK   SELL filled -- position FLAT")
            else:
                log.log(f"[9/{total_stages}] SELL not filled; cancelling and "
                        f"escalating to MARKET SELL", "WARN")
                try:
                    broker.cancel_order(sell_order_id, variety="NORMAL")
                except Exception as e:
                    log.log(f"[9/{total_stages}] SELL cancel WARN: {e}", "WARN")
                time.sleep(1)

        # MARKET SELL escalation path. We reach here if:
        #   - SELL LIMIT failed to place
        #   - SELL LIMIT placed but didn't fill in time
        if position_open:
            market_sell_params = _build_order_params(
                args.symbol, token, args.exchange, "SELL", "MARKET",
                price=None, qty=args.quantity,
            )
            log.kv("market_sell_params", market_sell_params)
            log.log(f"[9/{total_stages}] submitting MARKET SELL...")
            try:
                ms_id = broker.place_order(market_sell_params)
            except Exception as e:
                log.log(f"[9/{total_stages}] MARKET SELL exception: {e}", "FAIL")
                ms_id = None

            if ms_id:
                log.log(f"[9/{total_stages}] OK   MARKET SELL submitted, order_id={ms_id}")
                ms_deadline = min(time.time() + 30, wall_clock_deadline)
                ms_final = _wait_for_terminal(broker, ms_id, ms_deadline, log, "MKT_SELL")
                if ms_final in COMPLETED_STATUSES:
                    position_open = False
                    log.log(f"[9/{total_stages}] OK   MARKET SELL filled -- position FLAT")
                else:
                    log.log(f"[9/{total_stages}] FAIL MARKET SELL final status: "
                            f"{ms_final}. POSITION STILL OPEN.", "FAIL")
            else:
                log.log(f"[9/{total_stages}] FAIL MARKET SELL didn't take order_id. "
                        f"POSITION STILL OPEN.", "FAIL")

        # ── Stage 10: final state check ───────────────────────────────
        try:
            time.sleep(2)
            final_book = broker.get_orders()
            log.kv("final order_book", final_book)
            try:
                positions = broker.get_positions()
                log.kv("final positions", positions)
            except Exception as e:
                log.log(f"final get_positions WARN: {e}", "WARN")
        except Exception as e:
            log.log(f"[10/{total_stages}] final check WARN: {e}", "WARN")

        if position_open:
            log.log(f"[10/{total_stages}] CRITICAL: position still open. "
                    f"Manually flatten via AngelOne app NOW.", "ACT")
            exit_code = 77
        else:
            log.log(f"[10/{total_stages}] OK   position FLAT -- round-trip complete")
            exit_code = 0

    finally:
        try:
            broker.disconnect()
            log.log("disconnect() clean")
        except Exception as e:
            log.log(f"disconnect WARN (non-fatal): {e}", "WARN")

        log.log("=" * 76)
        if exit_code == 0:
            log.log("SUMMARY: Stage 2 PASS -- live single-trade round-trip completed")
            log.log("Next: post-mortem (slippage, fill latency), update e2e plan doc")
        elif exit_code == 77:
            log.log("SUMMARY: Stage 2 INCOMPLETE -- position may still be open")
            log.log("ACTION REQUIRED: open AngelOne app, navigate to Positions, "
                    "manually square off any open YESBANK position immediately.", "ACT")
        else:
            log.log(f"SUMMARY: Stage 2 FAIL at stage {exit_code}")

        log.log(f"Wall-clock: {time.time() - t_start:.1f}s")
        log.log(f"Full log: {log_path}")
        log.log("=" * 76)
        log.close()

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ABORTED] Ctrl+C received. If a position was opened it may "
              "still be live -- check AngelOne app NOW.")
        sys.exit(130)
    except Exception:
        print("\n[CRASHED] uncaught exception:")
        traceback.print_exc()
        sys.exit(99)
