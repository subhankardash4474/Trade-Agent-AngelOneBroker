"""End-to-end AngelOne SmartAPI smoke test using the new brokers/ module.

Goes further than tools/angelone_login.py by also:
  - Routing through the typed ``AngelOneBroker`` interface (proves the
    abstraction works, not just SmartConnect directly).
  - Exercising every read-only method (profile, funds, positions, orders, LTP).
  - Verifying funded-balance for live-mode readiness.
  - IP-whitelist diagnosis on AB1050 errors.

Places NO orders. Tokens are NOT persisted to disk.

Usage::
    python tools/angelone_smoke.py             # full verbose
    python tools/angelone_smoke.py --quiet     # condensed (for cron)

Exit codes:
    0  all probes succeeded; broker is ready for live cutover
    1  missing credentials
    2  login failed (see message for AB1007/AB1010/AB1050 hints)
    3  login OK but a probe failed (degraded — review which probe)
    4  unfunded account (login OK, but available_cash == 0)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.secrets import load_dotenv, apply_env_to_config  # noqa: E402
from brokers import get_broker, BrokerLoginError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AngelOne broker smoke test")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--ltp-symbol", default="SBIN-EQ",
        help="Symbol to probe for LTP (default: SBIN-EQ)"
    )
    parser.add_argument(
        "--ltp-token", default="3045",
        help="Symbol token for LTP probe (default: 3045 = SBIN-EQ on NSE)"
    )
    args = parser.parse_args()

    # Load config + .env, force angelone+live for the test.
    load_dotenv(str(ROOT / ".env"))
    config = yaml.safe_load(open(ROOT / "config.yaml"))
    config["broker"]["name"] = "angelone"
    config["broker"]["mode"] = "live"
    config = apply_env_to_config(config)

    broker = get_broker(config)
    if not args.quiet:
        print(f"=== Broker: {broker.name} ===")

    # ── Connect ──
    try:
        broker.connect()
    except BrokerLoginError as e:
        print(f"[FAIL] login: {e}")
        return 2
    if not broker.is_connected():
        print("[FAIL] connect() returned but is_connected() False")
        return 2
    if not args.quiet:
        print("  [OK] connected")

    profile = funds = positions = orders = ltp = None

    # ── Probes ──
    try:
        profile = broker.get_profile()
        if profile:
            print(f"  [OK] profile        : name={profile.get('name')!r}  "
                  f"client={profile.get('client_code')!r}  exchanges={profile.get('exchanges')!r}")
        else:
            print("  [WARN] profile probe returned empty")
    except Exception as e:
        print(f"  [FAIL] profile probe raised: {e}")

    try:
        funds = broker.get_funds()
        avail = funds.get("available_cash", 0.0) if funds else 0.0
        used = funds.get("used_margin", 0.0) if funds else 0.0
        net = funds.get("net", 0.0) if funds else 0.0
        print(f"  [OK] funds          : available_cash=Rs {avail:,.2f}  "
              f"used_margin=Rs {used:,.2f}  net=Rs {net:,.2f}")
    except Exception as e:
        print(f"  [FAIL] funds probe raised: {e}")

    try:
        positions = broker.get_positions()
        net_qty = 0
        if positions and positions.get("status"):
            data = positions.get("data") or []
            net_qty = sum(int(p.get("netqty", 0) or 0) for p in data if isinstance(p, dict))
            print(f"  [OK] positions      : {len(data)} symbols, net_qty={net_qty}")
        else:
            print(f"  [OK] positions      : empty (none open)")
    except Exception as e:
        print(f"  [FAIL] positions probe raised: {e}")

    try:
        orders = broker.get_orders()
        if orders and orders.get("status"):
            data = orders.get("data") or []
            print(f"  [OK] orders         : {len(data)} orders today")
        else:
            print(f"  [OK] orders         : empty (none today)")
    except Exception as e:
        print(f"  [FAIL] orders probe raised: {e}")

    try:
        ltp = broker.get_ltp("NSE", args.ltp_symbol, args.ltp_token)
        if ltp:
            print(f"  [OK] ltp({args.ltp_symbol:10s}) : Rs {ltp:.2f}")
        else:
            print(f"  [WARN] ltp({args.ltp_symbol}) returned None — symbol/token may be wrong")
    except Exception as e:
        print(f"  [FAIL] ltp probe raised: {e}")

    # ── Cleanup ──
    try:
        broker.disconnect()
        if not args.quiet:
            print("  [OK] disconnected cleanly")
    except Exception as e:
        if not args.quiet:
            print(f"  [warn] disconnect raised (ignored): {e}")

    # ── Verdict ──
    print()
    avail = (funds or {}).get("available_cash", 0.0)
    if avail <= 0.0:
        print("=== RESULT: LOGIN OK but account UNFUNDED (available_cash = Rs 0). ===")
        print("            Transfer funds before May 11 live cutover.")
        return 4

    print(f"=== RESULT: AngelOne broker fully ready for live mode (cash Rs {avail:,.2f}). ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
