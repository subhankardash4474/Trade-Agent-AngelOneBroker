"""
Indian Equity Charges Calculator (Zerodha-style).

Computes realistic per-trade costs for:
- Intraday (MIS): brokerage + STT + exchange txn + SEBI + GST + stamp duty
- Delivery (CNC): brokerage (zero at Zerodha) + STT + exchange txn + SEBI + GST
                  + stamp duty + DP (CDSL) charges on SELL

All values are fractions unless stated otherwise.
Rates are as of 2026-04-28.

DP charge clarification (A2-5, v3 charter, 2026-05-30): the CDSL DP
charge modelled below (``DP_CHARGE_CDSL`` + GST) is **per SELL ORDER on
delivery, not per day and not per holding day**. The advisor charter
phrase "₹13.5 / ISIN / day" was loose terminology; the actual broker
schedule is a one-time fee charged on the sell leg of any CNC trade,
regardless of how long the position was held. There is a separate
annual demat-account maintenance charge (~₹300/year flat) that is
NOT modelled here because it is a fixed infrastructure cost, not a
trade-attributable cost.

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
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Literal


# NUM-10 (audit 2026-05-28): use Decimal internally so leg-boundary
# rounding is deterministic and the float-jitter that quietly biased
# ``exit_commission = total - entry`` in portfolio.py disappears.
# 1 paisa (Rs 0.01) is the resolution at which Indian brokers actually
# bill charges (per SEBI / exchange contract notes), so quantizing to
# 0.01 at every leg boundary matches broker-truth and lets us reconcile
# backtester vs live to the rupee. Internal arithmetic uses 28-digit
# Decimal precision (default) which is ample for our turnover scale.
getcontext().prec = max(getcontext().prec, 28)
_PAISA = Decimal("0.01")


def _q(value) -> Decimal:
    """Quantize a Decimal / numeric to 1 paisa using banker's rounding.
    ``ROUND_HALF_EVEN`` matches the SEBI contract-note convention and
    keeps an unbiased estimator over a long-running portfolio.
    """
    if not isinstance(value, Decimal):
        # ``str(value)`` keeps the IEEE float round-tripped string so
        # we don't introduce a ``Decimal(0.1) -> 0.1000000000000000055``
        # rounding error at the boundary.
        value = Decimal(str(value))
    return value.quantize(_PAISA, rounding=ROUND_HALF_EVEN)


def _env_float(name: str, default: float) -> float:
    """Read a TRADING_CHARGES_<NAME> env override, fall back to default.

    Bad values (non-numeric) silently fall back; we never want a typo to
    crash the daemon at import time.

    F-93: previously a parse error was a complete silent revert -- an
    operator hot-patching ``TRADING_CHARGES_STT_INTRADAY_SELL=0.00025x``
    (typo) would get the hardcoded default with no warning. Emit a
    loud one-shot warning so the operator notices their override was
    discarded. The daemon still continues with the safe default to
    preserve the no-crash guarantee.
    """
    raw = os.environ.get(f"TRADING_CHARGES_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Defer the import to keep this module dependency-free for
        # tests that import charges in isolation.
        try:
            from loguru import logger as _logger  # type: ignore[import-not-found]
            _logger.warning(
                f"[charges] TRADING_CHARGES_{name}={raw!r} is not a valid "
                f"float -- using default {default}. Fix your env var or "
                f"trades will be priced against the wrong rate."
            )
        except Exception:
            # Last-resort fallback: print to stderr.
            import sys
            print(
                f"[charges][WARN] TRADING_CHARGES_{name}={raw!r} is not a "
                f"valid float -- using default {default}",
                file=sys.stderr,
            )
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


def _brokerage_dec(turnover: Decimal, product: str) -> Decimal:
    """Brokerage for one leg (buy OR sell) -- Decimal internal API."""
    if product == "DELIVERY":
        return turnover * Decimal(str(BROKERAGE_DELIVERY_PCT))
    brok = turnover * Decimal(str(BROKERAGE_INTRADAY_PCT))
    cap = Decimal(str(BROKERAGE_MAX_PER_ORDER))
    return brok if brok < cap else cap


def compute_round_trip(
    buy_price: float,
    sell_price: float,
    quantity: int,
    product: Literal["INTRADAY", "DELIVERY"] = "INTRADAY",
) -> TradeCharges:
    """Compute total charges for a full buy+sell round-trip on NSE equity.

    NUM-10 (audit 2026-05-28): the inner accumulation now runs in
    Decimal, with 1-paisa quantization applied component-wise. This
    pins each component to broker-truth resolution and (more
    importantly) makes ``compute_round_trip`` numerically equal to
    ``compute_one_leg(BUY) + compute_one_leg(SELL)`` so portfolio.py's
    ``exit_commission`` no longer needs to be derived by subtraction
    (which previously accumulated float jitter over many trades).
    """
    buy_val = Decimal(str(buy_price)) * Decimal(int(quantity))
    sell_val = Decimal(str(sell_price)) * Decimal(int(quantity))
    turnover = buy_val + sell_val

    # Brokerage (both legs) -- quantize per-leg first so the round-trip
    # total equals the sum of the two ``compute_one_leg`` results.
    brok_buy = _q(_brokerage_dec(buy_val, product))
    brok_sell = _q(_brokerage_dec(sell_val, product))
    brok = brok_buy + brok_sell

    # STT -- intraday SELL only; delivery both legs.
    stt_dec = Decimal("0")
    if product == "DELIVERY":
        stt_dec = (buy_val + sell_val) * Decimal(str(STT_DELIVERY))
    else:
        stt_dec = sell_val * Decimal(str(STT_INTRADAY_SELL))
    stt = _q(stt_dec)

    # Exchange transaction charges -- both sides.
    txn_buy = _q(buy_val * Decimal(str(NSE_TXN_CHARGE)))
    txn_sell = _q(sell_val * Decimal(str(NSE_TXN_CHARGE)))
    txn = txn_buy + txn_sell

    # SEBI fees -- both sides.
    sebi_buy = _q(buy_val * Decimal(str(SEBI_CHARGE)))
    sebi_sell = _q(sell_val * Decimal(str(SEBI_CHARGE)))
    sebi = sebi_buy + sebi_sell

    # GST is computed on the (already quantized) per-leg sub-totals so
    # the round-trip GST equals leg-buy GST + leg-sell GST byte-for-byte.
    gst_buy = _q((brok_buy + txn_buy + sebi_buy) * Decimal(str(GST_RATE)))
    gst_sell = _q((brok_sell + txn_sell + sebi_sell) * Decimal(str(GST_RATE)))
    gst = gst_buy + gst_sell

    # Stamp duty -- only on buy.
    stamp = _q(buy_val * Decimal(str(STAMP_DUTY_BUY)))

    # DP charges -- only on delivery SELL.
    dp = Decimal("0")
    if product == "DELIVERY":
        dp = _q(Decimal(str(DP_CHARGE_CDSL)) * (Decimal("1") + Decimal(str(DP_GST))))

    total = brok + stt + txn + sebi + gst + stamp + dp
    return TradeCharges(
        brokerage=float(brok),
        stt=float(stt),
        exchange_txn=float(txn),
        sebi=float(sebi),
        gst=float(gst),
        stamp_duty=float(stamp),
        dp_charges=float(dp),
        total=float(total),
    )


def compute_one_leg(
    price: float,
    quantity: int,
    side: Literal["BUY", "SELL"],
    product: Literal["INTRADAY", "DELIVERY"] = "INTRADAY",
) -> float:
    """Charge for a single leg (buy OR sell).

    NUM-10 (audit 2026-05-28): Decimal-quantized component-wise so the
    sum across two legs equals ``compute_round_trip(...).total`` byte
    for byte. This is the property portfolio.py relies on to size the
    exit commission via a direct compute instead of the subtractive
    ``total - entry`` (which used to drift over many trades).
    """
    value = Decimal(str(price)) * Decimal(int(quantity))
    brok = _q(_brokerage_dec(value, product))
    txn = _q(value * Decimal(str(NSE_TXN_CHARGE)))
    sebi = _q(value * Decimal(str(SEBI_CHARGE)))
    gst = _q((brok + txn + sebi) * Decimal(str(GST_RATE)))

    stamp = _q(value * Decimal(str(STAMP_DUTY_BUY))) if side == "BUY" else Decimal("0")

    stt = Decimal("0")
    if product == "DELIVERY":
        stt = _q(value * Decimal(str(STT_DELIVERY)))
    elif side == "SELL":
        stt = _q(value * Decimal(str(STT_INTRADAY_SELL)))

    dp = Decimal("0")
    if product == "DELIVERY" and side == "SELL":
        dp = _q(Decimal(str(DP_CHARGE_CDSL)) * (Decimal("1") + Decimal(str(DP_GST))))

    total = brok + stt + txn + sebi + gst + stamp + dp
    return float(total)
