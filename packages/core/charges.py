"""
Indian Equity Charges Calculator (AngelOne-calibrated).

Computes realistic per-trade costs for the broker the live trader actually
uses (AngelOne). Until 2026-06-01 these rates were calibrated for Zerodha
(0.03% intraday brokerage, ₹0 delivery, ₹13.5 DP, intraday-only stamp duty
applied unconditionally) which under-stated charges for any swing/CNC
backtest and for very-small intraday trades. CHG-01..CHG-05 in
``docs/findings/findings_log_2026-06-01.md`` documents the gap and the
per-variant PF adjustment that follows.

Sources for the AngelOne rates below:
- https://www.angelone.in/calculators/brokerage-calculator (rate table)
- SEBI Uniform Stamp Duty Act (2020) for the intraday/delivery split
- NSE circular for transaction-charge rate (0.00297% effective 2024)
- SEBI turnover-fee circular (₹10 per crore)

Computes per-trade costs for:
- Intraday (MIS):  brokerage [max(min(0.1% × turnover, ₹20), ₹5) per leg]
                  + STT (0.025% sell-side only)
                  + exchange txn + SEBI + GST + stamp duty (0.003% buy-side)
- Delivery (CNC): brokerage [same rule as intraday, NOT zero]
                  + STT (0.1% both legs)
                  + exchange txn + SEBI + GST
                  + stamp duty (0.015% buy-side, 5× intraday)
                  + DP (AngelOne) ₹20 + GST on SELL leg

All values are fractions unless stated otherwise.
Rates are as of 2026-06-01.

DP charge clarification (A2-5, v3 charter, 2026-05-30): the DP charge
modelled below (``DP_CHARGE`` + GST) is **per SELL ORDER on delivery, not
per day and not per holding day**. The advisor charter phrase "₹13.5 /
ISIN / day" was loose terminology; the actual broker schedule is a
one-time fee charged on the sell leg of any CNC trade, regardless of
how long the position was held. There is a separate annual demat-account
maintenance charge (~₹300/year flat) that is NOT modelled here because
it is a fixed infrastructure cost, not a trade-attributable cost.

CHG note (2026-06-01): the constant was renamed ``DP_CHARGE_CDSL`` →
``DP_CHARGE`` and the default raised from ₹13.5 (CDSL pass-through that
Zerodha bills literally) to ₹20 (AngelOne markup per their published
schedule). The previous env-var name ``TRADING_CHARGES_DP_CHARGE_CDSL``
is still honoured for one release with a one-shot WARNING so operators
who hot-patched it in production are not silently reverted.

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


# ---- Rate constants (NSE Equity, AngelOne calibration as of 2026-06-01) ----

# Brokerage: AngelOne charges max(min(0.1% × turnover, ₹20), ₹5) per executed
# order. Same rule applies to intraday and delivery. (Until CHG-01..CHG-03 on
# 2026-06-01 these defaults were Zerodha values: 0.03% intraday, 0% delivery,
# no minimum. That under-charged every delivery trade by ~₹40 round-trip
# brokerage and under-charged small intraday trades.)
BROKERAGE_INTRADAY_PCT = _env_float("BROKERAGE_INTRADAY_PCT", 0.001)
BROKERAGE_DELIVERY_PCT = _env_float("BROKERAGE_DELIVERY_PCT", 0.001)
BROKERAGE_MAX_PER_ORDER = _env_float("BROKERAGE_MAX", 20.0)
BROKERAGE_MIN_PER_ORDER = _env_float("BROKERAGE_MIN", 5.0)

STT_INTRADAY_SELL = _env_float("STT_INTRADAY_SELL", 0.00025)
STT_DELIVERY = _env_float("STT_DELIVERY", 0.001)

NSE_TXN_CHARGE = _env_float("NSE_TXN_CHARGE", 0.0000297)
SEBI_CHARGE = _env_float("SEBI_CHARGE", 0.000001)

# Stamp duty: SEBI Uniform Stamp Duty Act 2020. Same rate on every state
# (the "state-wise" language in broker UIs is a vestige of pre-2020
# regimes). Intraday and delivery have DIFFERENT rates: delivery is 5×
# intraday. (Until CHG-04 on 2026-06-01, both products shared the
# intraday rate, under-charging delivery stamp duty by 80%.)
STAMP_DUTY_BUY_INTRADAY = _env_float("STAMP_DUTY_BUY_INTRADAY", 0.00003)
STAMP_DUTY_BUY_DELIVERY = _env_float("STAMP_DUTY_BUY_DELIVERY", 0.00015)

GST_RATE = _env_float("GST_RATE", 0.18)

# DP (Depository Participant) charges -- delivery SELL leg only.
# AngelOne charges ₹20 + GST per executed sell order. (Until CHG-05 on
# 2026-06-01, the default was ₹13.5, the CDSL pass-through Zerodha bills
# without markup.) Env-var name change: DP_CHARGE_CDSL -> DP_CHARGE. The
# old name is honoured for backward compatibility (see ``_deprecated_dp_env``).
DP_CHARGE = _env_float("DP_CHARGE", 20.0)
DP_GST = _env_float("DP_GST", 0.18)


def _deprecated_dp_env() -> None:
    """Warn once if the operator still has TRADING_CHARGES_DP_CHARGE_CDSL set.

    CHG-05 (2026-06-01): the env var was renamed. If an operator hot-patched
    the old name on the trader VM, silently reverting to the new ₹20 default
    would mis-price every delivery sell leg. Print a CRITICAL log line so
    the operator notices before the next trading session.
    """
    legacy = os.environ.get("TRADING_CHARGES_DP_CHARGE_CDSL")
    if legacy is None:
        return
    try:
        from loguru import logger as _logger  # type: ignore[import-not-found]
        _logger.critical(
            f"[charges] env var TRADING_CHARGES_DP_CHARGE_CDSL={legacy!r} is "
            f"DEPRECATED -- rename to TRADING_CHARGES_DP_CHARGE. The current "
            f"value is being IGNORED; DP charge defaults to AngelOne ₹{DP_CHARGE}. "
            f"Fix the env var or every delivery sell leg will be mis-priced."
        )
    except Exception:
        import sys
        print(
            f"[charges][CRITICAL] env var TRADING_CHARGES_DP_CHARGE_CDSL={legacy!r} "
            f"is DEPRECATED -- rename to TRADING_CHARGES_DP_CHARGE. Value being "
            f"IGNORED; using AngelOne ₹{DP_CHARGE} default.",
            file=sys.stderr,
        )


_deprecated_dp_env()


def _log_active_rates() -> None:
    """Emit a single INFO line at module-import time naming the active
    broker and brokerage rate, so any operator who reads the daemon log
    can immediately see whether the cost model matches the live broker.

    CHG (2026-06-01): the absence of a "we are calibrated for X" line was
    the reason the Zerodha-vs-AngelOne mismatch survived 6 months of audits.
    """
    broker_hint = "AngelOne" if abs(BROKERAGE_INTRADAY_PCT - 0.001) < 1e-9 else "custom"
    try:
        from loguru import logger as _logger  # type: ignore[import-not-found]
        _logger.info(
            f"[charges] active rates: broker={broker_hint} | "
            f"intraday_brokerage_pct={BROKERAGE_INTRADAY_PCT} | "
            f"delivery_brokerage_pct={BROKERAGE_DELIVERY_PCT} | "
            f"brokerage_cap=Rs{BROKERAGE_MAX_PER_ORDER} | "
            f"brokerage_min=Rs{BROKERAGE_MIN_PER_ORDER} | "
            f"stt_intraday_sell={STT_INTRADAY_SELL} | "
            f"stt_delivery={STT_DELIVERY} | "
            f"stamp_intraday_buy={STAMP_DUTY_BUY_INTRADAY} | "
            f"stamp_delivery_buy={STAMP_DUTY_BUY_DELIVERY} | "
            f"dp_charge=Rs{DP_CHARGE}"
        )
    except Exception:
        # loguru not available (test isolation, early import); silently skip.
        # The rate values are still applied; only the disclosure log is missing.
        pass


_log_active_rates()


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
    """Brokerage for one leg (buy OR sell) -- Decimal internal API.

    AngelOne rule (CHG-01..CHG-03, 2026-06-01): ``max(min(rate × turnover, cap), floor)``
    per executed order. Same rule for intraday and delivery, just a different
    ``rate`` constant.

    Edge case: a zero turnover (no actual order) returns the floor (₹5), which
    matches AngelOne's published "minimum brokerage per executed order" rule.
    If a backtest sensitivity wants TRUE zero brokerage (e.g. "what if AngelOne
    waived charges"), set both ``BROKERAGE_*_PCT`` and ``BROKERAGE_MIN`` to 0
    via env vars.
    """
    rate = BROKERAGE_DELIVERY_PCT if product == "DELIVERY" else BROKERAGE_INTRADAY_PCT
    brok = turnover * Decimal(str(rate))
    cap = Decimal(str(BROKERAGE_MAX_PER_ORDER))
    floor = Decimal(str(BROKERAGE_MIN_PER_ORDER))
    # ``max(min(brok, cap), floor)`` -- expressed without builtins for Decimal
    capped = brok if brok < cap else cap
    return capped if capped > floor else floor


def _stamp_duty_rate(product: str) -> float:
    """Return SEBI Uniform Stamp Duty rate for the requested product.

    CHG-04 (2026-06-01): previously a single ``STAMP_DUTY_BUY`` constant was
    applied unconditionally at the intraday rate (0.003%); for delivery the
    correct rate is 0.015% (5× higher). The split below mirrors the SEBI
    Uniform Stamp Duty Act 2020.
    """
    return STAMP_DUTY_BUY_DELIVERY if product == "DELIVERY" else STAMP_DUTY_BUY_INTRADAY


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
    # CHG hotfix (2026-06-01): the delivery branch previously summed BUY+SELL
    # value and quantized once, which violated the NUM-10 invariant that
    # ``compute_round_trip == compute_one_leg(BUY) + compute_one_leg(SELL)``
    # for delivery trades. Quantize each leg independently so portfolio.py's
    # subtraction-free exit-commission split stays byte-exact.
    if product == "DELIVERY":
        stt_buy = _q(buy_val * Decimal(str(STT_DELIVERY)))
        stt_sell = _q(sell_val * Decimal(str(STT_DELIVERY)))
        stt = stt_buy + stt_sell
    else:
        stt = _q(sell_val * Decimal(str(STT_INTRADAY_SELL)))

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

    # Stamp duty -- only on buy. Product-aware rate (CHG-04, 2026-06-01).
    stamp = _q(buy_val * Decimal(str(_stamp_duty_rate(product))))

    # DP charges -- only on delivery SELL. (CHG-05, 2026-06-01: now ₹20 AngelOne.)
    dp = Decimal("0")
    if product == "DELIVERY":
        dp = _q(Decimal(str(DP_CHARGE)) * (Decimal("1") + Decimal(str(DP_GST))))

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

    # Stamp duty: buy-only, product-aware rate (CHG-04, 2026-06-01).
    stamp = (
        _q(value * Decimal(str(_stamp_duty_rate(product))))
        if side == "BUY"
        else Decimal("0")
    )

    stt = Decimal("0")
    if product == "DELIVERY":
        stt = _q(value * Decimal(str(STT_DELIVERY)))
    elif side == "SELL":
        stt = _q(value * Decimal(str(STT_INTRADAY_SELL)))

    # DP charges: delivery SELL only, ₹20 + GST (CHG-05, 2026-06-01).
    dp = Decimal("0")
    if product == "DELIVERY" and side == "SELL":
        dp = _q(Decimal(str(DP_CHARGE)) * (Decimal("1") + Decimal(str(DP_GST))))

    total = brok + stt + txn + sebi + gst + stamp + dp
    return float(total)
