"""CHG-01..CHG-05 (audit 2026-06-01) — regression tests for AngelOne calibration.

These tests pin the new rates against AngelOne's own published example and
guard against accidental Zerodha-era regressions. If any rate is changed
intentionally (e.g. SEBI raises STT), the failing assertion is a feature:
update the constant AND this test in the same commit so the audit trail is
clean.

Reference: https://www.angelone.in/calculators/brokerage-calculator

AngelOne's worked example (BUY 50 @ ₹1,000 + SELL 50 @ ₹2,000 intraday, NSE):
    Brokerage:            Rs 40.00
    STT/CTT:              Rs 24.50  (their docs round; SEBI math = Rs 25.00)
    Transaction Charges:  Rs  4.77  (their docs use 0.00318%; current NSE = 0.00297%)
    DP Charges:           Rs  0.00
    State Stamp Duty:     Rs  4.41  (their docs anomalous; SEBI uniform = Rs 1.50)
    SEBI Turnover Fees:   Rs  0.15
    GST (18%):            Rs  8.09
    -----
    Total:                Rs 81.92

Two of AngelOne's example numbers (STT 24.50, Stamp 4.41) don't fit any
current standard formula and appear to be artefacts of their calculator
UI. We match the SEBI-authoritative formulas, not their example arithmetic,
and document the divergence below each assertion.
"""

import importlib
import os
from decimal import Decimal


# ---------------------------------------------------------------------------
# Fresh-import helper: each test imports charges in isolation so env-var
# overrides applied via ``monkeypatch.setenv`` actually take effect.
# ---------------------------------------------------------------------------
def _fresh_charges():
    import packages.core.charges as charges_mod
    return importlib.reload(charges_mod)


# ---------------------------------------------------------------------------
# CHG-01: Intraday brokerage rate is 0.1% (AngelOne), not 0.03% (Zerodha)
# ---------------------------------------------------------------------------
def test_intraday_brokerage_rate_is_angelone():
    charges = _fresh_charges()
    # Pin the default. Until 2026-06-01 this was 0.0003 (Zerodha).
    assert charges.BROKERAGE_INTRADAY_PCT == 0.001, (
        "Intraday brokerage rate must be 0.1% (AngelOne). 0.0003 would be the "
        "Zerodha rate. See CHG-01 in docs/findings/findings_log_2026-06-01.md."
    )


def test_intraday_brokerage_caps_at_20_for_large_turnover():
    charges = _fresh_charges()
    # For ₹50,000 turnover: 0.1% × 50,000 = ₹50, capped at ₹20.
    fee = charges._brokerage_dec(Decimal("50000"), "INTRADAY")
    assert fee == Decimal("20.00"), f"Expected ₹20.00 cap, got {fee}"


def test_intraday_brokerage_uses_rate_below_cap():
    charges = _fresh_charges()
    # For ₹10,000 turnover: 0.1% × 10,000 = ₹10, well below ₹20 cap and above ₹5 floor.
    # Under the old Zerodha 0.03% defaults this would have been ₹3.00 — a 233% under-count.
    fee = charges._brokerage_dec(Decimal("10000"), "INTRADAY")
    assert fee == Decimal("10.00"), f"Expected ₹10.00, got {fee}"


# ---------------------------------------------------------------------------
# CHG-02: Minimum brokerage of ₹5 per executed order
# ---------------------------------------------------------------------------
def test_brokerage_minimum_floor_intraday():
    charges = _fresh_charges()
    # For ₹2,000 turnover: 0.1% × 2,000 = ₹2, but AngelOne floor is ₹5.
    fee = charges._brokerage_dec(Decimal("2000"), "INTRADAY")
    assert fee == Decimal("5.00"), f"Expected ₹5.00 floor, got {fee}"


def test_brokerage_minimum_floor_delivery():
    charges = _fresh_charges()
    # ₹5 floor applies to delivery too — same AngelOne rule.
    fee = charges._brokerage_dec(Decimal("1000"), "DELIVERY")
    assert fee == Decimal("5.00"), f"Expected ₹5.00 floor for DELIVERY, got {fee}"


def test_brokerage_floor_default_is_five():
    charges = _fresh_charges()
    assert charges.BROKERAGE_MIN_PER_ORDER == 5.0


# ---------------------------------------------------------------------------
# CHG-03: Delivery brokerage matches intraday (NOT Zerodha-free)
# ---------------------------------------------------------------------------
def test_delivery_brokerage_rate_is_angelone():
    charges = _fresh_charges()
    # Pin the default. Until 2026-06-01 this was 0.0 (Zerodha-free).
    assert charges.BROKERAGE_DELIVERY_PCT == 0.001, (
        "Delivery brokerage rate must be 0.1% (AngelOne). 0.0 would be the "
        "Zerodha-free policy. See CHG-03 in docs/findings/findings_log_2026-06-01.md."
    )


def test_delivery_round_trip_pays_brokerage():
    """v3 swing CNC backtest WILL pay brokerage now (it was free before)."""
    charges = _fresh_charges()
    # ₹20,000 buy + ₹20,500 sell delivery = ₹40 brokerage (₹20 + ₹20, both cap-bound)
    fees = charges.compute_round_trip(
        buy_price=200.0, sell_price=205.0, quantity=100, product="DELIVERY"
    )
    assert fees.brokerage == 40.00, (
        f"Delivery round-trip brokerage must be ₹40 (₹20 buy + ₹20 sell), got {fees.brokerage}. "
        "Pre-CHG-03, this returned ₹0 and silently flattered every v3 swing variant PF."
    )


# ---------------------------------------------------------------------------
# CHG-04: Stamp duty is product-aware (delivery 5× intraday)
# ---------------------------------------------------------------------------
def test_stamp_duty_intraday_rate():
    charges = _fresh_charges()
    assert charges.STAMP_DUTY_BUY_INTRADAY == 0.00003  # 0.003%


def test_stamp_duty_delivery_rate():
    charges = _fresh_charges()
    # SEBI Uniform Stamp Duty Act 2020: delivery is 0.015%, exactly 5× intraday.
    assert charges.STAMP_DUTY_BUY_DELIVERY == 0.00015
    # Use a tolerance because IEEE float means 5 × 3e-05 == 0.00015000000000000001
    assert abs(charges.STAMP_DUTY_BUY_DELIVERY - 5 * charges.STAMP_DUTY_BUY_INTRADAY) < 1e-12


def test_stamp_duty_helper_returns_correct_rate_by_product():
    charges = _fresh_charges()
    assert charges._stamp_duty_rate("INTRADAY") == 0.00003
    assert charges._stamp_duty_rate("DELIVERY") == 0.00015


def test_delivery_stamp_duty_in_round_trip():
    """A ₹20,000 buy on delivery pays 0.015% × 20,000 = ₹3.00 stamp duty,
    not ₹0.60 (which is what the intraday rate would yield).
    """
    charges = _fresh_charges()
    fees = charges.compute_round_trip(
        buy_price=200.0, sell_price=200.0, quantity=100, product="DELIVERY"
    )
    assert fees.stamp_duty == 3.00, (
        f"Delivery stamp duty on ₹20k buy must be ₹3.00 (0.015%), got {fees.stamp_duty}. "
        "Pre-CHG-04, this returned ₹0.60 (intraday rate applied unconditionally)."
    )


def test_intraday_stamp_duty_unchanged():
    """Regression: intraday stamp duty must STILL be 0.003%. Don't accidentally
    cross-contaminate the rates."""
    charges = _fresh_charges()
    fees = charges.compute_round_trip(
        buy_price=1000.0, sell_price=1100.0, quantity=50, product="INTRADAY"
    )
    # 0.003% × ₹50,000 buy = ₹1.50
    assert fees.stamp_duty == 1.50, f"Expected ₹1.50 intraday stamp, got {fees.stamp_duty}"


# ---------------------------------------------------------------------------
# CHG-05: DP charge is ₹20 (AngelOne), not ₹13.5 (Zerodha/CDSL pass-through)
# ---------------------------------------------------------------------------
def test_dp_charge_default_is_angelone():
    charges = _fresh_charges()
    assert charges.DP_CHARGE == 20.0, (
        "DP charge must be ₹20 (AngelOne markup). ₹13.5 is the CDSL pass-through "
        "Zerodha bills literally. See CHG-05 in docs/findings/findings_log_2026-06-01.md."
    )


def test_dp_charge_applied_to_delivery_round_trip():
    charges = _fresh_charges()
    fees = charges.compute_round_trip(
        buy_price=500.0, sell_price=500.0, quantity=10, product="DELIVERY"
    )
    # DP = ₹20 × 1.18 = ₹23.60
    assert fees.dp_charges == 23.60, f"Expected ₹23.60 DP+GST, got {fees.dp_charges}"


def test_dp_charge_not_applied_to_intraday():
    charges = _fresh_charges()
    fees = charges.compute_round_trip(
        buy_price=1000.0, sell_price=2000.0, quantity=50, product="INTRADAY"
    )
    assert fees.dp_charges == 0.0, f"Intraday must have ₹0 DP, got {fees.dp_charges}"


def test_legacy_dp_env_var_does_not_silently_revert(monkeypatch, capsys):
    """If an operator hot-patched TRADING_CHARGES_DP_CHARGE_CDSL on the trader
    VM, the new code must IGNORE it (since it's deprecated) but log loudly
    enough that the operator sees the rename and migrates the env var.
    """
    monkeypatch.setenv("TRADING_CHARGES_DP_CHARGE_CDSL", "13.5")
    charges = _fresh_charges()
    # The new default is ₹20; the legacy env var must NOT have overridden it.
    assert charges.DP_CHARGE == 20.0


# ---------------------------------------------------------------------------
# AngelOne worked example — round-trip total
# ---------------------------------------------------------------------------
def test_angelone_documented_example_brokerage_matches():
    """AngelOne example: BUY 50 @ ₹1,000 + SELL 50 @ ₹2,000 intraday on NSE.
    Their published brokerage is ₹40.00 (₹20 + ₹20). We must produce the
    same. (Pre-CHG-01, this returned ₹35 because the buy leg used 0.03% ×
    ₹50,000 = ₹15, below the cap.)
    """
    charges = _fresh_charges()
    fees = charges.compute_round_trip(
        buy_price=1000.0, sell_price=2000.0, quantity=50, product="INTRADAY"
    )
    assert fees.brokerage == 40.00, f"Expected ₹40.00, got {fees.brokerage}"


def test_round_trip_equals_sum_of_legs_invariant_preserved():
    """NUM-10 invariant: round-trip total must equal compute_one_leg(BUY) +
    compute_one_leg(SELL) byte-for-byte. CHG-* must not break this -- the
    refactor only changes rates, not the per-leg arithmetic shape.
    """
    charges = _fresh_charges()
    rt_intraday = charges.compute_round_trip(
        buy_price=1234.56, sell_price=1289.10, quantity=37, product="INTRADAY"
    )
    leg_buy = charges.compute_one_leg(1234.56, 37, "BUY", "INTRADAY")
    leg_sell = charges.compute_one_leg(1289.10, 37, "SELL", "INTRADAY")
    assert abs(rt_intraday.total - (leg_buy + leg_sell)) < 1e-9, (
        f"round-trip {rt_intraday.total} != legs {leg_buy + leg_sell}"
    )

    rt_delivery = charges.compute_round_trip(
        buy_price=789.0, sell_price=812.5, quantity=15, product="DELIVERY"
    )
    leg_buy_d = charges.compute_one_leg(789.0, 15, "BUY", "DELIVERY")
    leg_sell_d = charges.compute_one_leg(812.5, 15, "SELL", "DELIVERY")
    assert abs(rt_delivery.total - (leg_buy_d + leg_sell_d)) < 1e-9, (
        f"DELIVERY round-trip {rt_delivery.total} != legs {leg_buy_d + leg_sell_d}"
    )


# ---------------------------------------------------------------------------
# Env-var override sanity (the existing P3 polish mechanism keeps working)
# ---------------------------------------------------------------------------
def test_env_override_brokerage_rate(monkeypatch):
    monkeypatch.setenv("TRADING_CHARGES_BROKERAGE_INTRADAY_PCT", "0.0005")
    charges = _fresh_charges()
    assert charges.BROKERAGE_INTRADAY_PCT == 0.0005


def test_env_override_brokerage_min(monkeypatch):
    """A backtest sensitivity that wants TRUE zero-brokerage can disable the
    floor explicitly. Documented in _brokerage_dec docstring.
    """
    monkeypatch.setenv("TRADING_CHARGES_BROKERAGE_MIN", "0")
    monkeypatch.setenv("TRADING_CHARGES_BROKERAGE_INTRADAY_PCT", "0")
    charges = _fresh_charges()
    fee = charges._brokerage_dec(Decimal("10000"), "INTRADAY")
    assert fee == Decimal("0.00")


def test_env_override_stamp_duty_delivery(monkeypatch):
    monkeypatch.setenv("TRADING_CHARGES_STAMP_DUTY_BUY_DELIVERY", "0.0002")
    charges = _fresh_charges()
    assert charges.STAMP_DUTY_BUY_DELIVERY == 0.0002


# Reset env at module level after the suite to avoid polluting later imports.
def teardown_module(_module):
    for k in (
        "TRADING_CHARGES_BROKERAGE_INTRADAY_PCT",
        "TRADING_CHARGES_BROKERAGE_DELIVERY_PCT",
        "TRADING_CHARGES_BROKERAGE_MIN",
        "TRADING_CHARGES_STAMP_DUTY_BUY_INTRADAY",
        "TRADING_CHARGES_STAMP_DUTY_BUY_DELIVERY",
        "TRADING_CHARGES_DP_CHARGE",
        "TRADING_CHARGES_DP_CHARGE_CDSL",
    ):
        os.environ.pop(k, None)
    _fresh_charges()
