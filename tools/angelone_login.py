"""AngelOne SmartAPI login tester.

Read-only sanity check before live cutover (May 11). Loads credentials from
``.env``, generates a TOTP code, calls ``SmartConnect.generateSession`` and
fetches profile + funds. Places NO orders. Tokens are NOT persisted to disk.

Usage::

    python tools/angelone_login.py             # standard verbose run
    python tools/angelone_login.py --quiet     # minimal output (for cron)

Exit codes:
    0  login successful, profile + funds fetched
    1  missing credentials in .env
    2  TOTP generation failed (malformed secret)
    3  SmartConnect login rejected (AngelOne side error — see message)
    4  unexpected exception
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.secrets import load_dotenv  # noqa: E402


def _mask(v: str | None, keep: int = 4) -> str:
    if not v:
        return "(EMPTY)"
    if len(v) <= keep:
        return "*" * len(v)
    return v[:keep] + "*" * (len(v) - keep)


def main() -> int:
    parser = argparse.ArgumentParser(description="AngelOne SmartAPI login tester")
    parser.add_argument("--quiet", action="store_true", help="minimal output")
    args = parser.parse_args()

    load_dotenv(str(ROOT / ".env"))

    creds = {
        "api_key":       os.environ.get("ANGELONE_API_KEY", ""),
        "api_secret":    os.environ.get("ANGELONE_API_SECRET", ""),
        "client_id":     os.environ.get("ANGELONE_CLIENT_ID", ""),
        "password":      os.environ.get("ANGELONE_PASSWORD", ""),
        "totp_secret":   os.environ.get("ANGELONE_TOTP_SECRET", ""),
    }

    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"[FAIL] missing in .env: {missing}")
        return 1

    if not args.quiet:
        print("=== Credentials (masked) ===")
        for k, v in creds.items():
            print(f"  {k:14s}: {_mask(v)}  (len={len(v)})")
        print()

    # Generate TOTP.
    try:
        import pyotp
        totp_code = pyotp.TOTP(creds["totp_secret"]).now()
        if not args.quiet:
            print(f"=== TOTP code (current 30-sec window) ===")
            print(f"  {totp_code}")
            print()
    except Exception as e:
        print(f"[FAIL] TOTP generation failed: {e}")
        return 2

    # Login to SmartAPI.
    try:
        from SmartApi import SmartConnect
    except ImportError:
        try:
            from smartapi import SmartConnect  # legacy package name
        except ImportError as e:
            print(f"[FAIL] smartapi-python not installed: {e}")
            return 4

    if not args.quiet:
        print("=== Calling SmartConnect.generateSession ===")
    try:
        obj = SmartConnect(api_key=creds["api_key"])
        session = obj.generateSession(
            creds["client_id"], creds["password"], totp_code
        )
    except Exception as e:
        print(f"[FAIL] SmartConnect raised: {e}")
        return 3

    # Inspect response.
    if not session or not session.get("status"):
        msg = (session or {}).get("message", "no message")
        code = (session or {}).get("errorcode", "no code")
        print(f"[FAIL] login rejected — code={code}  message={msg}")
        # Common error codes:
        #   AB1007 = invalid totp
        #   AB1010 = invalid client/password
        #   AB1050 = ip not whitelisted
        if "AB1007" in str(code):
            print("       -> TOTP code rejected. Either the secret is wrong, ")
            print("          or your laptop clock is more than ~30s out of sync.")
        elif "AB1010" in str(code):
            print("       -> Client ID or MPin/password is wrong.")
        elif "AB1050" in str(code) or "ip" in msg.lower():
            print("       -> IP not whitelisted. Update Primary Static IP on")
            print("          AngelOne SmartAPI dashboard with your current public IP.")
        return 3

    data = session.get("data", {}) or {}
    jwt_token     = data.get("jwtToken")
    refresh_token = data.get("refreshToken")
    feed_token    = data.get("feedToken")

    if not args.quiet:
        print()
        print("=== Login OK — tokens received (masked, in-memory only) ===")
        print(f"  jwtToken     : {_mask(jwt_token, 12)}  (len={len(jwt_token or '')})")
        print(f"  refreshToken : {_mask(refresh_token, 12)} (len={len(refresh_token or '')})")
        print(f"  feedToken    : {_mask(feed_token, 12)}  (len={len(feed_token or '')})")
        print()

    # Read-only probes.
    if not args.quiet:
        print("=== Read-only API probes ===")

    try:
        profile = obj.getProfile(refresh_token)
        if profile and profile.get("status"):
            p = profile.get("data", {}) or {}
            print(f"  [OK] profile        : name={p.get('name')!r}  "
                  f"client={p.get('clientcode')!r}  email={p.get('email')!r}  "
                  f"broker={p.get('broker')!r}  exchanges={p.get('exchanges')!r}")
        else:
            print(f"  [WARN] profile fetch returned: {profile}")
    except Exception as e:
        print(f"  [WARN] profile probe raised: {e}")

    try:
        rms = obj.rmsLimit()
        if rms and rms.get("status"):
            d = rms.get("data", {}) or {}
            avail_cash = d.get("availablecash") or d.get("net")
            net = d.get("net")
            print(f"  [OK] funds (rmsLimit): availablecash={avail_cash}  net={net}")
        else:
            print(f"  [WARN] rmsLimit returned: {rms}")
    except Exception as e:
        print(f"  [WARN] rmsLimit probe raised: {e}")

    # Clean logout — be a good citizen, free the session.
    try:
        obj.terminateSession(creds["client_id"])
        if not args.quiet:
            print()
            print("  [OK] session terminated cleanly")
    except Exception as e:
        if not args.quiet:
            print(f"  [warn] terminateSession raised (not critical): {e}")

    print()
    print("=== RESULT: AngelOne SmartAPI credentials are VALID ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
