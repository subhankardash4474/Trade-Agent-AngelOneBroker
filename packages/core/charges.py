"""
Indian Equity Charges Calculator (Zerodha-style).

Computes realistic per-trade costs for:
- Intraday (MIS): brokerage + STT + exchange txn + SEBI + GST + stamp duty
- Delivery (CNC): brokerage (zero at Zerodha) + STT + exchange txn + SEBI + GST
                  + stamp duty + DP (CDSL) charges on SELL

All values are fractions unless stated otherwise.
Rates are as of 2026-04-28.

P3 polish (2026-05-17): rates can now be overridden at runtime via env vars
without a rebuild. Useful when SEBI / the broker bumps a rate mid-deployment
and we need a hot patch. Example:

    export TRADING_CHARGES_STT_INTRADAY_SELL=0.0003   # SEBI raised STT
    export TRADING_CHARGES_BROKERAGE_MAX=15.0          # broker promo

Any rate not overridden uses the hardcoded default. The override applies
at module-import time; callers do not need to be changed.

UPDATE PROCEDURE when SEBI / broker change rates permanently:
  1. Update the constant below.
  2. Add a comment noting the effective date + the SEBI/broker circular.
  3. Bump the model version if backtesting will be re-run with new costs.
  4. Remove any matching env-var override on the OCI VM.
"""

import os
from dataclasses import dataclass
from typing import Literal


def _env_float(name: str, default: float) -> float:
    """Read a TRADING_CHARGES_<NAME> env override, fall back to default.

    Bad values (non-numeric) silently fall back; we never want a typo to
    crash the daemon at import time."""
    raw = os.environ.get(f"TRADING_CHARGES_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ---- Rate constants (NSE Equity) ----
BROKERAGE_INTRADAY_PCT = _env_float("BROKERAGE_INTRADAY_PCT", 0.0003)
BROKERAGE_DELIVERY_PCT = _env_float("BROKERAGE_DELIVERY_PCT", 0.0)
BROKERAGE_MAX_PER_ORDER = _env_float("BROKERAGE_MAX", 20.0)

STT_INTRADAY_SELL = _env_float("STT_INTRADAY_SELL", 0.00025)
STT_DELIVERY = _env_float("STT_DELIVERY", 0.001)

NSE_TXN_CHARGE = _env_float("NSE_TXN_CHARGE", 0.0000297)
SEBI_CHARGE = _env_float("SEBI_CHARGE", 0.000001)
STAMP_DUTY_BUY = _env_float("STAMP_DUTY_BUY", 0.00003)
GST_RATE = _env_float("GST_RATE", 0.18)

# Delivery only
DP_CHARGE_CDSL = _env_float("DP_CHARGE_CDSL", 13.5)
DP_GST = _env_float("DP_GST", 0.18)


@dataclass
class TradeCharges:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    gst: float
    stamp_duty: float
    dp_charges: float
    total: float

    def to_dict(self) -> dict:
        return {
            "brokerage": round(self.brokerage, 4),
            "stt": round(self.stt, 4),
            "exchange_txn": round(self.exchange_txn, 4),
            "sebi": round(self.sebi, 4),
            "gst": round(self.gst, 4),
            "stamp_duty": round(self.stamp_duty, 4),
            "dp_charges": round(self.dp_charges, 4),
            "total": round(self.total, 4),
        }


def _brokerage(turnover: float, product: str) -> float:
    """Brokerage for one leg (buy OR sell)."""
    if product == "DELIVERY":
        return BROKERAGE_DELIVERY_PCT * turnover
    brok = BROKERAGE_INTRADAY_PCT * turnover
    return min(brok, BROKERAGE_MAX_PER_ORDER)


def compute_round_trip(
    buy_price: float,
    sell_price: float,
    quantity: int,
    product: Literal["INTRADAY", "DELIVERY"] = "INTRADAY",
) -> TradeCharges:
    """Compute total charges for a full buy+sell round-trip on NSE equity."""
    buy_val = buy_price * quantity
    sell_val = sell_price * quantity
    turnover = buy_val + sell_val

    # Brokerage (both legs)
    brok = _brokerage(buy_val, product) + _brokerage(sell_val, product)

    # STT
    if product == "DELIVERY":
        stt = STT_DELIVERY * (buy_val + sell_val)
    else:
        stt = STT_INTRADAY_SELL * sell_val

    # Exchange transaction charges (both sides)
    txn = NSE_TXN_CHARGE * turnover

    # SEBI fees
    sebi = SEBI_CHARGE * turnover

    # GST — only on brokerage + txn + SEBI
    gst = GST_RATE * (brok + txn + sebi)

    # Stamp duty — only on buy
    stamp = STAMP_DUTY_BUY * buy_val

    # DP charges — only on delivery SELL
    dp = 0.0
    if product == "DELIVERY":
        dp = DP_CHARGE_CDSL * (1 + DP_GST)

    total = brok + stt + txn + sebi + gst + stamp + dp
    return TradeCharges(
        brokerage=brok,
        stt=stt,
        exchange_txn=txn,
        sebi=sebi,
        gst=gst,
        stamp_duty=stamp,
        dp_charges=dp,
        total=total,
    )


def compute_one_leg(
    price: float,
    quantity: int,
    side: Literal["BUY", "SELL"],
    product: Literal["INTRADAY", "DELIVERY"] = "INTRADAY",
) -> float:
    """
    Approximate charge for a single leg (buy OR sell).
    Useful when you need to split cost between entry and exit.
    """
    value = price * quantity
    brok = _brokerage(value, product)
    txn = NSE_TXN_CHARGE * value
    sebi = SEBI_CHARGE * value
    gst = GST_RATE * (brok + txn + sebi)

    stamp = STAMP_DUTY_BUY * value if side == "BUY" else 0.0

    stt = 0.0
    if product == "DELIVERY":
        stt = STT_DELIVERY * value
    elif side == "SELL":
        stt = STT_INTRADAY_SELL * value

    dp = 0.0
    if product == "DELIVERY" and side == "SELL":
        dp = DP_CHARGE_CDSL * (1 + DP_GST)

    return brok + stt + txn + sebi + gst + stamp + dp
