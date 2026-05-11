"""Stage 1 of the e2e broker test plan: order place + cancel lifecycle.

Goal
----
Prove that the live ``place_order`` + ``cancel_order`` code path works
end-to-end against AngelOne with real (but harmless) order parameters.
Stage 0 already proved auth + read-only calls; this is the first write.

LIVE RESULT (2026-05-11 10:54 IST)
----------------------------------
PASSED. Findings baked in below:
  1. AngelOne SmartAPI ``placeOrder`` does NOT accept ``variety="AMO"``.
     Error AB1007: "Invalid Order Variety. Value should be NORMAL,
     STOPLOSS, ROBO". AMO must be placed via web/mobile UI only.
     We keep the AMO->NORMAL fallback as defensive code, but ``--variety
     NORMAL`` is now the recommended default.
  2. NSE circuit limits clip deep-OOM LIMIT prices: YESBANK at LTP ~Rs 22.80
     has a lower circuit at ~Rs 20.55 (intraday ~5% band). Our -10% OOM
     LIMIT was rejected by the exchange. For lifecycle tests this is
     still acceptable (rejection is cancellable), but for production
     strategies all LIMIT prices should be clamped within the daily
     circuit band.
  3. Empirical latencies: place_order ~500ms, cancel_order ~600ms,
     get_orders polling ~500ms. Well within daemon's 5s polling budget.

Risk envelope
-------------
Default mode is DRY-RUN -- read-only stages execute, but NO order is
sent. Pass ``--confirm`` to send a real order.

When ``--confirm`` is set, the live order is intentionally engineered
to be a no-op:
  * Symbol:   YESBANK-EQ (cheap, top-50 NSE liquidity)
  * Side:     BUY  -- 1 share only
  * Variety:  AMO first (will be rejected -- AngelOne SmartAPI does
              not accept AMO -- so falls back to NORMAL automatically)
  * Type:     LIMIT at LTP * 0.90  (-10% out-of-the-money). NOTE: this
              may itself be rejected by NSE for breaching the lower
              circuit limit; that's still a valid lifecycle outcome
              for this test.
  * Product:  DELIVERY  (no leverage, no intraday auto-square-off)
  * Cancel:   placed after ~20s
  * Capital deployed: ~Rs 25 max if all filters somehow pass and the
              order actually fills, which empirically does not happen.
  * Realistic worst-case loss: Rs 0 (every live run to date has been
              free of fill).

Stages (8 total in live mode, 5 in dry-run)
-------------------------------------------
  1. .env validation + 5 ANGELONE_* keys present
  2. SmartApi + AngelOneBroker import
  3. Broker instantiation
  4. connect() -- TOTP login
  5. resolve_token() + get_ltp() -- compute limit price
  6. PRE-FLIGHT funds check                              [LIVE only]
  7. place_order(AMO -> fallback NORMAL on reject)       [LIVE only]
  8. wait ~20s, then cancel_order() + verify cancelled   [LIVE only]
  + always: disconnect()

Usage
-----
    # Dry-run (no orders, verifies plumbing):
    python tools/test_amo_lifecycle.py

    # Live (places real order, cancels in 20s):
    python tools/test_amo_lifecycle.py --confirm

    # Force NORMAL variety only (skip AMO attempt):
    python tools/test_amo_lifecycle.py --confirm --variety NORMAL

Outputs
-------
    Console: live status
    File:    logs/live_e2e/stage1_<timestamp>.log  (full audit trail)

Exit codes
----------
    0  -- all live stages passed (or dry-run completed)
    N  -- failed at stage N (see stage table above)
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
DEEP_OOM_PCT = 0.10              # LIMIT BUY at LTP * (1 - 0.10) = -10% OOM
WAIT_BEFORE_CANCEL_SEC = 20
QUANTITY = 1                     # one share, always


# ---------------------------------------------------------------------------
# Tiny dual-sink logger (console + file). Avoids loguru dependency creep in
# tools/ scripts so this can run standalone before any imports.
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
    """Use AngelOne's searchScrip to find the instrument token at runtime.

    Why dynamic resolution: hard-coded tokens go stale after stock splits,
    name changes, or instrument master updates. The cost of one extra
    API call is negligible and we get correctness for free.
    """
    try:
        resp = smart_api.searchScrip(exchange, symbol)
    except Exception as e:
        log.log(f"searchScrip raised: {type(e).__name__}: {e}", "WARN")
        return None

    log.kv("searchScrip response", resp)
    if not resp or not resp.get("status"):
        return None
    data = resp.get("data") or []
    for entry in data:
        # Look for exact tradingsymbol match (case-insensitive). searchScrip
        # is fuzzy and can return many variants (NSE / BSE / different
        # series), so we filter strictly.
        ts = (entry.get("tradingsymbol") or "").upper()
        if ts == symbol.upper():
            tok = entry.get("symboltoken") or entry.get("token")
            if tok:
                return str(tok)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 1 -- AngelOne place_order + cancel_order live test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--confirm", action="store_true",
                    help="Actually place a live order. Without this flag, "
                         "we do read-only stages and print the order params "
                         "that WOULD be sent.")
    ap.add_argument("--variety", choices=["AMO", "NORMAL"], default="AMO",
                    help="Initial order variety. AMO is rejected during "
                         "market hours -> auto-fallback to NORMAL. "
                         "NORMAL goes straight to a live working limit "
                         "order. Default: AMO.")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL,
                    help=f"Trading symbol. Default: {DEFAULT_SYMBOL}")
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE,
                    help=f"Exchange. Default: {DEFAULT_EXCHANGE}")
    ap.add_argument("--wait-sec", type=int, default=WAIT_BEFORE_CANCEL_SEC,
                    help=f"Seconds between place and cancel. "
                         f"Default: {WAIT_BEFORE_CANCEL_SEC}")
    args = ap.parse_args()

    ts_run = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = ROOT / "logs" / "live_e2e" / f"stage1_{ts_run}.log"
    log = StageLogger(log_path)

    log.log("=" * 76)
    log.log("AngelOne Stage 1 -- Order Place+Cancel Lifecycle")
    log.log(f"Mode:     {'LIVE (will place real order)' if args.confirm else 'DRY-RUN (no orders sent)'}")
    log.log(f"Variety:  {args.variety} (with NORMAL fallback if AMO rejected)")
    log.log(f"Symbol:   {args.symbol} @ {args.exchange}")
    log.log(f"Quantity: {QUANTITY}")
    log.log(f"Log file: {log_path}")
    log.log("=" * 76)

    total_stages = 8 if args.confirm else 5

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
    placed_order_id: Optional[str] = None
    placed_variety: str = args.variety

    try:
        # ── Stage 4: connect ──────────────────────────────────────────
        try:
            broker.connect()
            if not broker.is_connected():
                log.log(f"[4/{total_stages}] FAIL connect() returned but is_connected()==False", "FAIL")
                exit_code = 4
                return exit_code
            log.log(f"[4/{total_stages}] OK   connect() succeeded (TOTP + JWT exchange)")
        except BrokerLoginError as e:
            log.log(f"[4/{total_stages}] FAIL connect: {e}", "FAIL")
            exit_code = 4
            return exit_code
        except Exception as e:
            log.log(f"[4/{total_stages}] FAIL connect unexpected: {type(e).__name__}: {e}", "FAIL")
            exit_code = 4
            return exit_code

        # ── Stage 5: resolve token + LTP + compute limit price ────────
        try:
            token = _resolve_token(broker.raw_api, args.exchange, args.symbol, log)
            if not token:
                log.log(f"[5/{total_stages}] FAIL could not resolve token for {args.symbol}", "FAIL")
                exit_code = 5
                return exit_code
            log.log(f"[5/{total_stages}] OK   token resolved: {args.symbol} -> {token}")

            ltp = broker.get_ltp(args.exchange, args.symbol, token)
            if ltp is None or ltp <= 0:
                log.log(f"[5/{total_stages}] FAIL get_ltp returned {ltp}", "FAIL")
                exit_code = 5
                return exit_code

            limit_price = round(ltp * (1 - DEEP_OOM_PCT), 2)
            notional = limit_price * QUANTITY
            log.log(f"[5/{total_stages}] OK   LTP=Rs {ltp:.2f}  "
                    f"LIMIT_BUY @ Rs {limit_price:.2f}  "
                    f"({DEEP_OOM_PCT*100:.0f}% OOM, notional Rs {notional:.2f})")
        except Exception as e:
            log.log(f"[5/{total_stages}] FAIL token/LTP: {type(e).__name__}: {e}", "FAIL")
            log.log(traceback.format_exc(), "TRACE")
            exit_code = 5
            return exit_code

        # Build the candidate order params now -- shown in both dry-run
        # and live modes for transparency.
        order_params_base = {
            "tradingsymbol":   args.symbol,
            "symboltoken":     token,
            "transactiontype": "BUY",
            "exchange":        args.exchange,
            "ordertype":       "LIMIT",
            "producttype":     "DELIVERY",
            "duration":        "DAY",
            "price":           f"{limit_price:.2f}",
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(QUANTITY),
        }
        order_params = {"variety": args.variety, **order_params_base}
        log.kv("order_params (planned)", order_params)

        if not args.confirm:
            log.log(f"[5/{total_stages}] DRY-RUN complete -- no order placed")
            log.log("To run live: re-invoke with --confirm")
            return 0

        # ────────────────────────────────────────────────────────────────
        # LIVE PATH: everything below runs only with --confirm.
        # ────────────────────────────────────────────────────────────────

        # ── Stage 6: pre-flight funds check ───────────────────────────
        try:
            funds = broker.get_funds()
            avail = float(funds.get("available_cash", 0.0))
            log.log(f"[6/{total_stages}] funds: available_cash=Rs {avail:,.2f}, "
                    f"required~=Rs {notional:.2f}")
            if avail < notional:
                log.log(f"[6/{total_stages}] FAIL insufficient cash "
                        f"(have Rs {avail:.2f}, need Rs {notional:.2f}). "
                        f"Fund the account or pick a cheaper symbol.", "FAIL")
                exit_code = 6
                return exit_code
            log.log(f"[6/{total_stages}] OK   pre-flight passed")
        except Exception as e:
            log.log(f"[6/{total_stages}] FAIL get_funds: {type(e).__name__}: {e}", "FAIL")
            exit_code = 6
            return exit_code

        # ── Stage 7: place_order (with AMO->NORMAL fallback) ──────────
        log.log(f"[7/{total_stages}] sending {args.variety} place_order...")
        try:
            placed_order_id = broker.place_order(order_params)
        except Exception as e:
            log.log(f"[7/{total_stages}] place_order exception: {type(e).__name__}: {e}", "WARN")
            log.log(traceback.format_exc(), "TRACE")
            placed_order_id = None

        if not placed_order_id and args.variety == "AMO":
            log.log(f"[7/{total_stages}] AMO returned no order_id (expected during market "
                    f"hours). Falling back to NORMAL.", "INFO")
            placed_variety = "NORMAL"
            order_params = {"variety": "NORMAL", **order_params_base}
            log.kv("order_params (NORMAL fallback)", order_params)
            try:
                placed_order_id = broker.place_order(order_params)
            except Exception as e:
                log.log(f"[7/{total_stages}] NORMAL fallback exception: {e}", "FAIL")
                log.log(traceback.format_exc(), "TRACE")
                placed_order_id = None

        if not placed_order_id:
            log.log(f"[7/{total_stages}] FAIL place_order returned no order_id "
                    f"(both AMO and NORMAL attempts)", "FAIL")
            exit_code = 7
            return exit_code

        log.log(f"[7/{total_stages}] OK   placed {placed_variety} order, "
                f"order_id={placed_order_id}")

        # ── Stage 8: wait + verify + cancel + final check ─────────────
        log.log(f"[{total_stages}/{total_stages}] waiting {args.wait_sec}s before cancel...")
        time.sleep(args.wait_sec)

        # Status snapshot before cancel.
        try:
            order_book = broker.get_orders()
            log.kv("order_book (pre-cancel)", order_book)
            our_order = None
            if isinstance(order_book, dict):
                for o in (order_book.get("data") or []):
                    if o.get("orderid") == placed_order_id or o.get("orderID") == placed_order_id:
                        our_order = o
                        break
            if our_order:
                status = our_order.get("status") or our_order.get("orderstatus") or "unknown"
                log.log(f"pre-cancel status: {status}")
                if str(status).lower() in {"complete", "filled", "executed"}:
                    log.log("WARN order appears FILLED before cancel. Notional "
                            f"~Rs {notional:.2f} now in the position. Will still "
                            f"attempt cancel (no-op), then exit -- review manually.", "WARN")
            else:
                log.log(f"order_id {placed_order_id} not found in pre-cancel book", "WARN")
        except Exception as e:
            log.log(f"pre-cancel get_orders WARN: {type(e).__name__}: {e}", "WARN")

        # The cancel itself.
        try:
            cancel_ok = broker.cancel_order(placed_order_id, variety=placed_variety)
            if cancel_ok:
                log.log(f"[{total_stages}/{total_stages}] OK   cancel_order ACKed "
                        f"(order_id={placed_order_id}, variety={placed_variety})")
            else:
                log.log(f"[{total_stages}/{total_stages}] FAIL cancel_order returned False", "FAIL")
                exit_code = 8
        except Exception as e:
            log.log(f"[{total_stages}/{total_stages}] FAIL cancel_order exception: "
                    f"{type(e).__name__}: {e}", "FAIL")
            log.log(traceback.format_exc(), "TRACE")
            exit_code = 8

        # Confirm final cancelled state.
        try:
            time.sleep(2)
            final_book = broker.get_orders()
            log.kv("order_book (post-cancel)", final_book)
            if isinstance(final_book, dict):
                for o in (final_book.get("data") or []):
                    if o.get("orderid") == placed_order_id or o.get("orderID") == placed_order_id:
                        final_status = o.get("status") or o.get("orderstatus") or "unknown"
                        log.log(f"final status of {placed_order_id}: {final_status}")
                        if str(final_status).lower() not in {"cancelled", "rejected", "canceled"}:
                            log.log(f"WARN final status is '{final_status}' -- "
                                    f"expected cancelled/rejected. Check AngelOne app.", "WARN")
                        break
        except Exception as e:
            log.log(f"post-cancel get_orders WARN: {type(e).__name__}: {e}", "WARN")

    finally:
        try:
            broker.disconnect()
            log.log("disconnect() clean")
        except Exception as e:
            log.log(f"disconnect WARN (non-fatal): {e}", "WARN")

        log.log("=" * 76)
        if exit_code == 0:
            log.log("SUMMARY: Stage 1 PASS -- place_order + cancel_order lifecycle verified")
            log.log("Next: Stage 2 (live single 5-min round-trip) -- target ~13:00 IST today")
        else:
            log.log(f"SUMMARY: Stage 1 FAIL at stage {exit_code}")
            log.log("Do NOT proceed to Stage 2 until this is green.")
            if placed_order_id:
                log.log(f"IMPORTANT: order_id {placed_order_id} may still be live. "
                        f"Check AngelOne app and cancel manually if needed.", "ACT")
        log.log(f"Full log: {log_path}")
        log.log("=" * 76)
        log.close()

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ABORTED] Ctrl+C received.")
        sys.exit(130)
    except Exception:
        print("\n[CRASHED] uncaught exception:")
        traceback.print_exc()
        sys.exit(99)
