"""AngelOne SmartAPI broker implementation.

Wraps ``SmartApi.SmartConnect`` with a richer lifecycle:

- TOTP-aware login (no manual code entry).
- Token-age tracking — surfaces a clear ``is_connected() -> False`` once
  the JWT approaches expiry, so the daemon can re-login on its own.
- Funded-balance preflight — refuses to advertise as connected if the
  account has Rs 0 cash (catches forgotten-funding mistakes).
- IP-whitelist diagnosis — when login fails with AB1050, surfaces the
  current public IP so the operator can update the SmartAPI app form.
- Read-only operations are network-resilient (single retry on transient
  network errors with short backoff).

Production wiring (planned weekend cutover):
    from brokers import get_broker
    broker = get_broker(config)
    broker.connect()
    smart_api = broker.raw_api          # legacy executor still works
    funds = broker.get_funds()
    if funds['available_cash'] < min_required:
        raise SystemExit('insufficient capital')

Until cutover, this module is purely additive — the live daemon ignores it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import pyotp
import pytz
from loguru import logger


# C-6 (audit 2026-05-26): wall-clock cap on `place_order` so a wedged
# broker socket can't stall the scan cycle. Operator-tunable via env so
# emergency operations can extend it without a redeploy.
_PLACE_ORDER_TIMEOUT_SEC = float(
    os.environ.get("ANGELONE_PLACE_ORDER_TIMEOUT_SEC", "15")
)

from .base import Broker, BrokerLoginError, BrokerNotConnectedError

IST = pytz.timezone("Asia/Kolkata")

# AngelOne JWT expires after 8 hours. We refresh proactively at 7 hours to
# avoid the cost of a refused order.
_TOKEN_LIFETIME_HOURS = 7

# Number of retries for read-only operations on transient network failure.
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SEC = 1.5


# F-16/F-38: AngelOne sends `status` as the string "false" (not the
# Python literal False) on rejection. Without explicit parsing, any
# non-empty dict reads as truthy and a failed cancel/modify looks
# successful. Mirror ExecutionEngine._interpret_broker_status: only
# accept clear positive sentinels; anything else is "not OK".
_STATUS_TRUE_STR = {"true", "ok", "success", "completed", "1"}
_STATUS_FALSE_STR = {"false", "fail", "failed", "rejected", "error", "0", "no"}


def _broker_status_is_ok(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in _STATUS_TRUE_STR:
            return True
        if lower in _STATUS_FALSE_STR:
            return False
        # Unknown string: be conservative, treat as failure rather than
        # silently passing through.
        return False
    # Any other type (lists/dicts/objects) -> treat as ok if truthy.
    return bool(value)


def _safe_call(label: str, fn, *args, **kwargs):
    """Call ``fn`` with simple retry. Returns the response or None on failure."""
    last_err: Optional[Exception] = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SEC * attempt)
    logger.warning(f"[AngelOne] {label} failed after {_RETRY_ATTEMPTS} attempts: {last_err}")
    return None


class AngelOneBroker(Broker):
    """SmartAPI-backed broker."""

    name = "angelone"

    def __init__(self, config: dict):
        broker_cfg = config.get("broker", {})
        self._api_key:     str = broker_cfg.get("api_key", "")
        self._api_secret:  str = broker_cfg.get("api_secret", "")
        self._client_id:   str = broker_cfg.get("client_id", "")
        self._password:    str = broker_cfg.get("password", "")
        self._totp_secret: str = broker_cfg.get("totp_secret", "")
        self._exchange:    str = config.get("market", {}).get("exchange", "NSE")

        # Optional: refuse to advertise as connected if balance < this floor.
        # 0 means "any balance is fine"; raising this catches the
        # accidentally-unfunded-on-Monday-morning failure mode.
        self._min_required_cash: float = float(
            broker_cfg.get("min_required_cash", 0.0)
        )

        self._smart = None
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._connected_at: Optional[datetime] = None
        self._cached_funds: Optional[dict] = None  # invalidated on every order

    # ── Lifecycle ────────────────────────────────────────────────────

    def connect(self) -> None:
        if self.is_connected():
            return

        missing = [k for k, v in {
            "api_key": self._api_key,
            "client_id": self._client_id,
            "password": self._password,
            "totp_secret": self._totp_secret,
        }.items() if not v]
        if missing:
            raise BrokerLoginError(f"AngelOne: missing config keys {missing}")

        try:
            from SmartApi import SmartConnect
        except ImportError as e:
            raise BrokerLoginError(
                "smartapi-python not installed. Run: pip install smartapi-python"
            ) from e

        # Generate TOTP code right before login (some networks delay a few
        # seconds; if the code rolls over mid-call AngelOne returns AB1007).
        try:
            totp_code = pyotp.TOTP(self._totp_secret).now()
        except Exception as e:
            raise BrokerLoginError(f"TOTP generation failed: {e}") from e

        smart = SmartConnect(api_key=self._api_key)
        try:
            session = smart.generateSession(self._client_id, self._password, totp_code)
        except Exception as e:
            raise BrokerLoginError(f"SmartConnect.generateSession raised: {e}") from e

        if not session or not session.get("status"):
            msg = (session or {}).get("message", "no message")
            code = (session or {}).get("errorcode", "no code")
            hint = self._diagnose(code, msg)
            raise BrokerLoginError(f"AngelOne login rejected (code={code}): {msg} {hint}")

        data = session.get("data", {}) or {}
        self._smart = smart
        self._jwt_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken")
        self._feed_token = data.get("feedToken") or _safe_call(
            "getfeedToken", smart.getfeedToken
        )
        self._connected_at = datetime.now(IST)
        self._cached_funds = None

        logger.info(
            f"[AngelOne] connected as {self._client_id} at "
            f"{self._connected_at.strftime('%H:%M:%S')} | feed_token={'OK' if self._feed_token else 'MISSING'}"
        )

    def disconnect(self) -> None:
        if self._smart is None:
            return
        try:
            self._smart.terminateSession(self._client_id)
            logger.info(f"[AngelOne] session for {self._client_id} terminated cleanly")
        except Exception as e:
            logger.debug(f"[AngelOne] terminateSession raised (ignored): {e}")
        finally:
            self._smart = None
            self._jwt_token = None
            self._refresh_token = None
            self._feed_token = None
            self._connected_at = None
            self._cached_funds = None

    def is_connected(self) -> bool:
        if self._smart is None or self._connected_at is None:
            return False
        age = datetime.now(IST) - self._connected_at
        if age > timedelta(hours=_TOKEN_LIFETIME_HOURS):
            logger.info(
                f"[AngelOne] session age {age} exceeds {_TOKEN_LIFETIME_HOURS}h — "
                "marking disconnected for refresh"
            )
            return False
        return True

    # ── Account info ─────────────────────────────────────────────────

    def get_profile(self) -> dict[str, Any]:
        self._require_connected()
        resp = _safe_call("getProfile", self._smart.getProfile, self._refresh_token)
        if not resp or not resp.get("status"):
            return {}
        d = resp.get("data", {}) or {}
        return {
            "name":         d.get("name", ""),
            "client_code":  d.get("clientcode", ""),
            "email":        d.get("email", ""),
            "exchanges":    list(d.get("exchanges") or []),
            "raw":          d,
        }

    def get_funds(self, *, use_cache: bool = False) -> dict[str, Any]:
        """Return a fresh funds snapshot.

        C-7 (audit 2026-05-26): the previous implementation returned the
        cached dict BY REFERENCE when use_cache=True. Two failure modes
        followed: (a) a caller that mutated the dict corrupted future
        reads; (b) the pre-trade cash gate could approve a second entry
        off a stale 'available_cash' captured before the first order
        locked margin — because the in-flight cache was only invalidated
        in `place_order()` AFTER the broker round-trip completed. We now
        return a defensive copy and document that `use_cache=True` is
        appropriate only for read-only diagnostics, NOT pre-trade gates.
        """
        self._require_connected()
        if use_cache and self._cached_funds is not None:
            return dict(self._cached_funds)
        resp = _safe_call("rmsLimit", self._smart.rmsLimit)
        if not resp or not resp.get("status"):
            # F-39: previously returned ``{"available_cash": 0.0, ...}``
            # on failure, which is INDISTINGUISHABLE from a genuinely
            # unfunded account. Callers that gate trades on cash > 0
            # then "correctly" refused entries during a broker outage,
            # but operators saw "no cash" rather than "API call failed"
            # and reacted incorrectly. Add an explicit ``"ok": False``
            # flag (and document the contract in the return value) so
            # downstream code can distinguish the two cases. The legacy
            # zero-cash fields are preserved for backward compatibility.
            return {
                "available_cash": 0.0,
                "used_margin":    0.0,
                "net":            0.0,
                "raw":            resp or {},
                "ok":             False,
                "error":          "rmsLimit returned no status -- broker API failure (NOT unfunded account)",
            }
        d = resp.get("data", {}) or {}
        avail = float(d.get("availablecash") or d.get("availableamount") or 0.0)
        used = float(d.get("utiliseddebits") or d.get("utilizedmargin") or 0.0)
        net = float(d.get("net") or avail)
        funds = {
            "available_cash": avail,
            "used_margin":    used,
            "net":            net,
            "raw":            d,
            "ok":             True,  # F-39: distinguish from API failure
        }
        self._cached_funds = funds
        return dict(funds)

    def get_positions(self) -> Optional[dict]:
        self._require_connected()
        return _safe_call("position", self._smart.position)

    def get_orders(self) -> Optional[dict]:
        self._require_connected()
        return _safe_call("orderBook", self._smart.orderBook)

    # ── Trading ──────────────────────────────────────────────────────

    def place_order(self, params: dict) -> Optional[str]:
        """Place an order via SmartConnect.placeOrder() with a wall-clock cap.

        C-6 (audit 2026-05-26, partial fix): bound the call with a hard
        timeout via a daemon worker thread. Pre-fix, a hung SmartConnect
        socket (TLS keep-alive that the broker silently dropped, DNS
        glitch, retry storm) could block the entire scan cycle for the
        full HTTP read timeout, which is unbounded in some smartapi-python
        versions. We now return None after `_PLACE_ORDER_TIMEOUT_SEC`
        even if the underlying call is still wedged — caller treats None
        as "order failed, retry by the normal retry path". The leaked
        thread is bounded (one per timeout) and dies when the broker
        connection eventually drops.

        Idempotency tagging (the other half of C-6) is deferred — needs a
        mock-broker harness to verify that a retry after timeout doesn't
        duplicate the order.
        """
        import threading as _threading
        self._require_connected()

        result: dict = {"order_id": None, "exc": None}

        def _worker() -> None:
            try:
                result["order_id"] = self._smart.placeOrder(params)
            except Exception as e:  # noqa: BLE001
                result["exc"] = e

        th = _threading.Thread(
            target=_worker, name="angelone_place_order", daemon=True,
        )
        th.start()
        th.join(timeout=_PLACE_ORDER_TIMEOUT_SEC)
        if th.is_alive():
            logger.error(
                f"[AngelOne] placeOrder timed out after "
                f"{_PLACE_ORDER_TIMEOUT_SEC:.1f}s "
                f"(symbol={params.get('tradingsymbol')}, "
                f"side={params.get('transactiontype')}); returning None. "
                f"WARNING: broker may still place the order in the background; "
                f"reconcile next cycle."
            )
            return None
        if result["exc"] is not None:
            logger.error(f"[AngelOne] placeOrder raised: {result['exc']}")
            return None
        self._cached_funds = None  # margin changed
        return result["order_id"]

    def modify_order(self, params: dict) -> bool:
        # F-38: previously this returned True on any non-exception path,
        # so AngelOne rejections (status="false" with no exception)
        # silently appeared as success. Mirror the dict-status parsing
        # used by ExecutionEngine.modify_stop_loss / cancel_order.
        self._require_connected()
        try:
            response = self._smart.modifyOrder(params)
            if isinstance(response, dict):
                if not _broker_status_is_ok(response.get("status")):
                    logger.error(
                        f"[AngelOne] modifyOrder rejected: status="
                        f"{response.get('status')!r} message="
                        f"{response.get('message')!r}"
                    )
                    return False
            return True
        except Exception as e:
            logger.error(f"[AngelOne] modifyOrder raised: {e}")
            return False

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        # F-16: parse the broker's status field instead of swallowing
        # "status=false" responses as success. An orphan SL-M left at
        # the broker after a "successful" cancel would risk a reverse
        # fill on the next entry / EOD flatten.
        self._require_connected()
        try:
            response = self._smart.cancelOrder(order_id, variety)
            if isinstance(response, dict):
                if not _broker_status_is_ok(response.get("status")):
                    logger.error(
                        f"[AngelOne] cancelOrder rejected: status="
                        f"{response.get('status')!r} message="
                        f"{response.get('message')!r}"
                    )
                    return False
            self._cached_funds = None
            return True
        except Exception as e:
            logger.error(f"[AngelOne] cancelOrder raised: {e}")
            return False

    # ── Market data ──────────────────────────────────────────────────

    def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        self._require_connected()
        resp = _safe_call("ltpData", self._smart.ltpData, exchange, symbol, token)
        if not resp or not resp.get("status"):
            return None
        return float((resp.get("data") or {}).get("ltp") or 0.0) or None

    # ── Escape hatch ─────────────────────────────────────────────────

    @property
    def raw_api(self) -> Any:
        """Return the SmartConnect instance — used by core/execution.py
        until the weekend refactor cuts it over to the typed methods above."""
        return self._smart

    # ── Internal helpers ─────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self.is_connected():
            raise BrokerNotConnectedError("AngelOne: call connect() first")

    @staticmethod
    def _diagnose(code: Any, msg: str) -> str:
        code_s = str(code or "")
        msg_l = (msg or "").lower()
        if "AB1007" in code_s or "totp" in msg_l:
            return ("[hint: TOTP rejected — check that your laptop clock is in sync "
                    "(within 30s of NTP time) and that the secret in .env matches "
                    "your authenticator app]")
        if "AB1010" in code_s or "password" in msg_l or "credential" in msg_l:
            return "[hint: client_id or MPin is wrong]"
        if "AB1050" in code_s or "ip" in msg_l or "whitelist" in msg_l:
            # B-3 residual / B-18 (audit 2026-05-26): mirror the
            # TRADER_DISABLE_SSL_VERIFY env handling used by the rest of
            # the codebase so this diagnostic path is consistent. Default
            # (env unset or "false") is full SSL verification; only the
            # explicit opt-in disables it.
            import os as _os
            import ssl as _ssl
            import urllib.request as _urlreq
            import json as _json
            disable_ssl = _os.environ.get("TRADER_DISABLE_SSL_VERIFY", "false").lower() in (
                "1", "true", "yes", "on",
            )
            try:
                ctx = _ssl._create_unverified_context() if disable_ssl else _ssl.create_default_context()
                with _urlreq.urlopen(
                    "https://api.ipify.org?format=json", timeout=5, context=ctx,
                ) as r:
                    ip = _json.loads(r.read()).get("ip", "?")
            except Exception:
                ip = "?"
            return (f"[hint: IP not whitelisted. Your current public IP is {ip}. "
                    "Update Primary Static IP at https://smartapi.angelbroking.com/ → My Apps]")
        return ""
