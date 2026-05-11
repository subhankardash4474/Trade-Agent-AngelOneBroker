"""Stage 0 of the e2e broker test plan: auth smoke test, NO orders.

Why this exists
---------------
Before Stage 1 (AMO order placement) on Saturday, we need ONE confirmed
fact: the live AngelOne SmartAPI auth path works end-to-end with the
credentials in .env. This script proves it without touching the order
book.

What it does (8 stages, in order)
---------------------------------
  1. Load .env, verify all 5 ANGELONE_* keys are present + non-empty
  2. Import SmartApi.SmartConnect (catches missing pip dep early)
  3. Instantiate AngelOneBroker from packages/brokers/angelone.py
  4. connect() -- the big one: TOTP gen + generateSession + jwtToken
  5. get_profile() -- validates the session works for read calls
  6. get_funds() -- THIS is the user-visible test: does the funded
     amount show up? (User said they funded Rs 1000; we'll see.)
  7. get_orders() -- read-only call, confirms more endpoints work
  8. disconnect() -- clean session termination

What it deliberately does NOT do
--------------------------------
  - place_order, cancel_order, modify_order: ZERO order mutations
  - get_positions(): could lie if account has open positions from a
    previous test; we don't want to act on stale data here
  - getCandleData: separate test, belongs to the historical-fetcher
    integration work
  - Any retries beyond the one-retry baked into _safe_call: we want
    auth failures to surface loudly, not get masked by exponential
    backoff

Usage
-----
    # Validate .env without hitting AngelOne (offline check):
    python tools/test_angelone_auth.py --dry-run

    # Full auth smoke (hits live AngelOne):
    python tools/test_angelone_auth.py

Exit codes: 0 = all 8 stages pass, non-zero = stage number that failed.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Bootstrap packages/ on sys.path so `brokers.angelone` resolves.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

REQUIRED_ENV_KEYS = (
    "ANGELONE_API_KEY",
    "ANGELONE_API_SECRET",
    "ANGELONE_CLIENT_ID",
    "ANGELONE_PASSWORD",
    "ANGELONE_TOTP_SECRET",
)

# RELIANCE on NSE Cash -- publicly known instrument token. Used as a
# benign smoke test for any future market-data calls. NOT used in this
# Stage 0 script (no get_ltp call); kept here as documentation for
# whoever extends this to Stage 0.5.
_RELIANCE_NSE_TOKEN = "2885"


def _mask(s: str, head: int = 3) -> str:
    """Show first `head` chars + '***' so we don't leak secrets to logs."""
    if not s:
        return "<empty>"
    return f"{s[:head]}***" if len(s) > head else "***"


def _row(stage_num: int, total: int, status: str, label: str, detail: str = "") -> str:
    return f"[{stage_num}/{total}] {status:<4} {label}" + (f" -- {detail}" if detail else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate .env without hitting AngelOne. "
                         "Useful for pre-flight check on the laptop "
                         "before sitting down to run the real test.")
    args = ap.parse_args()

    total_stages = 8 if not args.dry_run else 2
    passed = 0
    print("=" * 72)
    print("AngelOne Stage 0 -- Live Auth Smoke Test  (NO ORDERS PLACED)")
    print("=" * 72)
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Mode:   {'DRY-RUN (config only)' if args.dry_run else 'FULL (will hit AngelOne servers)'}")
    print()

    # ── Stage 1: .env present, all 5 keys non-empty ───────────────────
    # We use the in-house core.secrets.load_dotenv (stdlib-only, gracefully
    # no-ops if the file is missing) instead of python-dotenv so this script
    # also works inside the Docker container, where docker-compose injects
    # the .env values via env_file at startup but never mounts the file
    # itself into /app. In that path the file-load is a no-op and we just
    # read straight from os.environ.
    try:
        from core.secrets import load_dotenv  # noqa: E402
        env_path = ROOT / ".env"
        file_loaded = env_path.exists()
        if file_loaded:
            load_dotenv(str(env_path))
        missing = [k for k in REQUIRED_ENV_KEYS if not os.getenv(k)]
        if missing:
            src = ".env + os.environ" if file_loaded else "os.environ (no .env file present)"
            print(_row(1, total_stages, "FAIL", ".env keys",
                       f"missing or empty in {src}: {missing}"))
            return 1
        src_note = ".env file" if file_loaded else "os.environ (docker env_file inject)"
        print(_row(1, total_stages, "OK", ".env loaded",
                   f"{len(REQUIRED_ENV_KEYS)} ANGELONE_* keys present (via {src_note})"))
        passed = 1
    except Exception as e:
        print(_row(1, total_stages, "FAIL", ".env stage", f"unexpected: {e!r}"))
        return 1

    # ── Stage 2: SmartApi importable + AngelOneBroker class loads ─────
    try:
        from SmartApi import SmartConnect  # noqa: F401
        from brokers.angelone import AngelOneBroker, BrokerLoginError
        print(_row(2, total_stages, "OK", "SmartApi + AngelOneBroker imported"))
        passed = 2
    except ImportError as e:
        print(_row(2, total_stages, "FAIL", "import",
                   f"{e}. Run: pip install smartapi-python"))
        return 2
    except Exception as e:
        print(_row(2, total_stages, "FAIL", "import", f"unexpected: {e!r}"))
        return 2

    if args.dry_run:
        print()
        print("=" * 72)
        print(f"DRY-RUN SUMMARY: {passed}/{total_stages} stages PASS.")
        print("Run without --dry-run to perform the actual AngelOne login.")
        print("=" * 72)
        return 0

    # ── Stage 3: Instantiate broker with creds from .env ──────────────
    broker_config = {
        "broker": {
            "api_key":      os.getenv("ANGELONE_API_KEY"),
            "api_secret":   os.getenv("ANGELONE_API_SECRET"),
            "client_id":    os.getenv("ANGELONE_CLIENT_ID"),
            "password":     os.getenv("ANGELONE_PASSWORD"),
            "totp_secret":  os.getenv("ANGELONE_TOTP_SECRET"),
            "min_required_cash": 0.0,  # don't enforce floor for the auth test
        },
        "market": {"exchange": "NSE"},
    }
    broker = None
    try:
        broker = AngelOneBroker(broker_config)
        print(_row(3, total_stages, "OK", "broker instantiated",
                   f"client_id={_mask(os.getenv('ANGELONE_CLIENT_ID'))}"))
        passed = 3
    except Exception as e:
        print(_row(3, total_stages, "FAIL", "broker instantiation",
                   f"{type(e).__name__}: {e}"))
        return 3

    # Wrap stages 4-8 in try/finally so disconnect ALWAYS runs even on
    # mid-flow failure -- otherwise a hung session could block subsequent
    # logins (AngelOne caps concurrent sessions per client).
    try:
        # ── Stage 4: connect() ─ TOTP + login ─────────────────────────
        try:
            broker.connect()
            if not broker.is_connected():
                # connect() returned without raising but is_connected()
                # says no -- usually the min_required_cash gate, which
                # we disabled above. Surface this oddity.
                print(_row(4, total_stages, "FAIL", "connect()",
                           "returned without raising but is_connected()==False"))
                return 4
            print(_row(4, total_stages, "OK", "connect() succeeded",
                       "session active"))
            passed = 4
        except BrokerLoginError as e:
            print(_row(4, total_stages, "FAIL", "connect()", f"{e}"))
            print()
            print("Common causes for code AB1050 (Invalid IP):")
            print("  -> AngelOne SmartAPI Dashboard -> Edit App -> add current")
            print("     public IP to whitelist. Check https://api.ipify.org")
            print("Common causes for code AB1007 (Invalid TOTP):")
            print("  -> System clock drift. Sync Windows time and retry.")
            print("  -> Wrong totp_secret. Reset 2FA on AngelOne and update .env.")
            return 4
        except Exception as e:
            print(_row(4, total_stages, "FAIL", "connect()",
                       f"unexpected {type(e).__name__}: {e}"))
            return 4

        # ── Stage 5: get_profile() ────────────────────────────────────
        try:
            prof = broker.get_profile()
            if not prof or not prof.get("client_code"):
                print(_row(5, total_stages, "FAIL", "get_profile()",
                           f"empty or missing client_code: {prof}"))
                return 5
            name = (prof.get("name") or "")
            email = (prof.get("email") or "")
            exchanges = ",".join(prof.get("exchanges") or [])
            print(_row(5, total_stages, "OK", "get_profile()",
                       f"name={_mask(name, 1)}, "
                       f"client_code={_mask(prof['client_code'])}, "
                       f"email={_mask(email, 2)}, "
                       f"exchanges=[{exchanges}]"))
            passed = 5
        except Exception as e:
            print(_row(5, total_stages, "FAIL", "get_profile()",
                       f"{type(e).__name__}: {e}"))
            return 5

        # ── Stage 6: get_funds() -- the headline user-visible test ────
        try:
            funds = broker.get_funds()
            avail = float(funds.get("available_cash", 0.0))
            used = float(funds.get("used_margin", 0.0))
            net = float(funds.get("net", 0.0))
            print(_row(6, total_stages, "OK", "get_funds()",
                       f"available_cash=Rs {avail:,.2f}, "
                       f"used_margin=Rs {used:,.2f}, "
                       f"net=Rs {net:,.2f}"))
            if avail < 1.0:
                print("       WARNING: available_cash is < Rs 1. Did the funding")
                print("                clear? AngelOne shows funded balance under")
                print("                'Funds' -> 'Available to trade'.")
            passed = 6
        except Exception as e:
            print(_row(6, total_stages, "FAIL", "get_funds()",
                       f"{type(e).__name__}: {e}"))
            return 6

        # ── Stage 7: get_orders() -- read-only confidence check ───────
        try:
            orders = broker.get_orders()
            n_orders = 0
            if isinstance(orders, dict):
                data = orders.get("data") or []
                n_orders = len(data) if isinstance(data, list) else 0
            print(_row(7, total_stages, "OK", "get_orders()",
                       f"{n_orders} order(s) in book (expected 0 for fresh account)"))
            passed = 7
        except Exception as e:
            print(_row(7, total_stages, "FAIL", "get_orders()",
                       f"{type(e).__name__}: {e}"))
            return 7

    finally:
        # ── Stage 8: disconnect() ─ always runs, even on prior failure ─
        if broker is not None:
            try:
                broker.disconnect()
                print(_row(8, total_stages, "OK", "disconnect() clean"))
                passed = max(passed, 8) if passed >= 7 else passed
            except Exception as e:
                # Disconnect failure is non-fatal -- the JWT will expire
                # on its own in 8 hours. Still useful to know.
                print(_row(8, total_stages, "WARN", "disconnect()",
                           f"{type(e).__name__}: {e} (non-fatal, JWT auto-expires)"))

    # ── Summary ───────────────────────────────────────────────────────
    print()
    print("=" * 72)
    if passed == total_stages:
        print(f"SUMMARY: {passed}/{total_stages} stages PASS -- live auth path is wired correctly.")
        print()
        print("NEXT STEPS:")
        print("  1. Stage 1 (AMO order place + cancel) -- target: Saturday")
        print("  2. Stage 2 (live single-stock 5-min round-trip) -- Saturday or Sunday")
        print("  3. Stage 3 (5-stock basket via daemon in live mode) -- weekend after")
        print("  See docs/e2e_broker_test_plan.md for the full ladder.")
        return 0
    else:
        print(f"SUMMARY: {passed}/{total_stages} stages PASS, FAILED at stage {passed+1}.")
        print("Do NOT proceed to Stage 1 until this is green end-to-end.")
        return passed + 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ABORTED] Ctrl+C received before completion.")
        sys.exit(130)
    except Exception:
        print("\n[CRASHED] uncaught exception:")
        traceback.print_exc()
        sys.exit(99)
